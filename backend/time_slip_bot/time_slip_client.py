import requests
import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List

from config import settings

# ✅ BizTransaction
from biztransactionid.service import get_biz_transaction_type_id
from biztransactionid import TIME_SLIP_TRANSACTION_CLASS_ID

# ✅ New Imports for Reason Fetching
import logging
import gzip
import base64

# ============================================================
# Logging Configuration
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# Load Criteria from JSON
# ============================================================
try:
    with open("criteria.json", "r", encoding="utf-8") as f:
        CRITERIA = json.load(f)
    
    LEAVE_REASON_CRITERIA = CRITERIA["LEAVE_REASON_CRITERIA"]
    SHIFT_ID_CRITERIA = CRITERIA.get("SHIFT_ID_CRITERIA", {})
    logger.info("✓ Criteria loaded successfully from criteria.json")
    
except FileNotFoundError:
    logger.error("❌ criteria.json file not found!")
    LEAVE_REASON_CRITERIA = {}
    SHIFT_ID_CRITERIA = {}

except Exception as e:
    logger.error(f"❌ Error loading criteria.json: {e}")
    LEAVE_REASON_CRITERIA = {}
    SHIFT_ID_CRITERIA = {}


# ============================================================
# Persistent Session
# ============================================================
session = requests.Session()
session.headers.update({
    'User-Agent': 'TimeSlipClient/1.0',
    'Accept': 'application/json'
})

# ============================================================
# DATE PARSER
# ============================================================

def _parse_date(val: str) -> datetime:
    if not val:
        raise ValueError("TimeSlipDate is missing")

    # If it's already in ERP format, extract it
    if "/Date(" in val:
        try:
            ts = int(re.search(r"-?\d+", val).group()) / 1000
            return datetime.fromtimestamp(ts)
        except Exception:
            pass

    # Extract date pattern
    m = re.search(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", val)
    if not m:
        # Try YYYY-MM-DD
        m = re.search(r"\d{4}-\d{2}-\d{2}", val)
        
    if not m:
        raise ValueError(f"Invalid TimeSlipDate: {val}")

    clean = m.group(0)

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue

    raise ValueError(f"Invalid TimeSlipDate format: {val}")

# ============================================================
# HELPER FUNCTIONS (Copied from leave_client.py)
# ============================================================

def decode_response_body(body: str) -> Dict[str, Any]:
    """Parse a plain JSON response body, with support for legacy gzip wrappers."""
    if not isinstance(body, str):
        return {}

    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"Body": parsed}
    except json.JSONDecodeError:
        pass

    try:
        compressed = base64.b64decode(body, validate=True)
        decompressed = gzip.decompress(compressed)
        parsed = json.loads(decompressed.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"Body": parsed}
        return {}
    except Exception as e:
        logger.error(f"Decode error: {e}")
        return {}


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
            decoded = decode_response_body(body)
            if check_decoded_error(decoded): return []
            
            if "Body" in decoded:
                inner = decoded["Body"]
                return json.loads(inner) if isinstance(inner, str) else inner
            if "ResponseObject" in decoded:
                return decoded["ResponseObject"]

    if "Body" in response_data:
        body = response_data["Body"]
        if isinstance(body, str) and body:
            decoded = decode_response_body(body)
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
# TIME HELPERS
# ============================================================

def _duration_minutes(from_time: str, to_time: str) -> int | None:
    try:
        start = datetime.strptime(from_time.strip(), "%H:%M")
        end = datetime.strptime(to_time.strip(), "%H:%M")
    except Exception:
        return None

    if end < start:
        # If end is before start, assume it's for the next day (cross-day)
        diff = (end + timedelta(days=1)) - start
    else:
        diff = end - start

    return int(diff.total_seconds() / 60)

# ============================================================
# GET REASONS (Similar to leave_client.py)
# ============================================================

# ============================================================
# DATABASE-DRIVEN APPLY_PERMISSION
# ============================================================

def _permission_dependency(config: Dict[str, Any], operation: str) -> Dict[str, Any]:
    terms = ("reason", "lookup") if operation == "lookup" else ("save", "permission")
    for dependency in config.get("dependencies", []):
        if not isinstance(dependency, dict):
            continue
        identity = " ".join(str(value).casefold() for key, value in dependency.items()
                            if key.casefold() in {"name", "code", "id", "operation", "dependencyname",
                                                  "endpoint", "url", "path", "endpointurl", "apiendpoint", "apipath"})
        if all(term in identity for term in terms):
            return dependency
    raise RuntimeError(f"No configured Permission {operation} dependency was found.")


def _execute_permission_lookup(login: Dict[str, Any], slots: Dict[str, Any] | None = None):
    from capability_engine.configuration_loader import load_capability
    from capability_engine.dependency_executor import execute_dependency
    config = load_capability("APPLY_PERMISSION")
    raw = execute_dependency(_permission_dependency(config, "lookup"), login=login, inputs=slots or {},
                             session=session, url_builder=direct_url, timeout=60)
    return raw if isinstance(raw, list) else parse_api_response(raw) if isinstance(raw, dict) else []


def _employee_details_dependency(config: Dict[str, Any]) -> Dict[str, Any]:
    for dependency in config.get("dependencies", []):
        if isinstance(dependency, dict) and dependency.get("code") == "EMPLOYEE_DETAILS_LOOKUP":
            return dependency
    raise RuntimeError("APPLY_PERMISSION has no EMPLOYEE_DETAILS_LOOKUP dependency configured.")


def _employee_response_data(raw: Any) -> Any:
    """Unwrap common ERP envelopes and JSON-encoded Body values."""
    current = raw
    for _ in range(8):
        if isinstance(current, str):
            try:
                current = json.loads(current)
                continue
            except (TypeError, json.JSONDecodeError):
                decoded = decode_response_body(current)
                if decoded:
                    current = decoded
                    continue
                break
        if isinstance(current, dict):
            next_value = None
            found = False
            for key in ("Body", "contents", "ResponseObject", "Data"):
                if key in current:
                    next_value = current[key]
                    found = True
                    break
            if found:
                current = next_value
                continue
        break
    if isinstance(current, list):
        return current[0] if current and isinstance(current[0], dict) else current
    return current


def _configured_employee_response(raw: Any, dependency: Dict[str, Any]) -> Dict[str, Any]:
    data = _employee_response_data(raw)
    if not isinstance(data, dict):
        raise ValueError("EMPLOYEE_DETAILS_LOOKUP returned no usable employee detail object.")
    mapping = next((value for key, value in dependency.items()
                    if key.casefold() == "response_mapping"), None)
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except json.JSONDecodeError as exc:
            raise ValueError("EMPLOYEE_DETAILS_LOOKUP response_mapping is invalid JSON.") from exc
    if isinstance(mapping, list):
        normalized = {}
        for item in mapping:
            if not isinstance(item, dict):
                continue
            target = next((v for k, v in item.items() if k.casefold() in {"target", "targetfield", "field", "name"}), None)
            source = next((v for k, v in item.items() if k.casefold() in {"source", "sourcefield", "sourcepath", "path"}), None)
            if target and source:
                normalized[str(target)] = source
        mapping = normalized
    if not isinstance(mapping, dict):
        raise ValueError("EMPLOYEE_DETAILS_LOOKUP has no valid response_mapping configured.")

    result = {}
    for target in ("DepartmentCode", "DepartmentId", "DepartmentName", "EmployeeCode"):
        source = next((value for key, value in mapping.items() if str(key).casefold() == target.casefold()), None)
        if isinstance(source, dict):
            source = next((value for key, value in source.items()
                           if key.casefold() in {"source", "sourcefield", "sourcepath", "path"}), None)
        if not source:
            raise ValueError(f"EMPLOYEE_DETAILS_LOOKUP response_mapping is missing {target}.")
        value = data
        for part in str(source).replace("$.", "").split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                value = None
                break
        if value in (None, ""):
            raise ValueError(f"EMPLOYEE_DETAILS_LOOKUP response is missing required value for {target} (source {source}).")
        result[target] = value
    return result


def _execute_employee_details_lookup(config: Dict[str, Any], login: Dict[str, Any]) -> Dict[str, Any]:
    from capability_engine.dependency_executor import execute_dependency
    dependency = _employee_details_dependency(config)
    raw = execute_dependency(dependency, login=login, inputs={}, session=session,
                             url_builder=direct_url, timeout=60)
    return _configured_employee_response(raw, dependency)


def get_time_slip_reasons(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch Permission reasons using the configured criteria without changing them."""
    try:
        result = _execute_permission_lookup(login)
        return result if isinstance(result, list) else []
    except Exception as exc:
        logger.error("Failed to fetch configured Permission reasons: %s", exc)
        return []


def apply_time_slip(slots: Dict[str, Any], login: Dict[str, Any]) -> Dict[str, Any]:
    """Run APPLY_PERMISSION through configured validation, mappings, and dependencies."""
    from capability_engine.capability_executor import prepare_apply_permission
    from capability_engine.configuration_loader import load_capability
    from capability_engine.dependency_executor import execute_dependency

    config = load_capability("APPLY_PERMISSION")
    date = _parse_date(slots.get("TimeSlipDate"))
    from_time = str(slots.get("FromTime", "")).strip()
    to_time = str(slots.get("ToTime", "")).strip()
    duration = _duration_minutes(from_time, to_time)
    if duration is None or duration <= 0:
        raise ValueError("Invalid From Time / To Time range")
    reason_input = str(slots.get("TimeSlipReason", "")).strip()
    if not reason_input:
        raise ValueError("TimeSlipReason is required.")
    reasons = _execute_permission_lookup(login, dict(slots))
    selected = reason_input.casefold()
    reason = next((item for item in reasons if isinstance(item, dict) and
                   str(item.get("Name", "")).strip().casefold() == selected), None)
    if not reason or reason.get("Id") in (None, ""):
        raise ValueError(f"Permission reason '{reason_input}' was not found in the ERP reason list.")

    shift_data = get_employee_shift_id(login)
    managed = {
        "BizTransactionTypeId": get_biz_transaction_type_id(TIME_SLIP_TRANSACTION_CLASS_ID, login),
        "ShiftId": shift_data.get("ShiftId", "-1499999997"),
        "ShiftDescription": shift_data.get("ShiftDescription", "General Shift"),
    }
    values = dict(slots)
    values.update({"TimeSlipDate": date.strftime("%Y-%m-%d"), "FromTime": from_time, "ToTime": to_time})
    api_responses = _execute_employee_details_lookup(config, login)
    payload, _ = prepare_apply_permission(values, login,
                                          {"Id": reason["Id"], "Name": reason.get("Name", "")}, managed,
                                          api_responses=api_responses)

    # Keep the existing duration field; the configured computations populate
    # the mapped TimeSlipTimeSlipStartTime and TimeSlipTimeSlipEndTime fields.
    payload.setdefault("TimeSlipDuration", duration)

    def collect_time_fields(value, path=""):
        found = {}
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                found.update(collect_time_fields(child, child_path))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found.update(collect_time_fields(child, f"{path}[{index}]"))
        elif "time" in path.casefold():
            found[path] = value
        return found

    logger.info(
        "Permission time values: input_from=%s, input_to=%s, duration_minutes=%s, payload_time_fields=%s",
        from_time,
        to_time,
        duration,
        json.dumps(collect_time_fields(payload), default=str),
    )

    save_dependency = _permission_dependency(config, "save")
    method = next((str(value).upper() for key, value in save_dependency.items()
                   if key.casefold() in {"method", "httpmethod", "requestmethod"}), "POST")
    if method != "POST":
        raise ValueError("The configured Save Permission dependency must use POST.")
    logger.info("Permission request payload: %s", json.dumps(payload, default=str, ensure_ascii=False))
    try:
        raw = execute_dependency(save_dependency, login=login, inputs=values,
                                 session=session, url_builder=direct_url, timeout=60, json_body=payload)
    except requests.HTTPError as exc:
        response = exc.response
        if response is None:
            raise
        response_body = response.text
        logger.error("Permission submission HTTP failure: status=%s, response_body=%s",
                     response.status_code, response_body)
        raise RuntimeError(
            f"Permission submission failed with HTTP {response.status_code}: {response_body}"
        ) from exc
    if isinstance(raw, dict):
        status = raw.get("Status")
        nested_status = raw.get("status")
        if status not in (None, 200, "200", "Success", "success"):
            raise RuntimeError(f"Permission submission failed: {raw.get('Body') or status}")
        if isinstance(nested_status, dict) and nested_status.get("http_code") not in (None, 200, "200"):
            raise RuntimeError(f"Permission submission failed: {nested_status}")
    parsed = parse_api_response(raw) if isinstance(raw, dict) else raw
    number = None
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        number = parsed[0].get("TimeSlipNumber")
    elif isinstance(parsed, dict):
        number = parsed.get("TimeSlipNumber")
    return {"permission_number": number, "body": raw, "status_code": 200}


# ============================================================
# GET EMPLOYEE SHIFT ID
# ============================================================

def get_employee_shift_id(login: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch employee shift details from Employee service.
    Returns dict with ShiftId and ShiftDescription.
    """
    logger.info("🔍 Fetching employee shift details...")
    try:
        employee_id = login.get("UserId")
        if not employee_id:
            logger.warning("⚠️ UserId not found in login")
            return {"ShiftId": "-1499999997", "ShiftDescription": "General Shift"}
        
        payload = SHIFT_ID_CRITERIA
        if not payload:
            logger.warning("⚠️ SHIFT_ID_CRITERIA not found in criteria.json")
            return {"ShiftId": "-1499999997", "ShiftDescription": "General Shift"}

        import copy
        payload = copy.deepcopy(payload)
        for section in payload.get("SectionCriteriaList", []):
            for attribute in section.get("AttributesCriteriaList", []):
                field_name = attribute.get("FieldName")
                if field_name == "PeriodFromDate":
                    attribute["FieldValue"] = login.get("WorkPeriodFromDate", "")
                elif field_name == "PeriodToDate":
                    attribute["FieldValue"] = login.get("WorkPeriodToDate", "")
        url = direct_url(f"/prs/DailyAttendance/GetDailyAttendanceEmployeeDetail/?EmployeeId={employee_id}", login)
        
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        session.headers.update({'User-Agent': 'LeaveClient/1.0'})
        
        response = session.post(url, json=payload, headers=headers, timeout=60)
        if not response.ok:
            logger.error(
                "Employee shift lookup API returned status=%s, response_body=%s",
                response.status_code,
                response.text,
            )
        response.raise_for_status()

        def extract_body_data(response_data):
            current = response_data
            while isinstance(current, dict):
                body = current.get("Body")
                if body is None and isinstance(current.get("contents"), dict):
                    body = current["contents"].get("Body")
                if body is None:
                    break
                if isinstance(body, str):
                    try:
                        body = json.loads(body)
                    except json.JSONDecodeError:
                        decoded = decode_response_body(body)
                        if not decoded:
                            break
                        body = decoded
                current = body
            return current if isinstance(current, (list, dict)) else parse_api_response(response_data)

        result = extract_body_data(response.json())
        logger.info(f"🔍 Employee API Result Type: {type(result)}")
        logger.info(f"🔍 Employee API Result: {json.dumps(result, default=str)}")
        
        # Extract shift information from response
        employee_data = None
        if isinstance(result, list) and len(result) > 0:
            employee_data = result[0]
        elif isinstance(result, dict):
            employee_data = result

        if employee_data:
            shift_id = employee_data.get("ShiftId")
            shift_desc = employee_data.get("ShiftDescription", "General Shift")
            
            if shift_id:
                logger.info("Using attendance shift details: ShiftId=%s, ShiftDescription=%s", shift_id, shift_desc)
                return {"ShiftId": shift_id, "ShiftDescription": shift_desc}
            else:
                 logger.warning("⚠️ ShiftId missing in employee data")
        
        # Fallback if no data
        logger.warning("⚠️ No employee data found or ShiftId missing, using defaults")
        return {"ShiftId": "-1499999997", "ShiftDescription": "General Shift"}
    
    except Exception as e:
        logger.error(f"❌ Failed to fetch employee shift: {e}")
        # Return defaults on error
        return {"ShiftId": "-1499999997", "ShiftDescription": "General Shift"}

# ============================================================
# URL HELPERS
# ============================================================

def direct_url(path: str, login: Dict[str, Any] = None) -> str:
    """
    Build direct API URL using login object's BaseUri if available.
    This ensures requests go to the correct server/database.
    """
    base = settings.get_direct_url(login)
    path = path.lstrip("/")
    url = f"{base}/{path}"
    return url



# ============================================================
# APPLY TIME SLIP (✅ FIXED)
# ============================================================

def _period_epoch_timestamps() -> tuple:
    """Return (from_epoch_str, to_epoch_str) for start/end of current month as Unix timestamp strings (seconds)."""
    import calendar as _cal
    now = datetime.utcnow()
    start = datetime(now.year, now.month, 1)
    end_day = _cal.monthrange(now.year, now.month)[1]
    end = datetime(now.year, now.month, end_day, 23, 59, 59)
    return str(int(start.timestamp())), str(int(end.timestamp()))


def _to_epoch_str(gb_val) -> str | None:
    """Convert GB /Date(ms)/ or numeric string to epoch seconds string."""
    if gb_val is None:
        return None
    val = str(gb_val)
    m = re.search(r"/Date\((\d+)\)/", val)
    if m:
        return str(int(m.group(1)) // 1000)
    if val.isdigit():
        ts = int(val)
        return str(ts // 1000) if ts > 9_999_999_999 else val
    return None


def get_time_slip_balance(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch permission balance for the logged-in employee using TimeSlipSummary API."""
    logger.info("📋 Fetching permission balance...")
    try:
        payload = CRITERIA.get("TIMESLIP_BALANCE_CRITERIA")
        if not payload:
            logger.error("❌ TIMESLIP_BALANCE_CRITERIA not found in criteria.json")
            return []

        import copy
        payload = copy.deepcopy(payload)

        period_from, period_to = _period_epoch_timestamps()
        period_from = _to_epoch_str(login.get("PeriodFrom")) or period_from
        period_to   = _to_epoch_str(login.get("PeriodTo"))   or period_to

        for section in payload.get("SectionCriteriaList", []):
            for attr in section.get("AttributesCriteriaList", []):
                field_name = attr.get("FieldName")
                if field_name == "EmployeeId":
                    attr["FieldValue"] = login.get("UserId")
                elif field_name == "OUId":
                    attr["FieldValue"] = login.get("WorkOUId")
                elif field_name == "PeriodFromDate":
                    attr["FieldValue"] = period_from
                elif field_name == "PeriodToDate":
                    attr["FieldValue"] = period_to

        url = direct_url("/prs/TimeSlip.svc/TimeSlipSummary", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        logger.info(f"🔍 Permission Balance URL: {url}")
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        result = parse_api_response(response.json())
        logger.info(f"✅ Permission balance fetched: {len(result) if isinstance(result, list) else 'non-list'} records")
        if result:
            logger.info(f"📋 Permission balance raw record: {json.dumps(result[0] if isinstance(result, list) else result, indent=2)}")
        return result if isinstance(result, list) else []

    except Exception as e:
        logger.error(f"❌ Failed to fetch permission balance: {e}")
        return []
