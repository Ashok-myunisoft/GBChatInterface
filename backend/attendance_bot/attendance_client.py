import base64
import copy
import calendar
import gzip
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

import requests

from config import settings
from utils.date_parser import parse_date


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


session = requests.Session()
session.headers.update({
    "User-Agent": "AttendanceClient/1.0",
    "Accept": "application/json"
})


try:
    with open("criteria.json", "r", encoding="utf-8") as f:
        CRITERIA = json.load(f)

    ATTENDANCE_PAY_PERIOD_CRITERIA = CRITERIA["ATTENDANCE_PAY_PERIOD_CRITERIA"]
    ATTENDANCE_DAILY_CRITERIA = CRITERIA["ATTENDANCE_DAILY_CRITERIA"]
    logger.info("✓ Criteria loaded successfully from criteria.json")
except FileNotFoundError:
    logger.error("❌ criteria.json file not found! Attendance functionality will be limited.")
    ATTENDANCE_PAY_PERIOD_CRITERIA = {}
    ATTENDANCE_DAILY_CRITERIA = {}
except KeyError as e:
    logger.error(f"❌ Missing key in criteria.json: {e}. Attendance functionality will be limited.")
    ATTENDANCE_PAY_PERIOD_CRITERIA = {}
    ATTENDANCE_DAILY_CRITERIA = {}
except json.JSONDecodeError as e:
    logger.error(f"❌ Invalid JSON in criteria.json: {e}. Attendance functionality will be limited.")
    ATTENDANCE_PAY_PERIOD_CRITERIA = {}
    ATTENDANCE_DAILY_CRITERIA = {}


def direct_url(path: str, login: Dict[str, Any] = None) -> str:
    base = settings.get_direct_url(login)
    path = path.lstrip("/")
    return f"{base}/{path}"


def _first_non_empty(record: Dict[str, Any], *keys, default=""):
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return default


def _to_gb_date(value: Any) -> str:
    """
    Convert a date-like value into GoodBooks /Date(ms)/ format.
    Returns an empty string if the value cannot be parsed.
    """
    if value in (None, ""):
        return ""

    text = str(value).strip()
    if not text:
        return ""

    if re.match(r"^/Date\(\d+\)/$", text):
        return text

    match = re.search(r"\d+", text)
    if text.startswith("/Date(") and match:
        return f"/Date({match.group(0)})/"

    for fmt in (
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d-%m-%YT%H:%M:%S",
        "%d/%m/%YT%H:%M:%S",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            return f"/Date({int(dt.timestamp() * 1000)})/"
        except ValueError:
            continue

    return ""


def _to_ddmmyyyy(value: Any) -> str:
    """
    Convert a date-like value into DD-MM-YYYY format.
    Returns an empty string if the value cannot be parsed.
    """
    if value in (None, ""):
        return ""

    text = str(value).strip()
    if not text:
        return ""

    parsed = parse_date(text)
    if parsed:
        return parsed

    if re.match(r"^\d{1,2}[-/]\d{1,2}[-/]\d{4}$", text):
        parts = re.split(r"[-/]", text)
        return f"{int(parts[0]):02d}-{int(parts[1]):02d}-{parts[2]}"

    return ""


def _to_epoch_seconds(value: Any) -> str:
    """
    Convert a date-like value into epoch seconds as a string.
    This matches the format used by other GB summary APIs that accept
    PeriodFromDate / PeriodToDate style criteria.
    """
    if value in (None, ""):
        return ""

    text = str(value).strip()
    if not text:
        return ""

    # /Date(1714521600000)/
    match = re.search(r"/Date\((\d+)\)/", text)
    if match:
        return str(int(match.group(1)) // 1000)

    # Raw epoch seconds or milliseconds
    if text.isdigit():
        num = int(text)
        return str(num // 1000) if num > 9_999_999_999 else str(num)

    # Try the normalized DD-MM-YYYY form first
    parsed = parse_date(text)
    if parsed:
        try:
            dt = datetime.strptime(parsed, "%d-%m-%Y")
            return str(calendar.timegm(dt.timetuple()))
        except ValueError:
            pass

    # Try common direct formats
    for fmt in (
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            return str(calendar.timegm(dt.timetuple()))
        except ValueError:
            continue

    return ""


def clean_section_criteria(section_list: List[Dict]) -> List[Dict]:
    cleaned = []
    for section in section_list:
        attrs = []
        for attribute in section.get("AttributesCriteriaList", []):
            field_value = attribute.get("FieldValue")
            if field_value is None:
                continue
            if isinstance(field_value, str) and field_value.strip() == "":
                continue
            attrs.append(attribute)

        cleaned_section = dict(section)
        cleaned_section["AttributesCriteriaList"] = attrs
        cleaned.append(cleaned_section)
    return cleaned


def decode_response_body(body_base64: str) -> Dict[str, Any]:
    try:
        compressed = base64.b64decode(body_base64)
        decompressed = gzip.decompress(compressed)
        decoded_str = decompressed.decode("utf-8")
        return json.loads(decoded_str)
    except Exception as e:
        logger.error(f"Decode error: {e}")
        return {}


def parse_api_response(response_data: Dict[str, Any]) -> Any:
    status = response_data.get("Status")
    if status and status != 200:
        logger.warning(f"⚠️ API returned non-200 status: {status}")

    def check_decoded_error(decoded):
        if "ErrorNumber" in decoded:
            error_msg = decoded.get("Body", "Unknown error")
            logger.error(f"❌ API Error: {error_msg}")
            return True
        return False

    contents = response_data.get("contents", {})
    if "Body" in contents:
        body = contents["Body"]
        if isinstance(body, str) and body:
            decoded = decode_response_body(body)
            if check_decoded_error(decoded):
                return []
            if "Body" in decoded:
                inner = decoded["Body"]
                return json.loads(inner) if isinstance(inner, str) else inner
            if "ResponseObject" in decoded:
                return decoded["ResponseObject"]

    if "Body" in response_data:
        body = response_data["Body"]
        if isinstance(body, str) and body:
            decoded = decode_response_body(body)
            if check_decoded_error(decoded):
                return []
            if "Body" in decoded:
                inner = decoded["Body"]
                return json.loads(inner) if isinstance(inner, str) else inner
            if "ResponseObject" in decoded:
                return decoded["ResponseObject"]

    if "ResponseObject" in response_data:
        return response_data["ResponseObject"]
    if "Data" in response_data:
        return response_data["Data"]

    return []


def _resolve_placeholders(value, login: Dict[str, Any], selected_payperiod: Dict[str, Any] | None = None):
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v, login, selected_payperiod) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(v, login, selected_payperiod) for v in value]
    if not isinstance(value, str):
        return value

    if value == "{{login_dto.userId}}":
        return login.get("UserId")
    if value == "{{selected_payperiod.fromDate}}":
        return (selected_payperiod or {}).get("fromDate")
    if value == "{{selected_payperiod.toDate}}":
        return (selected_payperiod or {}).get("toDate")
    return value


def _prepare_payload(template: Dict[str, Any], login: Dict[str, Any], selected_payperiod: Dict[str, Any] | None = None):
    payload = copy.deepcopy(template)
    payload = _resolve_placeholders(payload, login, selected_payperiod)
    if "SectionCriteriaList" in payload:
        payload["SectionCriteriaList"] = clean_section_criteria(payload["SectionCriteriaList"])
    return payload


def get_pay_periods(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    logger.info("📋 Fetching pay periods from API...")
    try:
        payload = ATTENDANCE_PAY_PERIOD_CRITERIA
        if not payload:
            logger.error("❌ ATTENDANCE_PAY_PERIOD_CRITERIA not found in criteria.json")
            return []

        prepared = _prepare_payload(payload, login)
        # Use a normalized path so the endpoint works even if the server does not
        # tolerate the double-slash variant.
        url = direct_url("/prs/PayPeriod.svc/SelectList/PayPeriod/ApplicableOu/?FirstNumber=1&MaxResult=50", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        response = session.post(url, json=prepared, headers=headers, timeout=60)
        response.raise_for_status()

        result = parse_api_response(response.json())
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.error(f"❌ Failed to fetch pay periods: {e}")
        return []


def normalize_payperiod_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a pay period record into the small shape the chat flow needs.
    This is intentionally permissive so field name changes from the API do not
    break the existing flow.
    """
    if not isinstance(record, dict):
        return {"id": "", "name": "", "fromDate": "", "toDate": "", "record": {}}

    from_date_raw = _first_non_empty(
        record,
        "FromDate",
        "PeriodFromDate",
        "PeriodFrom",
        "StartDate",
        "Start",
        "From",
        default=""
    )
    to_date_raw = _first_non_empty(
        record,
        "ToDate",
        "PeriodToDate",
        "PeriodTo",
        "EndDate",
        "End",
        "To",
        default=""
    )

    return {
        "id": _first_non_empty(record, "Id", "PayPeriodId", "PeriodId", "Period.Id", default=""),
        "name": _first_non_empty(
            record,
            "Name",
            "PayPeriodName",
            "DisplayName",
            "displayname",
            "PeriodName",
            "PeriodDisplayName",
            "Period.DisplayName",
            default=""
        ),
        "fromDate": _to_gb_date(from_date_raw) or from_date_raw,
        "toDate": _to_gb_date(to_date_raw) or to_date_raw,
        "fromDateRaw": from_date_raw,
        "toDateRaw": to_date_raw,
        "record": record,
    }


def _build_attendance_date_candidates(selected_payperiod: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Build a small set of date representations to try against the attendance API.
    This is intentionally conservative and only uses values already present in the
    selected pay period record.
    """
    candidates = []

    from_candidates = [
        _to_epoch_seconds(selected_payperiod.get("fromDateRaw")),
        _to_epoch_seconds(selected_payperiod.get("fromDate")),
        selected_payperiod.get("fromDateRaw"),
        selected_payperiod.get("fromDate"),
        _to_ddmmyyyy(selected_payperiod.get("fromDateRaw")),
        _to_ddmmyyyy(selected_payperiod.get("fromDate")),
    ]
    to_candidates = [
        _to_epoch_seconds(selected_payperiod.get("toDateRaw")),
        _to_epoch_seconds(selected_payperiod.get("toDate")),
        selected_payperiod.get("toDateRaw"),
        selected_payperiod.get("toDate"),
        _to_ddmmyyyy(selected_payperiod.get("toDateRaw")),
        _to_ddmmyyyy(selected_payperiod.get("toDate")),
    ]

    seen = set()
    for from_value in from_candidates:
        if not from_value:
            continue
        for to_value in to_candidates:
            if not to_value:
                continue
            key = (str(from_value).strip(), str(to_value).strip())
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "fromDate": str(from_value).strip(),
                "toDate": str(to_value).strip(),
            })

    return candidates


def get_daily_attendance(login: Dict[str, Any], selected_payperiod: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("📋 Fetching daily attendance from API...")
    try:
        payload = ATTENDANCE_DAILY_CRITERIA
        if not payload:
            raise Exception("ATTENDANCE_DAILY_CRITERIA not found in criteria.json")

        employee_id = login.get("UserId")
        url = direct_url(f"/prs/DailyAttendance.svc/DailyAttendanceEmployeeDetail/?EmployeeId={employee_id}", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        candidates = _build_attendance_date_candidates(selected_payperiod)
        last_error = None
        logger.info(f"📋 Attendance date candidates: {candidates}")

        for date_pair in candidates:
            prepared = _prepare_payload(payload, login, date_pair)
            logger.info("📤 Attendance request payload: %s", json.dumps(prepared, indent=2, ensure_ascii=False, default=str))
            response = session.post(url, json=prepared, headers=headers, timeout=60)
            logger.info("📥 Attendance response status: %s", response.status_code)

            if response.status_code == 200:
                response_json = response.json()
                logger.info("📥 Attendance response JSON: %s", json.dumps(response_json, indent=2, ensure_ascii=False, default=str))
                parsed_body = parse_api_response(response_json)
                return {
                    "body": parsed_body if parsed_body not in (None, "", [], {}) else response_json,
                    "raw_body": response_json,
                    "used_dates": date_pair,
                    "status_code": response.status_code
                }

            try:
                response_json = response.json()
            except Exception:
                response_json = {}

            parsed_error = parse_api_response(response_json) if isinstance(response_json, dict) else []
            response_text = response.text if response.text else ""
            logger.warning("⚠️ Attendance response text: %s", response_text)
            logger.warning("⚠️ Attendance response JSON: %s", json.dumps(response_json, indent=2, ensure_ascii=False, default=str) if response_json else "{}")
            last_error = {
                "status_code": response.status_code,
                "body": response_json if response_json else response.text,
                "response_text": response_text,
                "used_dates": date_pair,
                "parsed_error": parsed_error,
                "request_payload": prepared,
            }

            error_text = json.dumps(response_json, default=str) if response_json else response.text
            if "Input string was not in a correct format" not in error_text:
                break

        if last_error:
            return last_error
        raise Exception("Daily attendance request failed without a usable response.")
    except Exception as e:
        logger.error(f"❌ Failed to fetch daily attendance: {e}")
        raise
