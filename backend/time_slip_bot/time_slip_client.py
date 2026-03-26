from urllib.parse import urlparse
import requests
import json
import uuid
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List

from config import settings
from utils import map_leave_reason

# ✅ BizTransaction
from biztransactionid.service import get_biz_transaction_type_id
from biztransactionid import TIME_SLIP_TRANSACTION_CLASS_ID

# ✅ New Imports for Reason Fetching
import logging
import gzip
import base64
import calendar

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
    logger.info("✓ Criteria loaded successfully from criteria.json")
    
except FileNotFoundError:
    logger.error("❌ criteria.json file not found!")
    LEAVE_REASON_CRITERIA = {}

except Exception as e:
    logger.error(f"❌ Error loading criteria.json: {e}")
    LEAVE_REASON_CRITERIA = {}


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
            ts = int(re.search(r"\d+", val).group()) / 1000
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


def decode_response_body(body_base64: str) -> Dict[str, Any]:
    """Decode base64 + gzip compressed response body."""
    try:
        compressed = base64.b64decode(body_base64)
        decompressed = gzip.decompress(compressed)
        decoded_str = decompressed.decode("utf-8")
        return json.loads(decoded_str)
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

def _to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


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

def get_time_slip_reasons(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch time slip reasons from API using LEAVE_REASON_CRITERIA."""
    logger.info("📋 Fetching time slip reasons from API...")
    try:
        url = direct_url("/ads/Reason.svc/SelectList", login)
        
        payload: Dict[str, Any]
        if "SectionCriteriaList" in LEAVE_REASON_CRITERIA:
            payload = LEAVE_REASON_CRITERIA.copy()
            payload["SectionCriteriaList"] = clean_section_criteria(
                LEAVE_REASON_CRITERIA["SectionCriteriaList"]
            )
        else:
            payload = LEAVE_REASON_CRITERIA
            
        # Use requests.Session (same as leave_client.py)
        # Use exact matching User-Agent just in case server logic depends on it
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        session.headers.update({'User-Agent': 'LeaveClient/1.0'})
        
        # Debug logging
        logger.info(f"🔍 URL: {url}")
        logger.info(f"🔍 Login keys: {list(login.keys())}")
        logger.info(f"🔍 Payload: {json.dumps(payload)}")
        
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = parse_api_response(response.json())
        return result if isinstance(result, list) else []
    
    except Exception as e:
        logger.error(f"❌ Failed to fetch time slip reasons: {e}")
        return []

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
        
        url = direct_url(f"/prs/Employee.svc/?EmployeeId={employee_id}", login)
        
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        session.headers.update({'User-Agent': 'LeaveClient/1.0'})
        
        response = session.get(url, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = parse_api_response(response.json())
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
                logger.info(f"✅ Found ShiftId: {shift_id}, ShiftDescription: {shift_desc}")
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

def apply_time_slip(slots: Dict[str, Any], login: Dict[str, Any]) -> bool:
    """
    ERP TimeSlip service does NOT return a response body.
    Success = HTTP 200 / 204 with no exception.
    """

    # ---------------- DATE ----------------
    date = _parse_date(slots.get("TimeSlipDate"))
    gb_date = f"/Date({int(date.timestamp() * 1000)})/"

    # ---------------- TIME ----------------
    from_time = slots.get("FromTime")
    to_time = slots.get("ToTime")

    duration = _duration_minutes(from_time, to_time)
    if duration is None:
        raise ValueError("Invalid From Time / To Time range")

    # ---------------- REASON ----------------
    user_reason_input = slots.get("TimeSlipReason")
    reason_id = None
    reason_name = user_reason_input

    try:
        # 1. Try dynamic fetch from API
        api_reasons = get_time_slip_reasons(login)
        normalized_input = (user_reason_input or "").strip().lower()

        for r in api_reasons:
            # Check 'Name' field from API response
            if r.get("Name", "").strip().lower() == normalized_input:
                reason_id = r.get("Id")
                reason_name = r.get("Name")
                break
    except Exception:
        # Fallback if fetch fails
        pass

    # 2. Fallback to static map if not found in API
    if not reason_id:
        mapped_id, mapped_name = map_leave_reason(user_reason_input)
        if mapped_id:
            reason_id = mapped_id
            reason_name = mapped_name

    # ---------------- EMPLOYEE ----------------
    emp_id = login.get("UserId")
    emp_name = login.get("UserName")
    emp_code = login.get("UserCode")
    
    # ---------------- SHIFT (Dynamic Fetch) ----------------
    shift_data = get_employee_shift_id(login)
    shift_id = shift_data.get("ShiftId", "-1499999997")
    shift_description = shift_data.get("ShiftDescription", "General Shift")

    # ---------------- BIZ TRANSACTION ----------------
    biz_transaction_type_id = get_biz_transaction_type_id(
        TIME_SLIP_TRANSACTION_CLASS_ID,
        login
    )

    # ---------------- PAYLOAD ----------------
    payload = {
        "TimeSlipId": 0,
        "BizTransactionTypeId": biz_transaction_type_id,
        "OrganizationUnitId": login.get("WorkOUId"),
        "PeriodId": login.get("WorkPeriodId"),
        "TimeSlipNumber": 0,

        "TimeSlipDate": gb_date,
        "TimeSlipAttendanceDate": gb_date,

        "EmployeeId": emp_id,
        "EmployeeName": emp_name,
        "EmployeeCode": emp_code,

        "ShiftId": shift_id,
        "ShiftDescription": shift_description,

        "TimeSlipType": "1",
        "TimeSlipDuration": duration,
        "TimeSlipStartTime": _to_minutes(from_time),
        "TimeSlipEndTime": _to_minutes(to_time),

        "DepartmentName": login.get("DepartmentName", ""),
        "DesignationName": login.get("DesignationName", ""),

        "TimeSlipFromTimeMailTemplate": from_time,
        "TimeSlipToTimeMailTemplate": to_time,
        "TimeSlipToDurationMailTemplate": f"{duration // 60:02}:{duration % 60:02}",

        "ApprovedById": -1,

        "TimeSlipReason": reason_name,
        "PermissionReasonId": reason_id,

        "FromTimeForPrint": from_time,
        "ToTimeForPrint": to_time,

        "TimeSlipStatus": 1,
        "TimeSlipPermissionHoursMailTemplate": "0:00",
        "TimeSlipVersion": 1,

        "GuId": str(uuid.uuid4())
    }

    # ---------------- SERVICE CALL ----------------
    url = direct_url("/prs/TimeSlip.svc/", login)

    # Derive Origin and Referer from GB_API_BASE
    parsed_base = urlparse(settings.GB_API_BASE)
    base_url = f"{parsed_base.scheme}://{parsed_base.netloc}"

    headers = {
        "Login": json.dumps(login),
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{base_url}/apps/main.html",
        "Origin": base_url,
        "User-Agent": "Mozilla/5.0"
    }

    if settings.DEBUG:
        print("\n===== TIME SLIP PAYLOAD =====")
        print(json.dumps(payload, indent=2))
        print("POST →", url)

    # ---------------- EXECUTE ----------------
    # ---------------- EXECUTE ----------------
    # Use session (requests) for consistent authentication
    try:
        response = session.post(url, json=payload, headers=headers, timeout=60)
        
        # 📝 DEBUG: Log full response details
        logger.info(f"📥 Save Time Slip Response Status: {response.status_code}")
        try:
            # Often GB returns empty body on success (204) or sometimes JSON
            if response.status_code != 204:
                response_json = response.json()
                logger.info(f"📥 Save Time Slip Response Body: {json.dumps(response_json, indent=2)}")
                
                # Check for nested errors often found in GB API
                if isinstance(response_json, dict):
                     if "status" in response_json and response_json["status"].get("http_code") != 200:
                         logger.error(f"❌ API Logical Error: {response_json}")
                     if "contents" in response_json:
                         logger.info(f"📥 Response Contents: {response_json['contents']}")
            else:
                logger.info("📥 Response Body: (No Content - 204)")

        except Exception as e:
            logger.warning(f"⚠️ Could not parse response JSON body: {response.text}")

        # Raise if NOT 2xx
        response.raise_for_status()

        # ✅ SUCCESS
        return True

    except Exception as e:
        logger.error(f"❌ Failed to save time slip: {e}")
        raise


# ============================================================
# GET TIME SLIP (PERMISSION) BALANCE
# ============================================================

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

        for section in payload.get("SectionCriteriaList", []):
            for attr in section.get("AttributesCriteriaList", []):
                field_name = attr.get("FieldName")
                if field_name == "EmployeeId":
                    attr["FieldValue"] = login.get("UserId")
                elif field_name == "OUId":
                    attr["FieldValue"] = login.get("WorkOUId")
                elif field_name == "PeriodFromDate":
                    attr["FieldValue"] = login.get("PeriodFrom", attr["FieldValue"])
                elif field_name == "PeriodToDate":
                    attr["FieldValue"] = login.get("PeriodTo", attr["FieldValue"])

        url = direct_url("/prs/TimeSlip.svc/TimeSlipSummary", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        logger.info(f"🔍 Permission Balance URL: {url}")
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        result = parse_api_response(response.json())
        logger.info(f"✅ Permission balance fetched: {len(result) if isinstance(result, list) else 'non-list'} records")
        return result if isinstance(result, list) else []

    except Exception as e:
        logger.error(f"❌ Failed to fetch permission balance: {e}")
        return []
