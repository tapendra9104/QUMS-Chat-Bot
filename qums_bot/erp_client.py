from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import requests

from .config import Settings
from .models import PendingLogin, Student
from .parsers import extract_login_state, parse_student_detail_response
from .security import decrypt_text


class ERPClientError(Exception):
    pass


class AuthenticationRequired(ERPClientError):
    pass


class LoginFailed(ERPClientError):
    pass


@dataclass
class LoginResult:
    cookies_json: str
    reg_id: str | None
    student_name: str | None


class ERPClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def start_manual_login(self, student: Student) -> PendingLogin:
        session = self._new_session()
        response = self._request(
            session,
            "get",
            f"{self.settings.base_url}/",
            context="ERP login page",
            timeout=30,
        )
        self._raise_response_error(response, "ERP login page")

        login_state = extract_login_state(response.text)
        if not login_state["request_verification_token"] or not login_state["captcha_data_url"]:
            raise ERPClientError("Could not parse the ERP login page.")

        return PendingLogin(
            student_id=student.id,
            request_verification_token=login_state["request_verification_token"],
            hdn_msg=login_state["hdn_msg"],
            check_online=login_state["check_online"],
            client_ip=login_state["client_ip"],
            captcha_data_url=login_state["captcha_data_url"],
            cookies_json=self._serialize_cookies(session),
            created_at=response.headers.get("date", ""),
        )

    def refresh_captcha(self, pending: PendingLogin) -> PendingLogin:
        session = self._session_from_cookies(pending.cookies_json)
        response = self._request(
            session,
            "post",
            f"{self.settings.base_url}/Account/showrefreshcaptchaImage",
            context="ERP captcha refresh",
            data={},
            timeout=30,
        )
        self._raise_response_error(response, "ERP captcha refresh")
        if not response.content:
            raise ERPClientError("ERP returned an empty captcha image.")

        data_url = "data:image/png;base64," + base64.b64encode(response.content).decode("ascii")
        return PendingLogin(
            student_id=pending.student_id,
            request_verification_token=pending.request_verification_token,
            hdn_msg=pending.hdn_msg,
            check_online=pending.check_online,
            client_ip=pending.client_ip,
            captcha_data_url=data_url,
            cookies_json=self._serialize_cookies(session),
            created_at=pending.created_at,
        )

    def complete_manual_login(self, student: Student, pending: PendingLogin, captcha: str) -> LoginResult:
        session = self._session_from_cookies(pending.cookies_json)
        password = decrypt_text(self.settings.app_secret, student.password_encrypted)
        payload = {
            "hdnMsg": pending.hdn_msg,
            "checkOnline": pending.check_online,
            "__RequestVerificationToken": pending.request_verification_token,
            "UserName": student.user_name,
            "Password": password,
            "clientIP": pending.client_ip,
            "captcha": captcha.strip(),
        }

        response = self._request(
            session,
            "post",
            f"{self.settings.base_url}/",
            context="ERP login submit",
            data=payload,
            headers={"Referer": f"{self.settings.base_url}/"},
            timeout=30,
            allow_redirects=True,
        )
        self._raise_response_error(response, "ERP login submit")

        try:
            detail_payload = self._post_json(session, "/Account/GetStudentDetail", {})
        except AuthenticationRequired as exc:
            raise LoginFailed("Login failed. Check the captcha and credentials.") from exc

        student_detail = parse_student_detail_response(detail_payload)
        cookies_json = self._serialize_cookies(session)
        return LoginResult(
            cookies_json=cookies_json,
            reg_id=str(student_detail.get("RegID") or "") if student_detail else None,
            student_name=str(student_detail.get("StudentName") or "").strip() if student_detail else None,
        )

    def ensure_authenticated(self, student: Student) -> requests.Session:
        if not student.session_cookies:
            raise AuthenticationRequired("ERP session is missing. Open the dashboard and complete login.")
        session = self._session_from_cookies(student.session_cookies)
        self._post_json(session, "/Account/GetStudentDetail", {})
        return session

    def get_student_detail(self, student: Student) -> dict[str, Any]:
        session = self.ensure_authenticated(student)
        return self._post_json(session, "/Account/GetStudentDetail", {})

    def get_timetable(self, student: Student) -> dict[str, Any]:
        session = self.ensure_authenticated(student)
        reg_id = self._require_reg_id(student, session)
        return self._post_json(session, "/Web_StudentAcademic/FillStudentTimeTable", {"RegID": reg_id})

    def get_substitutions(self, student: Student, chk: int = 1) -> dict[str, Any]:
        session = self.ensure_authenticated(student)
        return self._post_json(session, "/Account/GetAllSubstitute", {"chk": chk})

    def get_attendance_summary(self, student: Student) -> dict[str, Any]:
        session = self.ensure_authenticated(student)
        reg_id = self._require_reg_id(student, session)
        return self._post_json(
            session,
            "/Web_StudentAcademic/GetSubjectDetailStudentAcademicFromLive",
            {"RegID": reg_id},
        )

    def _require_reg_id(self, student: Student, session: requests.Session) -> str:
        if student.reg_id:
            return student.reg_id
        detail_payload = self._post_json(session, "/Account/GetStudentDetail", {})
        detail = parse_student_detail_response(detail_payload)
        reg_id = str(detail.get("RegID") or "").strip() if detail else ""
        if not reg_id:
            raise ERPClientError("Could not determine the ERP registration id for this student.")
        return reg_id

    def _post_json(self, session: requests.Session, path: str, data: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            session,
            "post",
            f"{self.settings.base_url}{path}",
            context=path,
            data=data,
            headers={
                "Referer": f"{self.settings.base_url}/",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )

        if self._looks_like_login_page(response.text):
            raise AuthenticationRequired("ERP session expired.")
        self._raise_response_error(response, path, allow_auth_failure=True)

        try:
            return response.json()
        except ValueError as exc:
            raise ERPClientError(f"ERP returned a non-JSON response for {path}.") from exc

    def _request(
        self,
        session: requests.Session,
        method: str,
        url: str,
        *,
        context: str,
        **kwargs,
    ) -> requests.Response:
        try:
            return session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise ERPClientError(f"{context} request failed: {exc}") from exc

    def _raise_response_error(
        self,
        response: requests.Response,
        context: str,
        *,
        allow_auth_failure: bool = False,
    ) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            if allow_auth_failure and response.status_code in {401, 403}:
                raise AuthenticationRequired("ERP session expired.") from exc
            raise ERPClientError(
                f"{context} request failed with status {response.status_code}."
            ) from exc

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        return session

    def _session_from_cookies(self, cookies_json: str) -> requests.Session:
        session = self._new_session()
        if not cookies_json:
            return session

        cookies = json.loads(cookies_json)
        for item in cookies:
            session.cookies.set(
                item["name"],
                item["value"],
                domain=item.get("domain"),
                path=item.get("path", "/"),
            )
        return session

    def _serialize_cookies(self, session: requests.Session) -> str:
        cookies = []
        for cookie in session.cookies:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                }
            )
        return json.dumps(cookies)

    def _looks_like_login_page(self, text: str) -> bool:
        lowered = text.lower()
        return 'id="username"' in lowered and 'id="captcha"' in lowered
