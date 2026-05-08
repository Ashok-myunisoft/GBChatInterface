import base64
import copy
import gzip
import json
import logging
from typing import Any, Dict, List

import requests

from config import settings


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
        "fromDate": _first_non_empty(
            record,
            "FromDate",
            "PeriodFromDate",
            "PeriodFrom",
            "StartDate",
            "Start",
            "From",
            default=""
        ),
        "toDate": _first_non_empty(
            record,
            "ToDate",
            "PeriodToDate",
            "PeriodTo",
            "EndDate",
            "End",
            "To",
            default=""
        ),
        "record": record,
    }


def get_daily_attendance(login: Dict[str, Any], selected_payperiod: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("📋 Fetching daily attendance from API...")
    try:
        payload = ATTENDANCE_DAILY_CRITERIA
        if not payload:
            raise Exception("ATTENDANCE_DAILY_CRITERIA not found in criteria.json")

        prepared = _prepare_payload(payload, login, selected_payperiod)
        employee_id = login.get("UserId")
        url = direct_url(f"/prs/DailyAttendance.svc/DailyAttendanceEmployeeDetail/?EmployeeId={employee_id}", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        response = session.post(url, json=prepared, headers=headers, timeout=60)
        response.raise_for_status()
        response_json = response.json()
        return {
            "body": response_json,
            "status_code": response.status_code
        }
    except Exception as e:
        logger.error(f"❌ Failed to fetch daily attendance: {e}")
        raise
