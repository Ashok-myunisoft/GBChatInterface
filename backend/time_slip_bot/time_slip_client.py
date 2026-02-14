from urllib.parse import urlparse
import httpx
import json
import uuid
import re
from datetime import datetime, timedelta
from typing import Dict, Any

from config import settings
from utils import map_leave_reason

# ✅ BizTransaction
from biztransactionid.service import get_biz_transaction_type_id
from biztransactionid import TIME_SLIP_TRANSACTION_CLASS_ID


# ============================================================
# HTTP CLIENT
# ============================================================

client = httpx.Client(timeout=settings.GB_API_TIMEOUT or 60.0)

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
    reason_id, reason_name = map_leave_reason(slots.get("TimeSlipReason"))
    if not reason_id:
        raise ValueError("Invalid or missing Time Slip Reason")

    # ---------------- EMPLOYEE ----------------
    emp_id = login.get("UserId")
    emp_name = login.get("UserName")
    emp_code = login.get("UserCode")

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

        "ShiftId": login.get("ShiftId", "-1499999997"),
        "ShiftDescription": login.get("ShiftDescription", "General Shift"),

        "TimeSlipType": "1",
        "TimeSlipDuration": duration,
        "TimeSlipTimeSlipStartTime": _to_minutes(from_time),
        "TimeSlipTimeSlipEndTime": _to_minutes(to_time),

        "DepartmentName": login.get("DepartmentName", ""),
        "DesignationName": login.get("DesignationName", ""),

        "TimeSlipFromTimeMailTemplate": from_time,
        "TimeSlipToTimeMailTemplate": to_time,
        "TimeSlipToDurtionMailTemplate": f"{duration // 60:02}:{duration % 60:02}",

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
    url = f"{settings.GB_API_BASE}/prs/TimeSlip.svc/"

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
    r = client.post(url, json=payload, headers=headers)

    # Raise if NOT 2xx
    r.raise_for_status()

    # ✅ SUCCESS (NO RESPONSE BODY EXPECTED)
    return True
