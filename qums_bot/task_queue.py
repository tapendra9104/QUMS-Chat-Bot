from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings, load_settings
from .db import Database
from .monitoring import capture_monitoring_exception, init_monitoring
from .runtime import build_runtime
from .service import BotService

try:
    from redis import Redis
    from rq import Queue, Worker
except ImportError:  # pragma: no cover - optional dependency path
    Queue = None
    Redis = None
    Worker = None


@dataclass(frozen=True)
class DispatchResult:
    dispatched: bool
    mode: str
    slot_key: str


class TaskDispatcher:
    def __init__(self, *, settings: Settings, db: Database, service: BotService) -> None:
        self.settings = settings
        self.db = db
        self.service = service
        self.timezone = ZoneInfo(settings.local_timezone)
        self._queue = None
        self._redis = None

    def dispatch_periodic(
        self,
        *,
        job_name: str,
        callback_name: str,
        interval_minutes: int | None = None,
        interval_seconds: int | None = None,
        now: datetime | None = None,
    ) -> DispatchResult:
        current = now or datetime.now(self.timezone)
        slot_key = self._slot_key(current, interval_minutes=interval_minutes, interval_seconds=interval_seconds)
        if not self._try_claim_slot(job_name, slot_key):
            return DispatchResult(dispatched=False, mode=self.settings.task_queue_mode, slot_key=slot_key)

        if self.settings.task_queue_mode == "rq":
            self._enqueue_rq(callback_name, job_name=job_name, slot_key=slot_key)
        else:
            callback = getattr(self.service, callback_name)
            if now is not None and callback_name in {
                "run_scheduled_dispatch",
                "run_substitution_sweep",
                "run_monitor_sweep",
                "run_evening_sweep",
                "run_retry_sweep",
            }:
                callback(now=now)
            else:
                callback()
        return DispatchResult(dispatched=True, mode=self.settings.task_queue_mode, slot_key=slot_key)

    def _enqueue_rq(self, callback_name: str, *, job_name: str, slot_key: str) -> None:
        if Queue is None or Redis is None:
            raise RuntimeError("TASK_QUEUE_MODE=rq requires the redis and rq packages.")
        if not self.settings.redis_url:
            raise RuntimeError("TASK_QUEUE_MODE=rq requires REDIS_URL.")
        if self._queue is None:
            self._queue = Queue(self.settings.task_queue_name, connection=self._redis_connection())
        self._queue.enqueue(
            execute_dispatched_task,
            callback_name,
            job_id=f"{job_name}:{slot_key}",
        )

    def _try_claim_slot(self, job_name: str, slot_key: str) -> bool:
        if self.settings.task_queue_mode != "rq":
            return self.db.try_claim_job_slot(job_name, slot_key)
        redis_conn = self._redis_connection()
        claim_key = f"{self.settings.task_queue_name}:dispatch:{job_name}:{slot_key}"
        return bool(redis_conn.set(claim_key, "1", nx=True, ex=7 * 24 * 60 * 60))

    def _redis_connection(self):
        if Redis is None:
            raise RuntimeError("TASK_QUEUE_MODE=rq requires the redis package.")
        if not self.settings.redis_url:
            raise RuntimeError("TASK_QUEUE_MODE=rq requires REDIS_URL.")
        if self._redis is None:
            self._redis = Redis.from_url(self.settings.redis_url)
        return self._redis

    def _slot_key(
        self,
        current: datetime,
        *,
        interval_minutes: int | None = None,
        interval_seconds: int | None = None,
    ) -> str:
        normalized = current.astimezone(self.timezone).replace(second=0, microsecond=0)
        if interval_seconds is not None:
            precise = current.astimezone(self.timezone).replace(microsecond=0)
            if interval_seconds <= 1:
                return precise.strftime("%Y-%m-%dT%H:%M:%S")
            floored_second = precise.second - (precise.second % interval_seconds)
            precise = precise.replace(second=floored_second)
            return precise.strftime("%Y-%m-%dT%H:%M:%S")
        if interval_minutes is None or interval_minutes <= 1:
            return normalized.strftime("%Y-%m-%dT%H:%M")
        floored_minute = normalized.minute - (normalized.minute % interval_minutes)
        normalized = normalized.replace(minute=floored_minute)
        return normalized.strftime("%Y-%m-%dT%H:%M")


def execute_dispatched_task(callback_name: str) -> None:
    settings = load_settings()
    init_monitoring(settings, component="worker-job")
    try:
        runtime = build_runtime(settings)
        getattr(runtime.service, callback_name)()
    except Exception as exc:
        capture_monitoring_exception(exc, flush=True)
        raise


def run_worker() -> None:
    settings = load_settings()
    init_monitoring(settings, component="worker")
    if settings.task_queue_mode != "rq":
        raise RuntimeError("The worker can only run when TASK_QUEUE_MODE=rq.")
    if Queue is None or Redis is None or Worker is None:
        raise RuntimeError("TASK_QUEUE_MODE=rq requires the redis and rq packages.")
    if not settings.redis_url:
        raise RuntimeError("TASK_QUEUE_MODE=rq requires REDIS_URL.")

    redis_conn = Redis.from_url(settings.redis_url)
    worker = Worker([settings.task_queue_name], connection=redis_conn)
    worker.work()
