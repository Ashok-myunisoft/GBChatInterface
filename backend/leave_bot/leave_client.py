"""
Complete Leave Client for GB API
Handles leave types, reasons, and leave application.
Fixed: Payload structure strictly matches the provided JSON sample (TLeaveStatus, TLeaveVersion, etc.).
"""

import json
import re
import gzip
import base64
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional
import logging

from config import settings
from utils import map_leave_type_id, map_leave_reason


# ============================================================
# Logging Configuration
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# Persistent Session
# ============================================================
session = requests.Session()
session.headers.update({
    'User-Agent': 'LeaveClient/1.0',
    'Accept': 'application/json'
})


# ============================================================
# Load Criteria from JSON
# ============================================================
try:
    with open("criteria.json", "r", encoding="utf-8") as f:
        CRITERIA = json.load(f)
    
    LEAVE_TYPE_CRITERIA = CRITERIA["LEAVE_TYPE_CRITERIA"]
    LEAVE_REASON_CRITERIA = CRITERIA["LEAVE_REASON_CRITERIA"]
    logger.info("✓ Criteria loaded successfully from criteria.json")
    
except FileNotFoundError:
    logger.error("❌ criteria.json file not found! Leave functionality will be limited.")
    LEAVE_TYPE_CRITERIA = {}
    LEAVE_REASON_CRITERIA = {}

except KeyError as e:
    logger.error(f"❌ Missing key in criteria.json: {e}. Leave functionality will be limited.")
    LEAVE_TYPE_CRITERIA = {}
    LEAVE_REASON_CRITERIA = {}

except json.JSONDecodeError as e:
    logger.error(f"❌ Invalid JSON in criteria.json: {e}. Leave functionality will be limited.")
    LEAVE_TYPE_CRITERIA = {}
    LEAVE_REASON_CRITERIA = {}


# ============================================================
# URL Helpers
# ============================================================
def direct_url(path: str, login: Dict[str, Any] = None) -> str:
    """
    Build direct API URL using login object's BaseUri if available.
    This ensures requests go to the correct server/database.
    """
    base = settings.get_direct_url(login)
    path = path.lstrip("/")
    url = f"{base}/{path}"
    logger.debug(f"Direct URL: {url}")
    return url



# ============================================================
# Clean Criteria (Remove Empty Fields)
# ============================================================
def clean_section_criteria(section_list: List[Dict]) -> List[Dict]:
    """Remove empty or null values from criteria to prevent API errors."""
    cleaned = []
    
    for section in section_list:
        attrs = []
        for attribute in section.get("AttributesCriteriaList", []):
            field_value = attribute.get("FieldValue")
            
            if field_value is None: continue
            if isinstance(field_value, str) and field_value.strip() == "": continue
            
            attrs.append(attribute)
        
        cleaned_section = dict(section)
        cleaned_section["AttributesCriteriaList"] = attrs
        cleaned.append(cleaned_section)
    
    return cleaned


# ============================================================
# Response Decoder
# ============================================================
def decode_response_body(body_base64: str) -> Dict[str, Any]:
    """Decode JSON response bodies, including optional base64 + gzip wrappers."""
    if not isinstance(body_base64, str):
        return {}

    # Some endpoints put JSON directly in Body instead of wrapping it.
    try:
        direct = json.loads(body_base64)
        if isinstance(direct, dict):
            return direct
        if isinstance(direct, list):
            return {"Body": json.dumps(direct)}
    except json.JSONDecodeError:
        pass

    try:
        encoded = base64.b64decode(body_base64, validate=True)
        # Gzip is optional; ADS responses can also be base64-encoded plain JSON.
        raw = gzip.decompress(encoded) if encoded.startswith(b"\x1f\x8b") else encoded
        decoded_text = raw.decode("utf-8-sig").strip()
        try:
            decoded = json.loads(decoded_text)
            if isinstance(decoded, dict):
                return decoded
            if isinstance(decoded, list):
                return {"Body": json.dumps(decoded)}
        except json.JSONDecodeError:
            return {"Body": decoded_text} if decoded_text else {}
    except (ValueError, OSError, UnicodeDecodeError) as e:
        logger.warning("Could not decode wrapped response body: %s", e)

    # Preserve an unwrapped non-JSON response for callers to report usefully.
    return {"Body": body_base64}


# ============================================================
# Response Parser
# ============================================================
def parse_api_response(response_data: Dict[str, Any]) -> Any:
    """Parse API response and extract data."""
    
    # Check for API-level status
    status = response_data.get("Status")
    if status and status != 200:
        logger.warning(f"⚠️ API returned non-200 status: {status}")

    def check_decoded_error(decoded):
        if "ErrorNumber" in decoded:
            error_msg = decoded.get('Body', 'Unknown error')
            logger.error(f"❌ API Error: {error_msg}")
            return True
        return False

    contents = response_data.get("contents", {})
    if "Body" in contents:
        body = contents["Body"]
        if isinstance(body, str) and body:
            try:
                decoded = json.loads(body)
            except json.JSONDecodeError:
                decoded = decode_response_body(body)
            if isinstance(decoded, list): return decoded
            if check_decoded_error(decoded): return []
            
            if "Body" in decoded:
                inner = decoded["Body"]
                return json.loads(inner) if isinstance(inner, str) else inner
            if "ResponseObject" in decoded:
                return decoded["ResponseObject"]

    if "Body" in response_data:
        body = response_data["Body"]
        if isinstance(body, str) and body:
            try:
                decoded = json.loads(body)
            except json.JSONDecodeError:
                decoded = decode_response_body(body)
            if isinstance(decoded, list): return decoded
            if check_decoded_error(decoded): return []

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


# ============================================================
# Get Leave Types
# ============================================================
def _run_configured_lookup(terms: tuple[str, ...], login: Dict[str, Any]):
    """Execute a lookup from the APPLY_LEAVE version's API dependency list."""
    from capability_engine.dependency_executor import execute_dependency
    dependency = _find_configured_dependency(terms)
    raw = execute_dependency(dependency, login=login, inputs={}, session=session,
                             url_builder=direct_url, timeout=settings.GB_API_TIMEOUT)
    return raw if isinstance(raw, list) else parse_api_response(raw)


def _find_configured_dependency(terms: tuple[str, ...]) -> Dict[str, Any]:
    from capability_engine.configuration_loader import load_capability
    config = load_capability("APPLY_LEAVE")
    for dependency in config["dependencies"]:
        if not isinstance(dependency, dict):
            continue
        identity = " ".join(str(value).casefold() for key, value in dependency.items()
                            if key.casefold() in {"name", "code", "id", "operation", "dependencyname",
                                                  "endpoint", "url", "path", "endpointurl", "apiendpoint", "apipath"})
        if any(term.casefold() in identity for term in terms):
            return dependency
    raise RuntimeError(f"No configured dependency found for {'/'.join(terms)}.")


def _dependency_setting(dependency: Dict[str, Any], *keys: str):
    lowered = {str(key).casefold(): value for key, value in dependency.items()}
    candidates = ("endpoint", "url", "path", "endpointurl", "apiendpoint", "apipath") if keys and keys[0].casefold() == "endpoint" else tuple(key.casefold() for key in keys)
    return next((lowered[key] for key in candidates if lowered.get(key) is not None), None)


def get_leave_types(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch Leave Type options through the configured capability dependency."""
    try:
        result = _run_configured_lookup(("leave type", "leavetype", "getselectlistmleave"), login)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.error("Error fetching configured Leave Type dependency: %s", e)
        return []

def get_leave_types_with_fallback(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    return get_leave_types(login)


# ============================================================
# Get Leave Reasons
# ============================================================
def get_leave_reasons(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch Leave Reason options through the configured capability dependency."""
    try:
        result = _run_configured_lookup(("leave reason", "leavereason", "getselectlistreason"), login)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.error("Error fetching configured Leave Reason dependency: %s", e)
        return []
# ============================================================
# Date Helpers
# ============================================================
def _parse_date(val: str) -> Optional[datetime]:
    if not val: return None
    match = re.search(r"\d{2}[-/]\d{2}[-/]\d{2,4}", val)
    if not match: return None
    clean = match.group(0).replace("/", "-")
    for fmt in ("%d-%m-%Y", "%d-%m-%y"):
        try: return datetime.strptime(clean, fmt)
        except ValueError: continue
    return None

def _gb_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _get_user_department_details(login: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch department fields for the current user from the live user service."""
    user_id = next(
        (login[key] for key in ("UserId", "userId") if login.get(key) not in (None, "")),
        None,
    )
    if user_id is None:
        raise ValueError("UserId not found in login DTO")

    dependency = _find_configured_dependency(("employee details", "getuser", "employee lookup"))
    method = str(_dependency_setting(dependency, "method", "httpmethod", "requestmethod") or "GET").upper()
    if method != "GET":
        raise ValueError("The configured employee details dependency must use GET.")
    endpoint = _dependency_setting(dependency, "endpoint", "url", "path", "endpointurl", "apiendpoint")
    if not endpoint:
        raise ValueError("The configured employee details dependency has no endpoint.")
    url = direct_url(endpoint, login)
    headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
    response = session.get(
        url,
        params={"UserId": user_id},
        json={},
        headers=headers,
        timeout=60,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        error_body = response.text[:2000]
        logger.error(
            "GetUser failed: status=%s, UserId=%s, response_body=%s",
            response.status_code, user_id, error_body,
        )
        raise RuntimeError(
            f"GetUser failed with HTTP {response.status_code}: {error_body or 'empty response body'}"
        ) from exc
    response_data = response.json()

    body = response_data.get("Body") if isinstance(response_data, dict) else response_data
    if isinstance(body, str):
        decoded = decode_response_body(body)
        body = decoded.get("Body", decoded.get("ResponseObject", decoded))
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                pass

    if isinstance(body, list):
        body = body[0] if body else {}
    if not isinstance(body, dict):
        body = response_data.get("ResponseObject", {}) if isinstance(response_data, dict) else {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    if isinstance(body, list):
        body = body[0] if body else {}
    if not isinstance(body, dict):
        raise ValueError("GetUser response body did not contain user details")

    return {
        "DepartmentCode": body.get("DepartmentCode"),
        "DepartmentId": body.get("DepartmentId"),
        "DepartmentName": body.get("DepartmentName"),
    }


# ============================================================
# Apply Leave (STRICT PAYLOAD FIX)
# ============================================================
class LeaveApiError(Exception):
    """An API failure whose message should be shown directly to the user."""


def _extract_general_errors(value: Any) -> Optional[str]:
    """Find ERP GeneralErrors messages in direct or nested response bodies."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None

    if isinstance(value, list):
        messages = [str(item) for item in value if item not in (None, "")]
        return "\n".join(messages) if messages else None

    if not isinstance(value, dict):
        return None

    errors = value.get("GeneralErrors")
    if errors:
        if isinstance(errors, list):
            messages = [str(item) for item in errors if item not in (None, "")]
            return "\n".join(messages) if messages else None
        return str(errors)

    for key in ("Body", "contents", "ResponseObject", "ErrorBody", "Errors"):
        if key in value:
            message = _extract_general_errors(value[key])
            if message:
                return message
    return None


def apply_leave(slots: Dict[str, Any], login: Dict[str, Any]) -> Dict[str, Any]:
    """Submit leave application after building from the active DB configuration."""
    return submit_leave_payload(prepare_leave_payload(slots, login), login)


def prepare_leave_payload(slots: Dict[str, Any], login: Dict[str, Any]) -> Dict[str, Any]:
    """Load APPLY_LEAVE configuration, validate inputs, and dynamically build payload."""
    from_dt = _parse_date(slots.get("FromDate"))
    to_dt = _parse_date(slots.get("ToDate"))
    if not from_dt or not to_dt:
        raise ValueError("Invalid dates provided")
    days = (to_dt - from_dt).days + 1
    if days <= 0:
        raise ValueError(f"Invalid date range: {days} days")
    values = dict(slots)
    values["NumberOfDays"] = float(days)
    if not values.get("LeaveTypeId"):
        values["LeaveTypeId"] = map_leave_type_id(values.get("LeaveType"))
    if not values.get("ReasonId"):
        reason_id, reason_name = map_leave_reason(values.get("Reason"))
        if reason_id:
            values["ReasonId"] = reason_id
            values["Reason"] = reason_name or values.get("Reason", "")
    if values.get("ReasonId") not in (None, ""):
        values["ReasonId"] = int(values["ReasonId"])
    department_details = _get_user_department_details(login)
    from capability_engine.capability_executor import prepare_apply_leave
    payload, _ = prepare_apply_leave(values, login, department_details)
    return payload


def submit_leave_payload(payload: Dict[str, Any], login: Dict[str, Any]) -> Dict[str, Any]:
    """Execute Save Leave for a payload that has already been confirmed."""
    logger.info("Submitting confirmed leave application.")
    try:
        # Keep direct_url as the URL builder; use the endpoint from this DB version.
        dependency = _find_configured_dependency(("save leave", "save tleave", "savetleave", "apply leave"))
        method = str(_dependency_setting(dependency, "method", "httpmethod", "requestmethod") or "POST").upper()
        if method != "POST":
            raise ValueError("The configured Save Leave dependency must use POST.")
        endpoint = _dependency_setting(dependency, "endpoint", "url", "path", "endpointurl", "apiendpoint")
        if not endpoint:
            raise ValueError("The configured Save Leave dependency has no endpoint.")
        url = direct_url(endpoint, login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        try:
            result = response.json()
        except ValueError:
            result = None
        api_error = _extract_general_errors(result)
        if api_error:
            raise LeaveApiError(api_error)
        if not response.ok:
            response.raise_for_status()
        if result is None:
            result = response.json()
        logger.info("Save Leave API response received.")

        if isinstance(result, dict) and isinstance(result.get("status"), dict):
            http_code = result["status"].get("http_code")
            if http_code and http_code != 200:
                raise Exception(f"Server returned error {http_code}. The endpoint {url} might be incorrect.")
        if isinstance(result, dict) and isinstance(result.get("contents"), str):
            raw_content = result["contents"]
            if "\r\n\r\n" in raw_content:
                try:
                    inner_data = json.loads(raw_content.split("\r\n\r\n", 1)[1])
                    inner_status = inner_data.get("Status")
                    if inner_status and inner_status != 200:
                        raise Exception(f"API Error: {inner_data.get('Body')} (Status: {inner_status})")
                except json.JSONDecodeError:
                    pass
        if isinstance(result, dict) and result.get("Status") in ("Failed", 400, 412, 405):
            decoded = {}
            body_raw = result.get("Body") or (result.get("contents") or {}).get("Body", "")
            if isinstance(body_raw, str) and body_raw:
                try:
                    decoded = decode_response_body(body_raw)
                except Exception:
                    pass
            error_msg = ""
            if decoded:
                inner = decoded.get("Body", "")
                if isinstance(inner, str):
                    try:
                        inner_parsed = json.loads(inner)
                        error_msg = inner_parsed.get("Body", "") if isinstance(inner_parsed, dict) else inner
                    except Exception:
                        error_msg = inner
            if not error_msg:
                error_msg = result.get("Body", "Leave could not be applied.")
            raise Exception(error_msg)
        logger.info("Leave application submitted successfully.")
        return result
    except Exception as e:
        logger.error("Leave application failed: %s", e)
        raise
# ============================================================
# Get Leave Balance (LeaveStatusReport)
# ============================================================
def get_leave_balance(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch leave balance for the logged-in employee using LeaveStatusReport API."""
    logger.info("📋 Fetching leave balance...")
    try:
        payload = CRITERIA.get("LEAVE_BALANCE_CRITERIA")
        if not payload:
            logger.error("❌ LEAVE_BALANCE_CRITERIA not found in criteria.json")
            return []

        import copy
        payload = copy.deepcopy(payload)

        # Replace dynamic fields from login_dto
        criteria_dto = payload.get("CriteriaDTO", {})
        for section in criteria_dto.get("SectionCriteriaList", []):
            for attr in section.get("AttributesCriteriaList", []):
                field_name = attr.get("FieldName")
                if field_name == "EmployeeId":
                    attr["FieldValue"] = next(
                        (login[key] for key in ("UserId", "userId", "EmployeeId", "employeeId")
                         if login.get(key) not in (None, "")),
                        None,
                    )
                elif field_name == "OUId":
                    attr["FieldValue"] = next(
                        (login[key] for key in ("WorkOUId", "workOUId", "OUId", "ouId")
                         if login.get(key) not in (None, "")),
                        None,
                    )
                elif field_name == "PeriodFrom":
                    attr["FieldValue"] = next(
                        (login[key] for key in ("PeriodFrom", "periodFrom", "WorkPeriodFromDate", "workPeriodFromDate")
                         if login.get(key) not in (None, "")),
                        None,
                    )
                elif field_name == "PeriodTo":
                    attr["FieldValue"] = next(
                        (login[key] for key in (
                            "PeriodTo", "periodTo", "WorkPeriodToDate", "workPeriodToDate",
                            "WorkPeriodToDat", "workPeriodToDat",
                        ) if login.get(key) not in (None, "")),
                        None,
                    )

        url = direct_url("/prs/Leave/LeaveStatusReport/?FirstNumber=-1&MaxResult=-1", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        logger.info(f"🔍 Leave Balance URL: {url}")
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        response_data = response.json()
        if isinstance(response_data, dict):
            logger.info(
                "LeaveStatusReport raw response structure: type=dict, keys=%s",
                list(response_data.keys()),
            )
            for key in ("contents", "ResponseObject", "Data", "Body"):
                if key not in response_data:
                    continue
                value = response_data[key]
                if isinstance(value, dict):
                    logger.info("LeaveStatusReport %s structure: type=dict, keys=%s", key, list(value.keys()))
                elif isinstance(value, list):
                    first_item = value[0] if value else None
                    first_item_keys = list(first_item.keys()) if isinstance(first_item, dict) else None
                    logger.info(
                        "LeaveStatusReport %s structure: type=list, length=%d, first_item_type=%s, first_item_keys=%s",
                        key, len(value), type(first_item).__name__ if first_item is not None else None, first_item_keys,
                    )
                else:
                    logger.info("LeaveStatusReport %s structure: type=%s", key, type(value).__name__)
        else:
            logger.info("LeaveStatusReport raw response structure: type=%s", type(response_data).__name__)

        if isinstance(response_data, dict) and isinstance(response_data.get("Body"), list):
            result = response_data["Body"]
        else:
            result = parse_api_response(response_data)
        logger.info(f"✅ Leave balance fetched: {len(result) if isinstance(result, list) else 'non-list'} records")
        if isinstance(result, list) and result:
            logger.info(f"🔑 Leave balance record fields: {list(result[0].keys())}")
            logger.info(f"📄 First record sample: {result[0]}")
        return result if isinstance(result, list) else []

    except Exception as e:
        logger.error(f"❌ Failed to fetch leave balance: {e}")
        return []


if __name__ == "__main__":
    print("Leave Client Module Loaded")
