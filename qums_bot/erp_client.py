from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

try:
    from PIL import Image, ImageEnhance, ImageFilter
except ImportError:
    Image = None
    ImageEnhance = None
    ImageFilter = None

logger = logging.getLogger(__name__)

try:
    import ddddocr as _ddddocr_module
except ImportError:  # graceful fallback if not installed
    _ddddocr_module = None

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
    _retryable_request_errors = (requests.Timeout, requests.ConnectionError)

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._ocr_instance = None

    @property
    def _ocr(self):
        """Lazy-load the ddddocr OCR engine on first use."""
        if self._ocr_instance is None:
            if _ddddocr_module is None:
                raise ERPClientError(
                    "ddddocr is not installed. Run: pip install ddddocr"
                )
            self._ocr_instance = _ddddocr_module.DdddOcr(show_ad=False)
        return self._ocr_instance

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

    # ── Confusable character mapping for QUMS captchas ──────────────────
    # QUMS captchas use UPPERCASE letters + digits only.  ddddocr commonly
    # confuses visually similar glyphs.  We fix those after OCR.
    _CONFUSABLE_TO_DIGIT: dict[str, str] = {
        "O": "0", "o": "0", "Q": "0",
        "I": "1", "l": "1", "i": "1", "|": "1",
        "Z": "2", "z": "2",
        "S": "5", "s": "5",
        "G": "6", "g": "6",
        "T": "7",
        "B": "8", "b": "8",
    }
    _CONFUSABLE_TO_ALPHA: dict[str, str] = {
        "0": "O",
        "1": "I",
        "2": "Z",
        "5": "S",
        "6": "G",
        "8": "B",
    }

    def solve_captcha(self, captcha_data_url: str) -> str:
        """Decode a base64 captcha data-URL and return the OCR-recognised text.

        Uses a multi-pass image preprocessing pipeline with **voting-based
        consensus** across many variants for maximum accuracy:

        1.  Raw image (baseline)
        2.  Grayscale + contrast enhanced + sharpened
        3.  Grayscale + hard binarization (threshold 128)
        4.  Grayscale + aggressive binarization (threshold 100)
        5.  OpenCV adaptive threshold (Gaussian)
        6.  OpenCV adaptive threshold (Mean)
        7.  CLAHE + Otsu threshold
        8.  Morphological opening to remove noise
        9.  Color-circle removal via HSV masking  ← key for QUMS
        10. Inverted + contrast boosted
        11. Dilated text for thicker strokes
        12. Scaled (2×) for better OCR on small images

        **QUMS captchas are always UPPERCASE + digits**, so all results are
        forced to uppercase and confusable characters are corrected.
        """
        image_bytes = self._decode_captcha_data_url(captcha_data_url)

        variants = self._preprocess_captcha_variants(image_bytes)
        candidates: list[tuple[str, str]] = []  # (label, text)

        for label, preprocessed in variants:
            try:
                result = self._ocr.classification(preprocessed)
            except Exception:
                continue
            cleaned = self._normalise_ocr_result(result)
            if not cleaned:
                continue
            candidates.append((label, cleaned))
            logger.debug("Captcha OCR [%s]: '%s'", label, cleaned)

        if not candidates:
            logger.warning("Captcha OCR: all variants returned empty results")
            return ""

        # ── Voting: pick the most common *valid* result ───────────────
        valid: list[str] = [
            text for _, text in candidates if self._is_valid_captcha_text(text)
        ]
        if valid:
            winner = max(set(valid), key=valid.count)
            logger.info(
                "Captcha OCR consensus (%d/%d votes): '%s'",
                valid.count(winner),
                len(valid),
                winner,
            )
            return winner

        # ── Fallback: try fixing confusables on all candidates ────────
        fixed_valid: list[str] = []
        for _, text in candidates:
            for fixed in self._generate_confusable_alternatives(text):
                if self._is_valid_captcha_text(fixed):
                    fixed_valid.append(fixed)
        if fixed_valid:
            winner = max(set(fixed_valid), key=fixed_valid.count)
            logger.info(
                "Captcha OCR confusable-fix consensus (%d votes): '%s'",
                fixed_valid.count(winner),
                winner,
            )
            return winner

        # ── Last resort: return the best raw candidate (longest) ──────
        best = max((text for _, text in candidates), key=len)
        logger.debug("Captcha OCR fallback: returning best candidate '%s'", best)
        return best

    def _normalise_ocr_result(self, raw: str | None) -> str:
        """Force uppercase and strip non-alphanumeric noise.

        QUMS captchas are always uppercase letters + digits, but ddddocr
        frequently returns lowercase.  This single step fixes the majority
        of misrecognitions.
        """
        if not raw:
            return ""
        # Remove any non-alphanumeric characters (spaces, punctuation, etc.)
        cleaned = re.sub(r'[^A-Za-z0-9]', '', raw.strip())
        return cleaned.upper()

    @staticmethod
    def _decode_captcha_data_url(captcha_data_url: str) -> bytes:
        """Extract raw image bytes from a data-URL or raw base64 string."""
        prefix = "data:image/png;base64,"
        if captcha_data_url.startswith(prefix):
            return base64.b64decode(captcha_data_url[len(prefix):])
        if captcha_data_url.startswith("data:image"):
            _, _, b64_part = captcha_data_url.partition(",")
            return base64.b64decode(b64_part)
        return base64.b64decode(captcha_data_url)

    @staticmethod
    def _is_valid_captcha_text(text: str) -> bool:
        """QUMS captchas are 4-6 uppercase alphanumeric characters."""
        return bool(re.match(r'^[A-Z0-9]{4,6}$', text))

    def _generate_confusable_alternatives(self, text: str) -> list[str]:
        """Generate plausible alternatives by swapping confusable characters.

        For instance, if OCR returned '0L9DD9' we try 'OL9DD9' as well.
        Only generates alternatives that pass _is_valid_captcha_text.
        """
        if not text or len(text) > 8:
            return [text]
        # We only fix characters that are commonly confused
        alternatives: set[str] = {text}
        for i, ch in enumerate(text):
            replacements: list[str] = []
            if ch in self._CONFUSABLE_TO_DIGIT:
                replacements.append(self._CONFUSABLE_TO_DIGIT[ch])
            if ch in self._CONFUSABLE_TO_ALPHA:
                replacements.append(self._CONFUSABLE_TO_ALPHA[ch])
            for repl in replacements:
                alt = text[:i] + repl + text[i + 1:]
                alternatives.add(alt)
        return [alt for alt in alternatives if self._is_valid_captcha_text(alt)]

    @staticmethod
    def _preprocess_captcha_variants(image_bytes: bytes) -> list[tuple[str, bytes]]:
        """Return many preprocessed versions of the captcha image.

        The QUMS captcha has semi-transparent coloured overlapping circles
        with dark alphanumeric text on top.  The key insight is to remove
        the coloured circles via HSV saturation masking while preserving
        the dark text.
        """
        variants: list[tuple[str, bytes]] = [("raw", image_bytes)]

        # ── PIL-based preprocessing ───────────────────────────────────
        if Image is not None:
            try:
                img_pil = Image.open(io.BytesIO(image_bytes)).convert("L")
                width, height = img_pil.size

                # V2: contrast enhanced + sharpened
                enhanced = ImageEnhance.Contrast(img_pil).enhance(2.5)
                enhanced = enhanced.filter(ImageFilter.SHARPEN)
                buf = io.BytesIO()
                enhanced.save(buf, format="PNG")
                variants.append(("pil_contrast_sharp", buf.getvalue()))

                # V3: hard binarization threshold=128
                binarized = img_pil.point(lambda x: 255 if x > 128 else 0, "1")
                buf2 = io.BytesIO()
                binarized.save(buf2, format="PNG")
                variants.append(("pil_binarized_128", buf2.getvalue()))

                # V4: aggressive binarization threshold=100
                binarized_low = img_pil.point(lambda x: 255 if x > 100 else 0, "1")
                buf3 = io.BytesIO()
                binarized_low.save(buf3, format="PNG")
                variants.append(("pil_binarized_100", buf3.getvalue()))

                # V5: extreme contrast + binarize
                extreme = ImageEnhance.Contrast(img_pil).enhance(5.0)
                extreme = extreme.point(lambda x: 255 if x > 140 else 0, "1")
                buf4 = io.BytesIO()
                extreme.save(buf4, format="PNG")
                variants.append(("pil_extreme_contrast", buf4.getvalue()))

                # V6: 2× upscale for small captchas
                if width < 300:
                    upscaled = img_pil.resize((width * 2, height * 2), Image.LANCZOS)
                    up_enhanced = ImageEnhance.Contrast(upscaled).enhance(2.5)
                    up_enhanced = up_enhanced.filter(ImageFilter.SHARPEN)
                    buf5 = io.BytesIO()
                    up_enhanced.save(buf5, format="PNG")
                    variants.append(("pil_upscaled_2x", buf5.getvalue()))

                # V7: inverted
                from PIL import ImageOps
                inverted = ImageOps.invert(img_pil)
                inv_enhanced = ImageEnhance.Contrast(inverted).enhance(2.0)
                buf6 = io.BytesIO()
                inv_enhanced.save(buf6, format="PNG")
                variants.append(("pil_inverted", buf6.getvalue()))

            except Exception:
                pass

        # ── OpenCV-based preprocessing ────────────────────────────────
        if cv2 is not None and np is not None:
            try:
                nparr = np.frombuffer(image_bytes, np.uint8)
                img_cv_gray = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
                img_cv_color = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

                if img_cv_gray is not None:
                    h, w = img_cv_gray.shape[:2]

                    # V8: median blur + Gaussian adaptive threshold
                    blurred = cv2.medianBlur(img_cv_gray, 3)
                    thresh_gauss = cv2.adaptiveThreshold(
                        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                        cv2.THRESH_BINARY, 11, 2,
                    )
                    _, buf_cv1 = cv2.imencode(".png", thresh_gauss)
                    variants.append(("cv2_adaptive_gauss", buf_cv1.tobytes()))

                    # V9: median blur + Mean adaptive threshold
                    thresh_mean = cv2.adaptiveThreshold(
                        blurred, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                        cv2.THRESH_BINARY, 15, 5,
                    )
                    _, buf_cv2 = cv2.imencode(".png", thresh_mean)
                    variants.append(("cv2_adaptive_mean", buf_cv2.tobytes()))

                    # V10: CLAHE + Otsu
                    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
                    cl_img = clahe.apply(img_cv_gray)
                    _, otsu = cv2.threshold(
                        cl_img, 0, 255,
                        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
                    )
                    _, buf_cv3 = cv2.imencode(".png", otsu)
                    variants.append(("cv2_clahe_otsu", buf_cv3.tobytes()))

                    # V11: morphological opening to remove circle-edge noise
                    kernel = np.ones((2, 2), np.uint8)
                    opened = cv2.morphologyEx(otsu, cv2.MORPH_OPEN, kernel)
                    _, buf_cv4 = cv2.imencode(".png", opened)
                    variants.append(("cv2_morph_open", buf_cv4.tobytes()))

                    # V12: dilate text for thicker strokes
                    dilate_kernel = np.ones((2, 2), np.uint8)
                    dilated = cv2.dilate(
                        cv2.bitwise_not(otsu), dilate_kernel, iterations=1,
                    )
                    dilated = cv2.bitwise_not(dilated)
                    _, buf_cv5 = cv2.imencode(".png", dilated)
                    variants.append(("cv2_dilated", buf_cv5.tobytes()))

                # ── V13-V15: Color-circle removal (KEY for QUMS) ─────
                if img_cv_color is not None:
                    hsv = cv2.cvtColor(img_cv_color, cv2.COLOR_BGR2HSV)
                    gray_from_color = cv2.cvtColor(
                        img_cv_color, cv2.COLOR_BGR2GRAY,
                    )

                    # Mask: pixels with noticeable colour saturation
                    # are background circles — set them to white
                    sat_mask = hsv[:, :, 1] > 30
                    cleaned = gray_from_color.copy()
                    cleaned[sat_mask] = 255

                    # V13: circle-removed + Otsu
                    _, cr_otsu = cv2.threshold(
                        cleaned, 0, 255,
                        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
                    )
                    _, buf_cr1 = cv2.imencode(".png", cr_otsu)
                    variants.append(("cv2_circle_removed_otsu", buf_cr1.tobytes()))

                    # V14: circle-removed + fixed threshold
                    _, cr_fixed = cv2.threshold(cleaned, 100, 255, cv2.THRESH_BINARY)
                    _, buf_cr2 = cv2.imencode(".png", cr_fixed)
                    variants.append(("cv2_circle_removed_fixed", buf_cr2.tobytes()))

                    # V15: circle-removed + contrast + sharp
                    cr_clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
                    cr_enhanced = cr_clahe.apply(cleaned)
                    _, cr_final = cv2.threshold(
                        cr_enhanced, 0, 255,
                        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
                    )
                    _, buf_cr3 = cv2.imencode(".png", cr_final)
                    variants.append(("cv2_circle_removed_clahe", buf_cr3.tobytes()))

                    # V16: aggressive circle removal (lower saturation threshold)
                    sat_mask_agg = hsv[:, :, 1] > 15
                    cleaned_agg = gray_from_color.copy()
                    cleaned_agg[sat_mask_agg] = 255
                    _, cr_agg = cv2.threshold(
                        cleaned_agg, 0, 255,
                        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
                    )
                    kernel_clean = np.ones((2, 2), np.uint8)
                    cr_agg = cv2.morphologyEx(cr_agg, cv2.MORPH_CLOSE, kernel_clean)
                    _, buf_cr4 = cv2.imencode(".png", cr_agg)
                    variants.append(("cv2_circle_removed_aggressive", buf_cr4.tobytes()))

                    # V17: upscaled circle-removed (if small)
                    if w < 300:
                        scale = 2
                        cr_up = cv2.resize(
                            cr_otsu,
                            (w * scale, h * scale),
                            interpolation=cv2.INTER_CUBIC,
                        )
                        _, buf_cr5 = cv2.imencode(".png", cr_up)
                        variants.append(("cv2_circle_removed_upscaled", buf_cr5.tobytes()))

            except Exception:
                pass

        return variants


    def auto_login(self, student: Student, *, max_attempts: int = 5) -> LoginResult:
        """Fully automatic login: fetch captcha → OCR solve → submit.

        Retries up to *max_attempts* times (default 5), refreshing the
        captcha image on each failed attempt.  The multi-variant OCR
        pipeline with voting consensus makes each individual attempt
        highly reliable, and multiple retries cover the remaining edge
        cases.
        """
        pending = self.start_manual_login(student)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            captcha_text = self.solve_captcha(pending.captcha_data_url)
            logger.info(
                "Auto-login attempt %d/%d for student %s — OCR result: '%s'",
                attempt,
                max_attempts,
                student.id,
                captcha_text,
            )
            if not captcha_text:
                # OCR returned nothing — refresh and retry
                if attempt < max_attempts:
                    pending = self.refresh_captcha(pending)
                    continue
                raise ERPClientError(
                    f"Auto-captcha OCR returned empty text after {max_attempts} attempts."
                )
            try:
                return self.complete_manual_login(student, pending, captcha_text)
            except LoginFailed as exc:
                last_error = exc
                logger.info(
                    "Auto-login attempt %d failed (captcha '%s' rejected): %s",
                    attempt,
                    captcha_text,
                    exc,
                )
                if attempt < max_attempts:
                    pending = self.refresh_captcha(pending)
                    continue

        assert last_error is not None
        raise ERPClientError(
            f"Auto-captcha login failed after {max_attempts} attempts: {last_error}"
        ) from last_error

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
        return self._session_from_cookies(student.session_cookies)

    def validate_session(self, student: Student) -> None:
        """Explicitly validate the ERP session is alive. Use only when a
        proactive liveness check is needed (e.g. session monitor).
        Regular data-fetching methods detect expiry lazily via _post_json.
        """
        session = self.ensure_authenticated(student)
        self._post_json(session, "/Account/GetStudentDetail", {})

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

    def get_internal_marks(self, student: Student, year_sem: str | None = None) -> dict[str, Any]:
        """Fetch internal marks from the ERP exam module."""
        session = self.ensure_authenticated(student)
        data: dict[str, Any] = {}
        if year_sem:
            data["YearSem"] = year_sem
        return self._post_json(session, "/Web_Exam/GetStudentInternalMarks", data)

    def get_assignments(self, student: Student) -> dict[str, Any]:
        """Fetch assignment list from the ERP academic module."""
        session = self.ensure_authenticated(student)
        reg_id = self._require_reg_id(student, session)
        return self._post_json(session, "/Web_StudentAcademic/GetStudentAssignment", {"RegID": reg_id})

    def get_fee_receipts(self, student: Student) -> dict[str, Any]:
        """Fetch fee receipt list from the ERP finance module."""
        session = self.ensure_authenticated(student)
        reg_id = self._require_reg_id(student, session)
        return self._post_json(session, "/Web_StudentFinance/GetStudentFeeReceipt", {"RegID": reg_id})

    def get_exam_list(self, student: Student) -> dict[str, Any]:
        """Fetch online exam list from the ERP exam module."""
        session = self.ensure_authenticated(student)
        reg_id = self._require_reg_id(student, session)
        return self._post_json(session, "/OnlineExam/GetExamList", {"RegID": reg_id})

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
        request_kwargs = dict(kwargs)
        request_kwargs["timeout"] = self._normalize_timeout(request_kwargs.get("timeout", 30))
        last_error: requests.RequestException | None = None
        for attempt in range(1, 4):
            try:
                return session.request(method, url, **request_kwargs)
            except self._retryable_request_errors as exc:
                last_error = exc
                if attempt >= 3:
                    break
                time.sleep(float(attempt))
            except requests.RequestException as exc:
                raise ERPClientError(f"{context} request failed: {exc}") from exc
        assert last_error is not None
        raise ERPClientError(f"{context} request failed after 3 attempts: {last_error}") from last_error

    def _normalize_timeout(
        self,
        timeout: float | tuple[float, float] | tuple[int, int] | int,
    ) -> tuple[float, float] | tuple[int, int] | int:
        if isinstance(timeout, tuple):
            return timeout
        timeout_value = float(timeout)
        return (min(timeout_value, 10.0), timeout_value)

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
