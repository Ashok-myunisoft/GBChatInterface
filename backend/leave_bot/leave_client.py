"""
Complete Leave Client for GB API
Handles leave types, reasons, and leave application.
Fixed: Payload structure strictly matches the provided JSON sample (TLeaveStatus, TLeaveVersion, etc.).
"""

import json
import re
import uuid
import calendar
import gzip
import base64
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional
import logging

from config import settings
from utils import map_leave_type_id, map_leave_reason
from biztransactionid.service import get_biz_transaction_type_id
from biztransactionid import LEAVE_TRANSACTION_CLASS_ID


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


def proxy_url(path: str, login: Dict[str, Any] = None) -> str:
    """
    Build proxy API URL using login object's FEUri and BaseUri if available.
    This ensures requests go to the correct server/database.
    """
    base = settings.get_proxy_url(login)
    path = path.lstrip("/")
    url = f"{base}/{path}"
    logger.debug(f"Proxy URL: {url}")
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
    """Decode base64 + gzip compressed response body."""
    try:
        compressed = base64.b64decode(body_base64)
        decompressed = gzip.decompress(compressed)
        decoded_str = decompressed.decode("utf-8")
        return json.loads(decoded_str)
    except Exception as e:
        logger.error(f"Decode error: {e}")
        return {}


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
# Get Leave Types
# ============================================================
def get_leave_types(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch leave types using criteria."""
    logger.info("📋 Fetching leave types from API...")
    try:
        url = direct_url("/prs/Leave.svc/SelectList", login)
        
        if "SectionCriteriaList" in LEAVE_TYPE_CRITERIA:
            payload = LEAVE_TYPE_CRITERIA.copy()
            payload["SectionCriteriaList"] = clean_section_criteria(
                LEAVE_TYPE_CRITERIA["SectionCriteriaList"]
            )
        else:
            payload = LEAVE_TYPE_CRITERIA
        
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = parse_api_response(response.json())
        return result if isinstance(result, list) else []
            
    except Exception as e:
        logger.error(f"❌ Error fetching leave types: {e}")
        return []


def get_leave_types_with_fallback(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    return get_leave_types(login)


# ============================================================
# Get Leave Reasons
# ============================================================
def get_leave_reasons(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch leave reasons from API."""
    logger.info("📋 Fetching leave reasons from API...")
    try:
        url = direct_url("/ads/Reason.svc/SelectList", login)
        
        if "SectionCriteriaList" in LEAVE_REASON_CRITERIA:
            payload = LEAVE_REASON_CRITERIA.copy()
            payload["SectionCriteriaList"] = clean_section_criteria(
                LEAVE_REASON_CRITERIA["SectionCriteriaList"]
            )
        else:
            payload = LEAVE_REASON_CRITERIA
            
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = parse_api_response(response.json())
        return result if isinstance(result, list) else []
    
    except Exception as e:
        logger.error(f"❌ Failed to fetch leave reasons: {e}")
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
    timestamp_ms = int(dt.timestamp() * 1000)
    return f"/Date({timestamp_ms})/"


# ============================================================
# Apply Leave (STRICT PAYLOAD FIX)
# ============================================================
def apply_leave(slots: Dict[str, Any], login: Dict[str, Any]) -> Dict[str, Any]:
    """Submit leave application with payload strictly matching the provided JSON structure."""
    logger.info("📝 Applying leave...")
    
    try:
        from_dt = _parse_date(slots.get("FromDate"))
        to_dt = _parse_date(slots.get("ToDate"))
        
        if not from_dt or not to_dt:
            raise Exception("Invalid dates provided")
        
        days = (to_dt - from_dt).days + 1
        if days <= 0:
            raise Exception(f"Invalid date range: {days} days")
        
        # Prepare Data Mapping
        emp_id = str(login["UserId"])
        emp_name = login.get("UserName", "")
        
        leave_type_id = slots.get("LeaveTypeId") or map_leave_type_id(slots.get("LeaveType"))
        # Use the name provided in slots, or default to empty string if missing
        leave_type_name = slots.get("LeaveType", "")
        
        # Use the ReasonId/Reason already set by the selection flow.
        # Fall back to the static map only if the slot is missing.
        reason_id = slots.get("ReasonId") or None
        reason_name = slots.get("Reason") or ""
        if not reason_id:
            mapped_id, mapped_name = map_leave_reason(slots.get("Reason"))
            reason_id = mapped_id
            if mapped_name:
                reason_name = mapped_name
        
        biz_type_id = get_biz_transaction_type_id(LEAVE_TRANSACTION_CLASS_ID, login)
        
        # Get Day Name (e.g., "Monday")
        day_name = from_dt.strftime("%A")

        # --- BUILD PAYLOAD STRICTLY MATCHING YOUR JSON SAMPLE ---
        payload = {
            "BizTransactionTypeId": biz_type_id,
            "EmployeeName": emp_name,
            "GuId": str(uuid.uuid4()),
            "OUId": login["WorkOUId"],
            "PeriodId": login["WorkPeriodId"],
            "ReasonId": reason_id,
            "TAvailabeLeave": 0,
            "TLeaveDayType": slots.get("TLeaveDayType", "FullDay"),
            "TLeaveDetailArray": [
                {
                    "DayType": slots.get("TLeaveDayTypeCode", "0"),
                    "EmployeeId": emp_id,
                    "EmployeeName": emp_name,
                    "LeaveDayName": day_name,
                    "LeaveTypeId": leave_type_id,
                    "TLeaveDetailLeaveDate": _gb_date(from_dt),
                    "TLeaveDetailLeaveDayType": slots.get("TLeaveDayTypeCode", "0"),
                    "TLeaveDetailNumberOfDays": str(days),
                    "TLeaveDetailSlNo": 1,
                    "TLeaveDetailValidTill": _gb_date(to_dt)
                }
            ],
            "TLeaveId": 0,
            "TLeaveLeaveDate": _gb_date(from_dt),
            "TLeaveLeaveNumber": "",
            "TLeaveNumberOfDays": str(days),
            "TLeaveReferenceDate": _gb_date(from_dt),
            "TLeaveReferenceNumber": "0",
            "TLeaveRemarks": reason_name,
            "TLeaveStatus": 1,
            "TLeaveType": leave_type_name,
            "TLeaveVersion": 1
        }
        
        logger.info(f"🚀 Submitting Leave Payload: {json.dumps(payload, indent=2)}")
        
        # Use HMS endpoint (Correction from previous issue)
        url = proxy_url("/prs/TLeave.svc", login)
        
        headers = {
            "Content-Type": "application/json",
            "Login": json.dumps(login)
        }
        
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        logger.info(f"📩 API Response: {json.dumps(result, indent=2)}")

        # --- ROBUST ERROR CHECKING ---
        if "status" in result and isinstance(result["status"], dict):
            http_code = result["status"].get("http_code")
            if http_code and http_code != 200:
                raise Exception(f"Server returned error {http_code}. The endpoint {url} might be incorrect.")

        if "contents" in result and isinstance(result["contents"], str):
            raw_content = result["contents"]
            if "\r\n\r\n" in raw_content:
                try:
                    json_part = raw_content.split("\r\n\r\n", 1)[1]
                    inner_data = json.loads(json_part)
                    inner_status = inner_data.get("Status")
                    if inner_status and inner_status != 200:
                        raise Exception(f"API Error: {inner_data.get('Body')} (Status: {inner_status})")
                except json.JSONDecodeError:
                    pass

        if isinstance(result, dict):
            if result.get("Status") == "Failed" or result.get("Status") == 400 or result.get("Status") == 412:
                raise Exception(f"API Rejected Request: {result}")

        logger.info("✅ Leave application submitted successfully")
        return result
    
    except Exception as e:
        logger.error(f"❌ Leave application failed: {e}")
        raise

# ============================================================
# Get Leave Balance (LeaveStatusReport)
# ============================================================
def get_leave_balance(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch leave balance for the logged-in employee using LeaveStatusReport API."""
    logger.info("📋 Fetching leave balance...")
    try:
        with open("criteria.json", "r", encoding="utf-8") as f:
            criteria_data = json.load(f)

        payload = criteria_data.get("LEAVE_BALANCE_CRITERIA")
        if not payload:
            logger.error("❌ LEAVE_BALANCE_CRITERIA not found in criteria.json")
            return []

        import copy
        payload = copy.deepcopy(payload)

        # Replace dynamic fields from login_dto
        for section in payload.get("SectionCriteriaList", []):
            for attr in section.get("AttributesCriteriaList", []):
                field_name = attr.get("FieldName")
                if field_name == "EmployeeId":
                    attr["FieldValue"] = login.get("UserId")
                elif field_name == "PeriodFrom":
                    attr["FieldValue"] = login.get("PeriodFrom")
                elif field_name == "PeriodTo":
                    attr["FieldValue"] = login.get("PeriodTo")

        url = direct_url("/prs/Leave.svc/LeaveStatusReport/?FirstNumber=-1&MaxResult=-1", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        logger.info(f"🔍 Leave Balance URL: {url}")
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        result = parse_api_response(response.json())
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

