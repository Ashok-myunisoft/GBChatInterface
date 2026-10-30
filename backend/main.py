import re
import json
import base64
import hashlib
import time
import difflib
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional
from pathlib import Path

# Ensure services/menu-agent/ is on sys.path before any local imports.
# Without this, "from services.extraction import ..." fails on every
# uvicorn start (including hot-reload) because the CWD is backend/,
# not services/menu-agent/.
_MENU_AGENT_DIR = Path(__file__).resolve().parent.parent / "services" / "menu-agent"
if str(_MENU_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_MENU_AGENT_DIR))

import httpx

from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from utils.holiday_checker import check_date_warning
from utils.date_parser import parse_date as _parse_leave_date

# ---------------- MENU (in-process, replaces old HTTP proxy to services/menu-agent) ----------------
from menu_bot.menu_client import menu_chat, shutdown_all_agents

# ---------------- PACK ----------------
from pack_bot.agent import call_ollama_chat, normalize_slots
from pack_bot.pack_client import save_pack, get_gb_timestamp

# ---------------- LEAVE ----------------
from leave_bot.leave_agent import call_leave_chat, normalize_leave_slots
from leave_bot.leave_client import (
    LeaveApiError,
    apply_leave,
    get_leave_types_with_fallback as get_leave_types,
    get_leave_reasons,
    get_leave_balance
)

# ---------------- TIME SLIP ----------------
from time_slip_bot.time_slip_agent import (
    call_time_slip_chat,
    normalize_time_slip_slots
)
from time_slip_bot.time_slip_client import (
    apply_time_slip,
    get_time_slip_reasons,
    get_time_slip_balance
)

# ---------------- ATTENDANCE ----------------
from attendance_bot.attendance_agent import call_attendance_chat, normalize_attendance_slots
from attendance_bot.attendance_client import (
    get_pay_periods,
    get_daily_attendance,
    normalize_payperiod_record
)

# ---------------- PURCHASE ORDER ----------------
from purchase_bot.purchase_agent import call_purchase_chat, normalize_purchase_slots
from purchase_bot.purchase_client import create_purchase_order, get_parties, get_items, get_stores

# ---------------- TASK ----------------
from task_bot.task_agent import call_task_chat, normalize_task_slots
from task_bot.task_client import (
    create_task,
    check_daily_hours_available,
    ACTIVITY_LIST,
    MAX_HOURS_PER_TASK_MIN,
    _parse_date as _task_parse_date,
    _parse_hhmm_to_minutes,
)


# ============================================================
# FASTAPI SETUP
# ============================================================

app = FastAPI()


@app.on_event("shutdown")
async def _shutdown_menu_agents():
    # Mirrors the cleanup services/menu-agent/app.py's lifespan() used
    # to do for its own _agents dict, now that those agents live here.
    await shutdown_all_agents()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


# ============================================================
# LOGIN PARSER
# ============================================================

def parse_login(header):
    if not header:
        return settings.GB_LOGIN_DTO
    try:
        return json.loads(base64.b64decode(header))
    except Exception:
        try:
            return json.loads(header)
        except Exception:
            return settings.GB_LOGIN_DTO


class ChatRequest(BaseModel):
    message: str


# ============================================================
# CONVERSATION STATE
# ============================================================

PACK_STATE = {}
LEAVE_STATE = {}
TIME_SLIP_STATE = {}
ATTENDANCE_STATE = {}
PURCHASE_STATE = {}
TASK_STATE = {}
MENU_STATE = {}   # lightweight: only tracks whether user is in a menu conversation
GREETED_USERS: set = set()

# Last-active timestamp per user (for TTL-based expiry)
_STATE_TS: dict = {}
STATE_TTL = 1800  # seconds — abandon after 30 min of inactivity


def _login_state_key(login: Dict[str, Any]) -> str:
    """Create a stable state key from the current Login DTO/session."""
    session_value = next(
        (
            (key, login[key])
            for key in ("LoginEventLogId", "ValidityOfSession", "SessionId")
            if login.get(key) not in (None, "", 0, "0")
        ),
        None,
    )
    identity = (
        json.dumps(session_value, separators=(",", ":"), default=str)
        if session_value
        else json.dumps(login, sort_keys=True, separators=(",", ":"), default=str)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()

# Day-type options shown to the user during leave flow
LEAVE_DAY_TYPE_OPTIONS = [
    {"label": "Full Day",    "value": "FullDay",    "code": "0"},
    {"label": "First Half",  "value": "FirstHalf",  "code": "1"},
    {"label": "Second Half", "value": "SecondHalf", "code": "2"},
]


def _cleanup_expired():
    """Remove state for users who have been inactive longer than STATE_TTL."""
    now = time.time()
    expired = [uid for uid, ts in _STATE_TS.items() if now - ts > STATE_TTL]
    for uid in expired:
        PACK_STATE.pop(uid, None)
        LEAVE_STATE.pop(uid, None)
        TIME_SLIP_STATE.pop(uid, None)
        ATTENDANCE_STATE.pop(uid, None)
        PURCHASE_STATE.pop(uid, None)
        TASK_STATE.pop(uid, None)
        MENU_STATE.pop(uid, None)
        _STATE_TS.pop(uid, None)
        GREETED_USERS.discard(uid)


# ============================================================
# HELPERS
# ============================================================

def _calculate_days(from_date: str, to_date: str) -> str:
    formats = [
        "%d-%m-%y", "%d-%m-%Y",
        "%d/%m/%y", "%d/%m/%Y",
        "%Y-%m-%d"
    ]

    def parse(val):
        for f in formats:
            try:
                return datetime.strptime(val.strip(), f)
            except Exception:
                continue
        return None

    start = parse(from_date)
    end = parse(to_date)

    if not start or not end or end < start:
        return ""

    return str((end - start).days + 1)


def _extract_time(text: str):
    m = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", text)
    return m.group(0) if m else None


_BALANCE_KEYWORDS = ["balance", "available", "remaining", "left", "how many"]
_PERMISSION_BALANCE_KEYWORDS = ["permission", "time slip", "timeslip", "permission balance"]

_LEAVE_TYPE_ALIASES: dict = {
    "casual": "Casual Leave",
    "cl": "Casual Leave",
    "sick": "Sick Leave",
    "sl": "Sick Leave",
    "lop": "Loss of Pay",
    "loss of pay": "Loss of Pay",
    "earned": "Earned Leave",
    "el": "Earned Leave",
    "maternity": "Maternity Leave",
    "ml": "Maternity Leave",
    "comp off": "Comp Off Leave",
    "compoff": "Comp Off Leave",
    "coff": "Comp Off Leave",
    "absent": "Absent",
}


def _is_balance_query(message: str) -> bool:
    msg = message.lower()
    words = re.findall(r'\b\w+\b', msg)
    has_leave = "leave" in msg or _fuzzy_in(words, "leave")
    if not has_leave:
        return False
    return (
        any(k in msg for k in _BALANCE_KEYWORDS) or
        any(_fuzzy_in(words, k) for k in _BALANCE_KEYWORDS)
    )


def _is_greeting_message(message: str) -> bool:
    normalized = re.sub(r"[^\w\s']", "", message.casefold()).strip()
    return normalized in {
        "hi", "hello", "hey", "howdy", "greetings",
        "hi there", "hello there", "hey there",
        "good morning", "good afternoon", "good evening",
    }


def _extract_leave_date_mentions(message: str) -> List[str]:
    """Find supported date expressions in a leave request message."""
    months = (
        "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        "jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    )
    weekdays = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    pattern = re.compile(
        rf"\b(?:day after tomorrow|tomorrow|tmrw|today|yesterday|"
        rf"(?:(?:next|last|this)\s+)?(?:{weekdays})|"
        rf"\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}}|"
        rf"\d{{1,2}}[-/]\d{{1,2}}(?:[-/]\d{{2,4}})?|"
        rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{months})|"
        rf"(?:{months})\s+\d{{1,2}}(?:st|nd|rd|th)?)\b",
        re.IGNORECASE,
    )
    return [
        parsed
        for match in pattern.finditer(message)
        if (parsed := _parse_leave_date(match.group(0)))
    ]


def _is_new_leave_application_request(message: str) -> bool:
    return bool(
        re.search(r"\bleave\b", message, re.IGNORECASE)
        and re.search(r"\b(apply|request|take)\b", message, re.IGNORECASE)
    )


def _explicit_leave_slot_value(slot_name: str, value: str, message: str) -> bool:
    value_words = re.findall(r"\w+", str(value).casefold())
    message_words = set(re.findall(r"\w+", message.casefold()))
    if not value_words:
        return False

    if slot_name == "LeaveType":
        if _extract_leave_date_mentions(str(value)):
            return False
        meaningful_words = [word for word in value_words if word not in {"leave", "type"}]
        if set(meaningful_words) & {"apply", "request", "take", "want", "need"}:
            return False
        return bool(meaningful_words) and all(word in message_words for word in meaningful_words)

    normalized_value = " ".join(value_words)
    normalized_message = " ".join(re.findall(r"\w+", message.casefold()))
    if slot_name == "Reason":
        day_type_values = {
            " ".join(re.findall(r"\w+", option["label"].casefold()))
            for option in LEAVE_DAY_TYPE_OPTIONS
        }
        if set(value_words) & {"leave", "apply", "request", "take", "want", "need"}:
            return False
        if normalized_value in day_type_values:
            return False
        if _extract_leave_date_mentions(str(value)):
            return False
        return normalized_value in normalized_message

    return False


def _is_permission_balance_query(message: str) -> bool:
    msg = message.lower()
    words = re.findall(r'\b\w+\b', msg)
    has_permission = any(k in msg for k in _PERMISSION_BALANCE_KEYWORDS) or \
                     _fuzzy_in(words, "permission") or _fuzzy_in(words, "timeslip")
    if not has_permission:
        return False
    return (
        any(k in msg for k in _BALANCE_KEYWORDS) or
        any(_fuzzy_in(words, k) for k in _BALANCE_KEYWORDS)
    )


def _extract_leave_type_filter(message: str):
    msg = message.lower()
    for alias, canonical in _LEAVE_TYPE_ALIASES.items():
        if alias in msg:
            return canonical
    words = re.findall(r'\b\w+\b', msg)
    for alias, canonical in _LEAVE_TYPE_ALIASES.items():
        if " " not in alias and _fuzzy_in(words, alias, cutoff=0.85):
            return canonical
    return None


def _format_balance_response(balances: list, filter_type: Optional[str] = None) -> str:
    if not balances:
        return "No leave balance information found."

    def _name(b):
        return (b.get("LeaveName") or b.get("LeaveTypeName") or b.get("TLeaveTypeName") or
                b.get("Name") or "")

    def _available(b):
        return (b.get("LeaveBalance") if b.get("LeaveBalance") is not None else
                b.get("AvailableLeave") if b.get("AvailableLeave") is not None else
                b.get("TAvailableLeave") if b.get("TAvailableLeave") is not None else
                b.get("Available") if b.get("Available") is not None else
                b.get("Balance") if b.get("Balance") is not None else 0)

    if filter_type:
        filtered = [b for b in balances if filter_type.lower() in _name(b).lower()]
        if filtered:
            b = filtered[0]
            return f"{_name(b)}: {_available(b)} days available."
        return f"No balance information found for '{filter_type}'."

    lines = ["Your leave balances are:\n"]
    for b in balances:
        lines.append(f"{_name(b)}: {_available(b)} days")
    return "\n".join(lines)


def _format_permission_balance_response(records: list) -> str:
    if not records:
        return "No permission balance information found."

    from datetime import datetime
    r = records[0]

    balance_hours = r.get("BalanceHours", 0)
    balance_times = int(r.get("BalanceTimes", 0))
    taken_times   = int(r.get("PermissionTakenTimes", 0))
    used_hours    = r.get("TimeSlipDuration", 0)
    month         = r.get("MonthPart") or datetime.now().month
    year          = r.get("YearPart") or datetime.now().year

    total_times = taken_times + balance_times
    total_hours = round(used_hours + balance_hours, 2)
    month_name  = datetime(year, month, 1).strftime("%B %Y")

    lines = [
        f"Permission balance for {month_name}:",
        f"  Total allowed : {total_times} time(s)  ({total_hours} hrs)",
        f"  Used          : {taken_times} time(s)  ({used_hours} hrs)",
        f"  Remaining     : {balance_times} time(s)  ({balance_hours} hrs)",
    ]

    if balance_times == 0 or balance_hours <= 0:
        lines.append("  ⚠️ No permission balance remaining this month.")

    return "\n".join(lines)


def _extract_selected_payperiod(record: dict) -> dict:
    return normalize_payperiod_record(record)


# ============================================================
# INTENT RESOLUTION
# ============================================================

def _fuzzy_in(words: list, keyword: str, cutoff: float = 0.82) -> bool:
    return bool(difflib.get_close_matches(keyword, words, n=1, cutoff=cutoff))


async def forward_to_menu_agent(message: str, raw_login_header: str):
    """
    Calls the Menu Agent in-process (backend/menu_bot/agent_manager.py).

    Previously this proxied over HTTP to a standalone Menu Agent
    service (services/menu-agent/app.py, port 8001). That service still
    exists and still runs standalone if started directly, but backend
    no longer talks to it over the network -- menu_chat() imports and
    drives the same MenuAgent class directly.

    Returns (reply_text, picklist_or_None) -- the picklist is a dict with
    "items" and "total" when the reply contains a numbered list, else None.
    """
    return await menu_chat(message, raw_login_header or "{}")


def resolve_intent(message: str) -> str:
    msg = message.lower()
    words = re.findall(r'\b\w+\b', msg)

    leave_score = 0
    ts_score = 0
    attendance_score = 0

    # -------- LEAVE SIGNALS --------
    if "leave" in msg or _fuzzy_in(words, "leave"):
        leave_score += 3
    if any(k in msg for k in ["sick", "casual", "lop"]) or \
            any(_fuzzy_in(words, k) for k in ["sick", "casual", "lop"]):
        leave_score += 2
    if re.search(r"\b\d+\s*(day|days)\b", msg):
        leave_score += 2
    if re.search(r"from\s+\d{1,2}[-/]\d{1,2}", msg):
        leave_score += 2

    # -------- TIME SLIP SIGNALS --------
    if "permission" in msg or "time slip" in msg or \
            _fuzzy_in(words, "permission") or _fuzzy_in(words, "timeslip"):
        ts_score += 3
    if _extract_time(msg):
        ts_score += 3
    if "today" in msg:
        ts_score += 1

    # -------- ATTENDANCE SIGNALS --------
    if any(k in msg for k in ["attendance", "daily attendance", "check in", "check-in", "punch"]):
        attendance_score += 3

    # -------- PURCHASE SIGNALS --------
    purchase_score = 0
    if any(k in msg for k in ["purchase", "po", "purchase order", "buy", "procure"]):
        purchase_score += 3
    if any(k in msg for k in ["vendor", "supplier", "party", "item", "material", "goods"]):
        purchase_score += 2

    # -------- TASK SIGNALS --------
    task_score = 0
    if "task" in msg or _fuzzy_in(words, "task"):
        task_score += 3
    if re.search(r"\bw\d{4,}\b", msg):
        task_score += 4

    # -------- MENU SIGNALS --------
    menu_score = 0
    if any(k in msg for k in ["menu", "module vs menu", "modulevsmenu", "menuset", "menu set"]):
        menu_score += 4
    if any(k in msg for k in ["create menu", "add menu", "new menu", "menu creation"]):
        menu_score += 3
    if any(k in msg for k in ["module", "section", "parent menu"]):
        menu_score += 2

    scores = {
        "leave": leave_score,
        "time_slip": ts_score,
        "attendance": attendance_score,
        "purchase": purchase_score,
        "task": task_score,
        "menu": menu_score,
    }

    best = max(scores, key=lambda k: scores[k])
    if scores[best] > 0:
        return best

    return "unknown"


# ============================================================
# BUSINESS FUNCTIONS
# ============================================================

def create_pack(slots, login):
    pack_name = slots.get("PackName")
    pack_code = slots.get("PackCode")

    payload = {
        "PackName": pack_name,
        "PackCode": pack_code,
        "PackConversionType": 0,
        "PackConversionFactor": "0",
        "PackId": 0,
        "PackVersion": 1,
        "PackStatus": 1,
        "PackCreatedOn": get_gb_timestamp(),
        "PackModifiedOn": get_gb_timestamp(),
        "PackCreatedByName": login["UserName"],
        "PackModifiedByName": login["UserName"]
    }

    save_pack(payload, login)

    return {
        "status": "success",
        "message": f"Pack '{pack_name}' created successfully (Code: {pack_code}) 🎉"
    }


def _extract_leave_number(result) -> str:
    try:
        from leave_bot.leave_client import parse_api_response
        data = parse_api_response(result)
        if isinstance(data, list) and data:
            return data[0].get("TLeaveLeaveNumber") or data[0].get("LeaveNumber") or ""
        if isinstance(data, dict):
            return data.get("TLeaveLeaveNumber") or data.get("LeaveNumber") or ""
    except Exception:
        pass
    return ""


def _extract_decoded_service_body(payload):
    if not payload:
        return None

    candidate = payload
    if isinstance(payload, dict) and "body" in payload:
        candidate = payload.get("body")

    if isinstance(candidate, list):
        return candidate
    if isinstance(candidate, str):
        text = candidate.strip()
        return text if text else None
    if not isinstance(candidate, dict) or not candidate:
        return candidate if candidate not in (None, "", [], {}) else None

    try:
        from leave_bot.leave_client import parse_api_response
        if any(key in candidate for key in ("contents", "Body", "ResponseObject", "Data")):
            decoded = parse_api_response(candidate)
            if decoded in (None, "", [], {}):
                return None
            return decoded

        return candidate
    except Exception:
        return candidate


def _format_service_body(body) -> str:
    if body in (None, "", [], {}):
        return ""
    if isinstance(body, str):
        return body.strip()
    try:
        return json.dumps(body, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(body)


ATTENDANCE_STATUS_MAP = {
    "PP": "Present",
    "A": "Absent",
    "WO": "Week Off",
    "PH": "Public Holiday",
    "NONE": "No Data",
}

ATTENDANCE_STATUS_ORDER = ["Present", "Absent", "Week Off", "Public Holiday", "No Data"]


def _normalize_attendance_status(value) -> Dict[str, str]:
    raw = "" if value in (None, "") else str(value).strip().upper()
    label = ATTENDANCE_STATUS_MAP.get(raw, raw.title() if raw else "No Data")
    tone = {
        "Present": "present",
        "Absent": "absent",
        "Week Off": "week_off",
        "Public Holiday": "public_holiday",
        "No Data": "no_data",
    }.get(label, "no_data")
    return {
        "code": raw or "NONE",
        "label": label,
        "tone": tone,
    }


def _normalize_attendance_date_value(record: Dict[str, Any]) -> str:
    return _format_attendance_date(
        record.get("DailyAttendanceAttendanceDate")
        or record.get("AttendanceDate")
        or record.get("Date")
    )


def _parse_clock_time(value) -> tuple[str, bool]:
    if value in (None, "", 0, "0"):
        return "", False

    text = str(value).strip()
    if not text or text in {"00:00", "0:00", "00:00:00"}:
        return "", False

    match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour == 0 and minute == 0:
            return "", False
        try:
            dt = datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M")
            return dt.strftime("%I:%M %p"), True
        except ValueError:
            return f"{hour:02d}:{minute:02d}", True

    if text.isdigit():
        minutes = int(text)
        if minutes <= 0:
            return "", False
        hour = (minutes // 60) % 24
        minute = minutes % 60
        try:
            dt = datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M")
            return dt.strftime("%I:%M %p"), True
        except ValueError:
            return f"{hour:02d}:{minute:02d}", True

    return text, True


def _format_worked_duration(value) -> str:
    if value in (None, "", 0, "0"):
        return "--"

    text = str(value).strip()
    if not text or text in {"00:00", "0:00", "00:00:00"}:
        return "--"

    match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        if hours == 0 and minutes == 0:
            return "--"
        return f"{hours:02d}:{minutes:02d} Hrs"

    if text.isdigit():
        total_minutes = int(text)
        if total_minutes <= 0:
            return "--"
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return f"{hours:02d}:{minutes:02d} Hrs"

    return f"{text} Hrs"


def _attendance_row_remarks(record: Dict[str, Any], in_time: str, out_time: str) -> Dict[str, str]:
    remarks = (record.get("DailyAttendanceRemarks") or record.get("Remarks") or "").strip()
    if in_time != "--" and out_time == "--" and not remarks:
        remarks = "Incomplete Punch"
    if not remarks:
        remarks = "-"
    tone = "incomplete" if remarks == "Incomplete Punch" else "normal"
    return {
        "value": remarks,
        "tone": tone,
    }


def _build_attendance_row(record: Dict[str, Any]) -> Dict[str, Any]:
    status = _normalize_attendance_status(
        record.get("TextDayType")
        or record.get("DailyAttendanceDayType")
        or record.get("Type")
        or record.get("Status")
    )

    in_raw = (
        record.get("DailyAttendanceCalInTime")
        or record.get("In")
        or record.get("DailyAttendanceInTime")
    )
    out_raw = (
        record.get("DailyAttendanceCalOutTime")
        or record.get("Out")
        or record.get("DailyAttendanceOutTime")
    )
    worked_raw = (
        record.get("DailyAttendanceCalWorkedHours")
        or record.get("Worked")
        or record.get("DailyAttendanceWorkedHours")
    )

    in_time, has_in_time = _parse_clock_time(in_raw)
    out_time, has_out_time = _parse_clock_time(out_raw)
    worked_hours = _format_worked_duration(worked_raw)
    remarks = _attendance_row_remarks(record, in_time, out_time)

    return {
        "date": _normalize_attendance_date_value(record),
        "status": status["label"],
        "statusCode": status["code"],
        "statusTone": status["tone"],
        "inTime": in_time or "--",
        "outTime": out_time or "--",
        "workedHours": worked_hours,
        "workType": (record.get("WorkTypeName") or record.get("WorkType") or "").strip() or "-",
        "shift": (record.get("ShiftDescription") or record.get("Shift") or "").strip() or "-",
        "remarks": remarks["value"],
        "remarksTone": remarks["tone"],
        "hasInTime": has_in_time,
        "hasOutTime": has_out_time,
        "isIncompletePunch": remarks["tone"] == "incomplete",
    }


def _build_attendance_summary(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {
        "totalDays": len(rows),
        "presentDays": 0,
        "absentDays": 0,
        "weekOffDays": 0,
        "holidayDays": 0,
        "incompletePunches": 0,
    }

    for row in rows:
        tone = row.get("statusTone")
        if tone == "present":
            summary["presentDays"] += 1
        elif tone == "absent":
            summary["absentDays"] += 1
        elif tone == "week_off":
            summary["weekOffDays"] += 1
        elif tone == "public_holiday":
            summary["holidayDays"] += 1

        if row.get("isIncompletePunch"):
            summary["incompletePunches"] += 1

    return summary


def _build_attendance_markdown(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return ""

    lines = [f"Daily attendance for {len(rows)} day(s):", ""]
    lines.append("| Date | Status | In Time | Out Time | Worked Hours | Work Type | Shift | Remarks |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")

    for row in rows:
        lines.append(
            "| "
            + " | ".join([
                _escape_md_cell(row.get("date")),
                _escape_md_cell(row.get("status")),
                _escape_md_cell(row.get("inTime")),
                _escape_md_cell(row.get("outTime")),
                _escape_md_cell(row.get("workedHours")),
                _escape_md_cell(row.get("workType")),
                _escape_md_cell(row.get("shift")),
                _escape_md_cell(row.get("remarks")),
            ])
            + " |"
        )

    return "\n".join(lines)


def build_attendance_view(payload) -> Dict[str, Any]:
    records = _parse_attendance_records(payload)
    rows = [_build_attendance_row(record) for record in records if isinstance(record, dict)]
    if not rows:
        return {
            "message": "",
            "data": {
                "rows": [],
                "summary": _build_attendance_summary([]),
                "columns": [
                    "Date", "Status", "In Time", "Out Time",
                    "Worked Hours", "Work Type", "Shift", "Remarks",
                ],
            },
        }

    summary = _build_attendance_summary(rows)
    return {
        "message": _build_attendance_markdown(rows),
        "data": {
            "rows": rows,
            "summary": summary,
            "columns": [
                "Date", "Status", "In Time", "Out Time",
                "Worked Hours", "Work Type", "Shift", "Remarks",
            ],
        },
    }


def _parse_attendance_records(payload):
    if payload in (None, "", [], {}):
        return []

    data = payload

    if isinstance(data, dict):
        if "ResponseObject" in data and data["ResponseObject"] not in (None, "", [], {}):
            data = data["ResponseObject"]
        elif "Data" in data and data["Data"] not in (None, "", [], {}):
            data = data["Data"]
        elif "Body" in data:
            data = data["Body"]

    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except Exception:
            return []

    if isinstance(data, dict):
        if "ResponseObject" in data and data["ResponseObject"] not in (None, "", [], {}):
            data = data["ResponseObject"]
        elif "Data" in data and data["Data"] not in (None, "", [], {}):
            data = data["Data"]

    return data if isinstance(data, list) else []


def _format_attendance_date(value) -> str:
    if value in (None, "", 0):
        return ""

    text = str(value).strip()
    if not text:
        return ""

    m = re.search(r"/Date\((\-?\d+)\)/", text)
    if m:
        try:
            ts_ms = int(m.group(1))
            if ts_ms < 0:
                return ""
            return datetime.utcfromtimestamp(ts_ms / 1000).strftime("%d-%m-%Y")
        except Exception:
            pass

    try:
        from utils.date_parser import parse_date
        parsed = parse_date(text)
        if parsed:
            return parsed
    except Exception:
        pass

    return text


def _format_minutes_as_hhmm(value) -> str:
    if value in (None, "", 0, "0"):
        return ""
    try:
        minutes = int(float(value))
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"
    except Exception:
        return str(value).strip()


def _escape_md_cell(value) -> str:
    if value in (None, ""):
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    return text.replace("|", "\\|")


def _format_attendance_response(payload) -> str:
    return build_attendance_view(payload).get("message", "")


def apply_leave_flow(slots, login):
    try:
        result = apply_leave(slots, login)
        decoded_body = _extract_decoded_service_body(result)
        body_text = _format_service_body(decoded_body)
        if body_text:
            try:
                parsed = json.loads(body_text)
                if isinstance(parsed, dict) and "Body" in parsed:
                    body_text = str(parsed["Body"])
            except Exception:
                pass
            message = body_text
        else:
            leave_number = _extract_leave_number(result)
            number_text = f" (Leave No: {leave_number})" if leave_number else ""
            message = f"Leave applied successfully ✅ for {slots.get('EmployeeName')}{number_text}"
        return {"status": "success", "message": message}
    except LeaveApiError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            return {"status": "error", "message": "Server is taking too long to respond. Please try again later."}
        if "balance" in error_msg.lower() and "less than" in error_msg.lower():
            return {"status": "error", "message": "Sorry, you don't have sufficient leave balance to apply for this leave. 🙏"}
        return {"status": "error", "message": f"Leave could not be applied. {error_msg}"}


def apply_time_slip_flow(slots, login):
    try:
        result = apply_time_slip(slots, login)
        ts_number = ""
        body = None

        if isinstance(result, dict):
            ts_number = result.get("permission_number") or ""
            body = result.get("body")
        else:
            ts_number = result or ""

        decoded_body = _extract_decoded_service_body(body)
        body_text = _format_service_body(decoded_body)
        if body_text:
            try:
                parsed = json.loads(body_text)
                if isinstance(parsed, dict) and "Body" in parsed:
                    body_text = str(parsed["Body"])
            except Exception:
                pass
            message = body_text
        else:
            number_text = f" (Permission No: {ts_number})" if ts_number else ""
            message = f"Permission applied successfully ✅{number_text}"
        return {"status": "success", "message": message}
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            return {"status": "error", "message": "Server is taking too long to respond. Please try again later."}
        return {"status": "error", "message": f"Failed to apply time slip: {error_msg}"}


def apply_task_flow(task_state, login):
    slots = task_state["slots"]
    try:
        result = create_task(
            slots,
            login,
            requester=task_state.get("requester", ""),
            allocation_split=task_state.get("allocation_split", ""),
            work_id=task_state.get("work_id"),
        )
        decoded_body = _extract_decoded_service_body(result)
        body_text = _format_service_body(decoded_body)
        if body_text:
            message = body_text
        else:
            task_number = ""
            if isinstance(result, dict):
                task_number = result.get("Number") or result.get("TaskNumber") or ""
            number_text = f" (Task No: {task_number})" if task_number else ""
            work_number = slots.get("WorkNumber", "")
            message = f"Task created successfully ✅{number_text} against Work {work_number}"
        return {"status": "success", "message": message}
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            return {"status": "error", "message": "Server is taking too long to respond. Please try again later."}
        return {"status": "error", "message": f"Failed to create task: {error_msg}"}


# ============================================================
# PURCHASE HELPERS
# ============================================================

def _first_non_empty(item: dict, *keys, default=""):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return default


def _best_match_record(records: list, needle: str, *, name_keys=(), code_keys=(), id_keys=()):
    if not needle:
        return None

    normalized = str(needle).strip().lower()
    if not normalized:
        return None

    prepared = []
    for record in records or []:
        name = str(_first_non_empty(record, *name_keys, default="")).strip()
        code = str(_first_non_empty(record, *code_keys, default="")).strip() if code_keys else ""
        rec_id = str(_first_non_empty(record, *id_keys, default="")).strip() if id_keys else ""

        if name and normalized == name.lower():
            return record
        if code and normalized == code.lower():
            return record
        if rec_id and normalized == rec_id.lower():
            return record

        if name:
            prepared.append((name.lower(), record))

    if prepared:
        close = difflib.get_close_matches(normalized, [name for name, _ in prepared], n=1, cutoff=0.8)
        if close:
            close_name = close[0]
            for name, record in prepared:
                if name == close_name:
                    return record

    return None


def _build_options(records: list, *, label_keys=(), value_keys=(), code_keys=()):
    options = []
    for record in records or []:
        label = str(_first_non_empty(record, *label_keys, default="")).strip()
        value = _first_non_empty(record, *value_keys, default="")
        if not label:
            continue
        options.append({
            "label": label,
            "value": value,
            "code": str(_first_non_empty(record, *code_keys, default="")).strip() if code_keys else "",
            "record": record
        })
    return options


# ============================================================
# CHAT ENDPOINT
# ============================================================

@app.post("/gbaiapi/chat_Interface")
async def chat(req: ChatRequest, Login: Optional[str] = Header(None)):
    login = parse_login(Login)
    state_key = _login_state_key(login)
    message = req.message.strip()

    _cleanup_expired()
    _STATE_TS[state_key] = time.time()

    PACK_STATE.setdefault(state_key, {"intent": None, "slots": {}, "awaiting": None})
    LEAVE_STATE.setdefault(state_key, {"intent": None, "slots": {}})
    TIME_SLIP_STATE.setdefault(state_key, {"intent": None, "slots": {}})
    ATTENDANCE_STATE.setdefault(state_key, {"intent": None, "slots": {}, "selected_payperiod": None})
    PURCHASE_STATE.setdefault(state_key, {"intent": None, "slots": {}})
    TASK_STATE.setdefault(state_key, {"intent": None, "slots": {}, "requester": "", "allocation_split": "", "work_id": None})
    MENU_STATE.setdefault(state_key, {"active": False})

    pack_state = PACK_STATE[state_key]
    leave_state = LEAVE_STATE[state_key]
    ts_state = TIME_SLIP_STATE[state_key]
    attendance_state = ATTENDANCE_STATE[state_key]
    purchase_state = PURCHASE_STATE[state_key]
    task_state = TASK_STATE[state_key]
    menu_state = MENU_STATE[state_key]

    # A fresh explicit leave application starts with empty slots instead of
    # inheriting details from an unfinished request for this user.
    if leave_state.get("intent") == "apply" and _is_new_leave_application_request(message):
        leave_state = {"intent": "apply", "slots": {}}
        LEAVE_STATE[state_key] = leave_state

    # ========================================================
    # FIRST MESSAGE GREETING
    # ========================================================

    if _is_greeting_message(message):
        GREETED_USERS.add(state_key)
        try:
            balances = []
            perm_records = []

            balance_lines = []
            warnings = []

            def _name(b):
                return (b.get("LeaveName") or b.get("LeaveTypeName") or
                        b.get("TLeaveTypeName") or b.get("Name") or "")

            def _available(b):
                for key in ("LeaveBalance", "AvailableLeave", "TAvailableLeave", "Available", "Balance"):
                    if b.get(key) is not None:
                        return b[key]
                return 0

            for b in balances:
                name = _name(b)
                avail = _available(b)
                balance_lines.append(f"  {name}: {avail} days")
                if "casual" in name.lower() and float(avail or 0) <= 0:
                    warnings.append(
                        "⚠️ You have no Casual Leave remaining. "
                        "You can apply for other available leave types."
                    )

            greeting = ""
            if balance_lines:
                greeting += "Here are your current leave balances:\n" + "\n".join(balance_lines)
                if warnings:
                    greeting += "\n\n" + "\n".join(warnings)

            if perm_records:
                perm_text = _format_permission_balance_response(perm_records)
                greeting += ("\n\n" if greeting else "") + perm_text

            greeting += "\n\nI can help you apply Leave, submit a Time Slip, view Daily Attendance, create a Task, or create a Pack."
            return {"status": "success", "message": greeting.strip()}

        except Exception:
            return {
                "status": "success",
                "message": "Hello! 👋 I can help you apply Leave, submit a Time Slip, view Daily Attendance, create a Task, or create a Pack."
            }

    # ========================================================
    # LEAVE BALANCE QUERY
    # ========================================================

    if _is_balance_query(message):
        try:
            balances = get_leave_balance(login)
            leave_filter = _extract_leave_type_filter(message)
            return {
                "status": "success",
                "message": _format_balance_response(balances, filter_type=leave_filter)
            }
        except Exception:
            return {"status": "error", "message": "Unable to fetch leave balance at the moment. Please try again later."}

    if _is_permission_balance_query(message):
        try:
            records = get_time_slip_balance(login)
            return {
                "status": "success",
                "message": _format_permission_balance_response(records)
            }
        except Exception:
            return {"status": "error", "message": "Unable to fetch permission balance at the moment. Please try again later."}

    # ========================================================
    # MENU AGENT — proxy entire conversation when active
    # ========================================================

    if menu_state["active"] or resolve_intent(message) == "menu":
        menu_state["active"] = True
        reply_text, picklist = await forward_to_menu_agent(message, Login or "{}")
        response = {"status": "success", "message": reply_text}
        if picklist is not None:
            response["picklist"] = picklist
        return response

    # ========================================================
    # ROUTE NEW REQUESTS INTO A FLOW WHEN NO ACTIVE STATE EXISTS
    # ========================================================

    if not any([
        pack_state.get("intent"),
        leave_state.get("intent"),
        ts_state.get("intent"),
        attendance_state.get("intent"),
        purchase_state.get("intent"),
        task_state.get("intent"),
    ]):
        root_intent = resolve_intent(message)
        if root_intent == "leave":
            leave_state = {"intent": "apply", "slots": {}}
            LEAVE_STATE[state_key] = leave_state
        elif root_intent == "time_slip":
            ts_state["intent"] = "apply"
        elif root_intent == "attendance":
            attendance_state["intent"] = "get"
        elif root_intent == "purchase":
            purchase_state["intent"] = "create"
        elif root_intent == "task":
            task_state["intent"] = "create"

    # ========================================================
    # CONTINUE ACTIVE LEAVE FLOW
    # ========================================================

    if leave_state["intent"] == "apply":
        if (
            not leave_state["slots"].get("FromDate")
            and not leave_state["slots"].get("ToDate")
        ):
            single_date = _extract_leave_date_mentions(message)
            if len(single_date) == 1:
                leave_state["slots"]["FromDate"] = single_date[0]
                leave_state["slots"]["ToDate"] = single_date[0]
            elif len(single_date) > 1:
                leave_state["slots"]["FromDate"] = single_date[0]
                leave_state["slots"]["ToDate"] = single_date[-1]

        # Only accept a day type when its label is explicitly in the user's message.
        normalized_message = " ".join(re.findall(r"\w+", message.casefold()))
        for day_type in LEAVE_DAY_TYPE_OPTIONS:
            normalized_label = " ".join(re.findall(r"\w+", day_type["label"].casefold()))
            if normalized_label and normalized_label in normalized_message:
                leave_state["slots"]["TLeaveDayType"] = day_type["value"]
                leave_state["slots"]["TLeaveDayTypeCode"] = day_type["code"]
                break

        is_selection = False

        if "last_options" in leave_state:
            options = leave_state["last_options"]
            selected = None

            if message.isdigit():
                if leave_state.get("awaiting_field") == "TLeaveDayType":
                    idx = int(message)
                else:
                    idx = int(message) - 1
                if 0 <= idx < len(options):
                    selected = options[idx]

            if not selected:
                labels = [o["label"].lower() for o in options]
                close = difflib.get_close_matches(message.lower(), labels, n=1, cutoff=0.7)
                if close:
                    selected = next((o for o in options if o["label"].lower() == close[0]), None)

            if selected:
                target_field = leave_state.get("awaiting_field")
                if target_field == "LeaveType":
                    leave_state["slots"]["LeaveTypeId"] = str(selected["value"])
                    leave_state["slots"]["LeaveType"] = selected["label"]
                elif target_field == "Reason":
                    leave_state["slots"]["ReasonId"] = str(selected["value"])
                    leave_state["slots"]["Reason"] = selected["label"]
                elif target_field == "TLeaveDayType":
                    leave_state["slots"]["TLeaveDayType"] = selected["value"]
                    leave_state["slots"]["TLeaveDayTypeCode"] = selected.get("code", "0")

                is_selection = True
                leave_state.pop("last_options", None)
                leave_state.pop("awaiting_field", None)

        if not is_selection:
            has_date_pattern = bool(
                re.search(r"\d{1,2}[-/]\d{1,2}", message)
                or _extract_leave_date_mentions(message)
            )

            if (
                leave_state["slots"].get("LeaveType")
                and leave_state["slots"].get("FromDate")
                and leave_state["slots"].get("ToDate")
                and not leave_state["slots"].get("Reason")
                and message
                and not has_date_pattern
                and leave_state.get("awaiting_field") == "Reason"
            ):
                leave_state["slots"]["Reason"] = message.strip()
            elif leave_state["slots"].get("LeaveType") and not leave_state["slots"].get("FromDate"):
                from utils.date_parser import parse_date as _parse_date_direct
                parsed = _parse_date_direct(message.strip())
                if parsed:
                    leave_state["slots"]["FromDate"] = parsed
                else:
                    return {"status": "success", "message": "Please provide From Date (e.g., Jan 2 or 02-01-2024)."}
            elif leave_state["slots"].get("LeaveType") and leave_state["slots"].get("FromDate") and not leave_state["slots"].get("ToDate"):
                from utils.date_parser import parse_date as _parse_date_direct
                parsed = _parse_date_direct(message.strip())
                if parsed:
                    leave_state["slots"]["ToDate"] = parsed
                else:
                    return {"status": "success", "message": "Please provide To Date (e.g., Jan 2 or 02-01-2024)."}
            else:
                ai = call_leave_chat(message)
                slots = normalize_leave_slots(ai["action"]["slots"])
                for key in ("LeaveType", "Reason"):
                    value = slots.get(key)
                    if value and _explicit_leave_slot_value(key, value, message):
                        leave_state["slots"][key] = value

        leave_state["slots"].setdefault("EmployeeName", login.get("UserName"))

        if not leave_state["slots"].get("LeaveType"):
            types = get_leave_types(login)
            if not types:
                return {"status": "error", "message": "Unable to fetch leave types at the moment. Please try again later."}
            options = [{"label": t.get("Name") or t.get("TLeaveTypeName"), "value": t.get("Id") or t.get("TLeaveTypeId")} for t in types]
            leave_state["last_options"] = options
            leave_state["awaiting_field"] = "LeaveType"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            warning = check_date_warning(leave_state["slots"].get("FromDate", ""))
            prefix = f"{warning}\n\n" if warning else ""
            return {"status": "success", "message": f"{prefix}Please select Leave Type from the options below:\n{options_text}\n\nReply with the number of your choice."}

        if not leave_state["slots"].get("FromDate"):
            return {"status": "success", "message": "Please provide From Date (e.g., Jan 2 or 02-01-2024)."}

        if not leave_state["slots"].get("ToDate"):
            return {"status": "success", "message": "Please provide To Date."}

        if not leave_state["slots"].get("Reason"):
            reasons = get_leave_reasons(login)
            if not reasons:
                return {"status": "error", "message": "Unable to fetch leave reasons."}
            options = [{"label": r.get("Name") or r.get("TLeaveReasonName"), "value": r.get("Id") or r.get("TLeaveReasonId")} for r in reasons]
            leave_state["last_options"] = options
            leave_state["awaiting_field"] = "Reason"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {"status": "success", "message": f"Please select the reason for your leave from the options below:\n{options_text}\n\nReply with the number of your choice."}

        if not leave_state["slots"].get("TLeaveDayType"):
            options = [{"label": o["label"], "value": o["value"], "code": o["code"]} for o in LEAVE_DAY_TYPE_OPTIONS]
            leave_state["last_options"] = options
            leave_state["awaiting_field"] = "TLeaveDayType"
            options_text = "\n".join(f"{i}. {o['label']}" for i, o in enumerate(options))
            return {"status": "success", "message": f"Please select the Day Type:\n{options_text}\n\nReply with the number of your choice."}

        days = _calculate_days(leave_state["slots"]["FromDate"], leave_state["slots"]["ToDate"])

        if not days:
            return {"status": "success", "message": "Invalid date range. Ensure end date is after start date."}

        leave_state["slots"]["NumberOfDays"] = days

        result = apply_leave_flow(leave_state["slots"], login)

        if result.get("status") == "success":
            LEAVE_STATE.pop(state_key, None)

        return result

    # ========================================================
    # CONTINUE ACTIVE TIME SLIP FLOW
    # ========================================================

    if ts_state["intent"] == "apply":
        is_selection = False

        if "last_options" in ts_state:
            options = ts_state["last_options"]
            selected = None

            if message.isdigit():
                idx = int(message) - 1
                if 0 <= idx < len(options):
                    selected = options[idx]

            if not selected:
                labels = [o["label"].lower() for o in options]
                close = difflib.get_close_matches(message.lower(), labels, n=1, cutoff=0.7)
                if close:
                    selected = next((o for o in options if o["label"].lower() == close[0]), None)

            if selected:
                target_field = ts_state.get("awaiting_field")
                if target_field == "TimeSlipReason":
                    ts_state["slots"]["TimeSlipReason"] = selected["label"]

                is_selection = True
                ts_state.pop("last_options", None)
                ts_state.pop("awaiting_field", None)

        if not is_selection:
            awaiting = ts_state.get("awaiting_field")

            if awaiting == "TimeSlipDate":
                from utils.date_parser import parse_date
                parsed = parse_date(message.strip())
                if parsed:
                    ts_state["slots"]["TimeSlipDate"] = parsed
                    ts_state.pop("awaiting_field", None)

            elif awaiting == "FromTime":
                extracted = _extract_time(message)
                if extracted:
                    ts_state["slots"]["FromTime"] = extracted
                    ts_state.pop("awaiting_field", None)

            elif awaiting == "ToTime":
                extracted = _extract_time(message)
                if extracted:
                    ts_state["slots"]["ToTime"] = extracted
                    ts_state.pop("awaiting_field", None)

            else:
                ai = call_time_slip_chat(message)
                slots = normalize_time_slip_slots(ai["action"]["slots"])
                for k, v in slots.items():
                    if v:
                        ts_state["slots"][k] = v

        if not ts_state["slots"].get("TimeSlipDate"):
            ts_state["awaiting_field"] = "TimeSlipDate"
            return {"status": "success", "message": "Please provide the Time Slip date (for example, 02-01-2024)."}

        if not ts_state["slots"].get("FromTime"):
            ts_state["awaiting_field"] = "FromTime"
            return {"status": "success", "message": "Please provide From Time (for example, 09:00)."}

        if not ts_state["slots"].get("ToTime"):
            ts_state["awaiting_field"] = "ToTime"
            return {"status": "success", "message": "Please provide To Time (for example, 11:00)."}

        if not ts_state["slots"].get("TimeSlipReason"):
            reasons = get_time_slip_reasons(login)
            if not reasons:
                return {"status": "error", "message": "Unable to fetch permission reasons at the moment. Please try again later."}
            options = [{"label": r.get("Name") or r.get("TLeaveReasonName"), "value": r.get("Id") or r.get("TLeaveReasonId")} for r in reasons]
            ts_state["last_options"] = options
            ts_state["awaiting_field"] = "TimeSlipReason"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {"status": "success", "message": f"Please select the permission reason from the options below:\n{options_text}\n\nReply with the number of your choice."}

        result = apply_time_slip_flow(ts_state["slots"], login)

        if result.get("status") == "success":
            TIME_SLIP_STATE.pop(state_key, None)

        return result

    # ========================================================
    # CONTINUE ACTIVE ATTENDANCE FLOW
    # ========================================================

    if attendance_state["intent"] == "get":
        is_selection = False

        if "last_options" in attendance_state:
            options = attendance_state["last_options"]
            selected = None

            if message.isdigit():
                idx = int(message) - 1
                if 0 <= idx < len(options):
                    selected = options[idx]

            if not selected:
                labels = [o["label"].lower() for o in options]
                close = difflib.get_close_matches(message.lower(), labels, n=1, cutoff=0.7)
                if close:
                    selected = next((o for o in options if o["label"].lower() == close[0]), None)

            if selected:
                record = selected.get("record", {})
                attendance_state["selected_payperiod"] = record if isinstance(record, dict) else {}
                attendance_state["slots"]["PayPeriodName"] = selected["label"]
                attendance_state["slots"]["PayPeriodId"] = str(attendance_state["selected_payperiod"].get("id", ""))
                is_selection = True
                attendance_state.pop("last_options", None)
                attendance_state.pop("awaiting_field", None)

        if not is_selection:
            ai = call_attendance_chat(message)
            slots = normalize_attendance_slots(ai.get("action", {}).get("slots", {}))
            if slots.get("PayPeriodName"):
                attendance_state["slots"]["PayPeriodName"] = slots["PayPeriodName"]
            if slots.get("PayPeriodId"):
                attendance_state["slots"]["PayPeriodId"] = str(slots["PayPeriodId"])

        if not attendance_state.get("selected_payperiod"):
            payperiods = get_pay_periods(login)
            if not payperiods:
                return {"status": "error", "message": "Unable to fetch pay periods at the moment. Please try again later."}

            normalized_payperiods = [normalize_payperiod_record(p) for p in payperiods]
            options = _build_options(normalized_payperiods, label_keys=("name",), value_keys=("id",))
            attendance_state["last_options"] = options
            attendance_state["awaiting_field"] = "PayPeriod"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {"status": "success", "message": f"Please select the PayPeriod from the options below:\n{options_text}\n\nReply with the number of your choice."}

        selected_payperiod = attendance_state["selected_payperiod"]
        if not selected_payperiod.get("fromDate") or not selected_payperiod.get("toDate"):
            attendance_state.pop("selected_payperiod", None)
            return {"status": "error", "message": "Unable to read the selected PayPeriod dates. Please try selecting the PayPeriod again."}

        try:
            result = get_daily_attendance(login, selected_payperiod)
            if isinstance(result, dict) and result.get("status_code", 200) >= 400:
                error_body = result.get("body") or result.get("raw_body") or {}
                if isinstance(error_body, (dict, list)):
                    error_text = json.dumps(error_body, indent=2, ensure_ascii=False, default=str)
                else:
                    error_text = str(error_body)
                used_dates = result.get("used_dates") or {}
                request_payload = result.get("request_payload")
                debug_bits = []
                if used_dates:
                    debug_bits.append(f"used_dates={json.dumps(used_dates, default=str)}")
                if request_payload:
                    debug_bits.append(f"request_payload={json.dumps(request_payload, indent=2, ensure_ascii=False, default=str)}")
                if debug_bits:
                    error_text = f"{error_text}\n\n" + "\n".join(debug_bits)
                if not error_text.strip():
                    error_text = "Failed to fetch daily attendance."
                return {"status": "error", "message": f"Failed to fetch daily attendance: {error_text}"}

            attendance_body = result.get("body") if isinstance(result, dict) else result
            attendance_view = build_attendance_view(attendance_body)
            formatted_attendance = attendance_view.get("message", "")
            attendance_data = attendance_view.get("data", {})
            if formatted_attendance:
                ATTENDANCE_STATE.pop(state_key, None)
                return {"status": "success", "message": formatted_attendance, "attendanceData": attendance_data}

            decoded_body = _extract_decoded_service_body(result)
            body_text = _format_service_body(decoded_body)
            message_text = body_text if body_text else "Daily attendance retrieved successfully."

            ATTENDANCE_STATE.pop(state_key, None)
            return {"status": "success", "message": message_text, "attendanceData": attendance_data}
        except Exception as e:
            error_msg = str(e)
            if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
                return {"status": "error", "message": "Server is taking too long to respond. Please try again later."}
            return {"status": "error", "message": f"Failed to fetch daily attendance: {error_msg}"}

    # ========================================================
    # CONTINUE ACTIVE PURCHASE FLOW
    # ========================================================

    if purchase_state["intent"] == "create":
        is_selection = False

        if "last_options" in purchase_state:
            options = purchase_state["last_options"]
            selected = None

            if message.isdigit():
                idx = int(message) - 1
                if 0 <= idx < len(options):
                    selected = options[idx]

            if not selected:
                labels = [o["label"].lower() for o in options]
                close = difflib.get_close_matches(message.lower(), labels, n=1, cutoff=0.7)
                if close:
                    selected = next((o for o in options if o["label"].lower() == close[0]), None)

            if selected:
                target_field = purchase_state.get("awaiting_field")
                record = selected.get("record", {})

                if target_field == "Party":
                    purchase_state["slots"]["PartyName"] = selected["label"]
                    purchase_state["slots"]["PartyId"] = str(_first_non_empty(record, "Id", "PartyId", default=""))
                    purchase_state["slots"]["PartyCode"] = str(_first_non_empty(record, "Code", "PartyCode", default=""))
                    purchase_state["slots"]["PartyBranchId"] = str(_first_non_empty(record, "PartyBranchId", default="-1"))
                elif target_field == "Item":
                    purchase_state["slots"]["ItemName"] = selected["label"]
                    purchase_state["slots"]["ItemId"] = str(_first_non_empty(record, "Id", "ItemId", default=""))
                    purchase_state["slots"]["ItemCode"] = str(_first_non_empty(record, "Code", "ItemCode", default=""))
                elif target_field == "Store":
                    purchase_state["slots"]["StoreName"] = selected["label"]
                    store_id = str(_first_non_empty(record, "Id", "StoreId", default=""))
                    if store_id:
                        purchase_state["slots"]["StoreId"] = store_id
                        purchase_state["slots"]["FromStoreId"] = store_id

                is_selection = True
                purchase_state.pop("last_options", None)
                purchase_state.pop("awaiting_field", None)

        if not is_selection:
            ai = call_purchase_chat(message)
            slots = normalize_purchase_slots(ai.get("action", {}).get("slots", {}))
            for k, v in slots.items():
                if v not in (None, ""):
                    purchase_state["slots"][k] = v

        if purchase_state["slots"].get("PartyName") and not purchase_state["slots"].get("PartyId"):
            party = _best_match_record(
                get_parties(login), purchase_state["slots"]["PartyName"],
                name_keys=("Name", "PartyName"), code_keys=("Code", "PartyCode"), id_keys=("Id", "PartyId")
            )
            if party:
                purchase_state["slots"]["PartyName"] = str(_first_non_empty(party, "Name", "PartyName", default="")).strip()
                purchase_state["slots"]["PartyId"] = str(_first_non_empty(party, "Id", "PartyId", default=""))
                purchase_state["slots"]["PartyCode"] = str(_first_non_empty(party, "Code", "PartyCode", default=""))
                purchase_state["slots"]["PartyBranchId"] = str(_first_non_empty(party, "PartyBranchId", default="-1"))

        if purchase_state["slots"].get("ItemName") and not purchase_state["slots"].get("ItemId"):
            item = _best_match_record(
                get_items(login), purchase_state["slots"]["ItemName"],
                name_keys=("Name", "ItemName"), code_keys=("Code", "ItemCode"), id_keys=("Id", "ItemId")
            )
            if item:
                purchase_state["slots"]["ItemName"] = str(_first_non_empty(item, "Name", "ItemName", default="")).strip()
                purchase_state["slots"]["ItemId"] = str(_first_non_empty(item, "Id", "ItemId", default=""))
                purchase_state["slots"]["ItemCode"] = str(_first_non_empty(item, "Code", "ItemCode", default=""))

        if purchase_state["slots"].get("StoreName") and not purchase_state["slots"].get("StoreId"):
            store = _best_match_record(
                get_stores(login), purchase_state["slots"]["StoreName"],
                name_keys=("Name", "StoreName"), code_keys=("Code", "StoreCode"), id_keys=("Id", "StoreId")
            )
            if store:
                purchase_state["slots"]["StoreName"] = str(_first_non_empty(store, "Name", "StoreName", default="")).strip()
                store_id = str(_first_non_empty(store, "Id", "StoreId", default=""))
                if store_id:
                    purchase_state["slots"]["StoreId"] = store_id
                    purchase_state["slots"]["FromStoreId"] = store_id

        if not purchase_state["slots"].get("PartyId"):
            parties = get_parties(login)
            if not parties:
                return {"status": "error", "message": "Unable to fetch purchase parties at the moment. Please try again later."}
            options = _build_options(parties, label_keys=("Name", "PartyName"), value_keys=("Id", "PartyId"), code_keys=("Code", "PartyCode"))
            purchase_state["last_options"] = options
            purchase_state["awaiting_field"] = "Party"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {"status": "success", "message": f"Please select the party from the options below:\n{options_text}\n\nReply with the number of your choice."}

        if not purchase_state["slots"].get("ItemId"):
            items = get_items(login)
            if not items:
                return {"status": "error", "message": "Unable to fetch purchase items at the moment. Please try again later."}
            options = _build_options(items, label_keys=("Name", "ItemName"), value_keys=("Id", "ItemId"), code_keys=("Code", "ItemCode"))
            purchase_state["last_options"] = options
            purchase_state["awaiting_field"] = "Item"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {"status": "success", "message": f"Please select the item from the options below:\n{options_text}\n\nReply with the number of your choice."}

        if not purchase_state["slots"].get("StoreId"):
            stores = get_stores(login)
            if not stores:
                return {"status": "error", "message": "Unable to fetch purchase stores at the moment. Please try again later."}
            options = _build_options(stores, label_keys=("Name", "StoreName"), value_keys=("Id", "StoreId"), code_keys=("Code", "StoreCode"))
            purchase_state["last_options"] = options
            purchase_state["awaiting_field"] = "Store"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {"status": "success", "message": f"Please select the store from the options below:\n{options_text}\n\nReply with the number of your choice."}

        if not purchase_state["slots"].get("Quantity"):
            return {"status": "success", "message": "Please provide the purchase quantity."}

        if not purchase_state["slots"].get("Rate"):
            return {"status": "success", "message": "Please provide the purchase rate."}

        try:
            float(purchase_state["slots"].get("Quantity", 0))
            float(purchase_state["slots"].get("Rate", 0))
        except Exception:
            return {"status": "success", "message": "Quantity and Rate must be numeric. Please provide valid values."}

        try:
            result = create_purchase_order(purchase_state["slots"], login)
            PURCHASE_STATE.pop(state_key, None)
            return {"status": "success", "message": "Purchase order created successfully."}
        except Exception as e:
            return {"status": "error", "message": f"Failed to create purchase order: {e}"}

    # ========================================================
    # CONTINUE ACTIVE TASK FLOW
    # ========================================================

    if task_state["intent"] == "create":
        is_selection = False

        awaiting_simple_number = task_state.get("awaiting_field")
        # Activity picklist (last_options) takes priority — a digit reply when last_options
        # is present is always a picklist index, not a ProjectSequence/ResourceSequence value.
        if awaiting_simple_number in ("ProjectSequence", "ResourceSequence", "PlanDays") \
                and message.strip().isdigit() and "last_options" not in task_state:
            task_state["slots"][awaiting_simple_number] = message.strip()
            task_state.pop("awaiting_field", None)
            is_selection = True

        if not is_selection and "last_options" in task_state:
            options = task_state["last_options"]
            selected = None

            if message.isdigit():
                idx = int(message) - 1
                if 0 <= idx < len(options):
                    selected = options[idx]

            if not selected:
                labels = [o["label"].lower() for o in options]
                close = difflib.get_close_matches(message.lower(), labels, n=1, cutoff=0.7)
                if close:
                    selected = next((o for o in options if o["label"].lower() == close[0]), None)

            if selected:
                target_field = task_state.get("awaiting_field")
                if target_field == "Activity":
                    task_state["slots"]["Activity"] = selected["label"]

                is_selection = True
                task_state.pop("last_options", None)
                task_state.pop("awaiting_field", None)

        if not is_selection:
            ai = call_task_chat(message, login)
            action = ai.get("action", {})

            if action.get("intent") == "none" and not task_state["slots"].get("WorkNumber"):
                response_text = ai.get("response", "Please provide a valid Work Number.")
                is_rejection = any(w in response_text.lower() for w in
                                    ["couldn't", "doesn't", "not found", "not been approved", "no session"])
                return {
                    "status": "error" if is_rejection else "success",
                    "message": response_text
                }

            new_slots = normalize_task_slots(action.get("slots", {}))
            for k, v in new_slots.items():
                if v:
                    task_state["slots"][k] = v

            # Inline regex rescue: if the user said "Activity None/none/N/A" but the LLM
            # left the Activity slot empty, catch it here so we don't re-ask every time.
            if not task_state["slots"].get("Activity"):
                import re as _re
                if _re.search(r"\bactivity\s*(none|no|n/?a|null)\b", message, _re.IGNORECASE):
                    task_state["slots"]["Activity"] = "NONE"

            if task_state["slots"].get("WorkNumber") and not task_state.get("work_id"):
                from task_bot.task_agent import _get_validated_work
                cached = _get_validated_work()
                if (cached and cached.get("valid")
                        and cached.get("WorkNumber", "").upper()
                        == task_state["slots"]["WorkNumber"].upper()):
                    task_state["requester"] = cached.get("requester", "")
                    task_state["allocation_split"] = cached.get("allocation_split", "")
                    task_state["work_id"] = cached.get("work_id")

        # Collect ALL missing required fields and ask for them together in one message.
        missing_parts = []

        if not task_state["slots"].get("WorkNumber"):
            missing_parts.append("Work Number (e.g. W0092618)")

        if not task_state["slots"].get("TaskName"):
            missing_parts.append("Task Name")

        if not task_state["slots"].get("Description"):
            missing_parts.append("Description")

        if not task_state["slots"].get("Activity"):
            if not ACTIVITY_LIST:
                return {"status": "error", "message": "Activity list isn't configured yet — add ACTIVITY_LIST to criteria.json."}
            options = [{"label": a, "value": a} for a in ACTIVITY_LIST]
            task_state["last_options"] = options
            task_state["awaiting_field"] = "Activity"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            if missing_parts:
                missing_parts_text = ", ".join(missing_parts)
                return {"status": "success", "message": f"Still need: {missing_parts_text}. Also, please select the Activity:\n{options_text}\n\nReply with the number of your choice."}
            return {"status": "success", "message": f"Please select the Activity from the options below:\n{options_text}\n\nReply with the number of your choice."}

        if not task_state["slots"].get("ProjectSequence"):
            missing_parts.append("Project Sequence")

        if not task_state["slots"].get("ResourceSequence"):
            missing_parts.append("Resource Sequence")

        if not task_state["slots"].get("PlanDays"):
            missing_parts.append("Plan Days (default is 1 — reply '1' if unsure)")

        if not task_state["slots"].get("Hours"):
            missing_parts.append("Hours (HH:MM format, max 08:00 — e.g. 08:00 or 07:30)")

        if missing_parts:
            task_state["awaiting_field"] = (
                "ProjectSequence" if not task_state["slots"].get("ProjectSequence") else
                "ResourceSequence" if not task_state["slots"].get("ResourceSequence") else
                "PlanDays" if not task_state["slots"].get("PlanDays") else
                None
            )
            missing_text = ", ".join(missing_parts)
            return {"status": "success", "message": f"Please provide the following: {missing_text}"}

        hours_minutes = _parse_hhmm_to_minutes(task_state["slots"]["Hours"])
        if hours_minutes is None or hours_minutes > MAX_HOURS_PER_TASK_MIN:
            task_state["slots"].pop("Hours", None)
            return {"status": "success", "message": "Hours must be in HH:MM format and can't exceed 08:00. Please re-enter."}

        if not task_state["slots"].get("TaskDate"):
            return {"status": "success", "message": "What date is this task for? (e.g. 21-07-2026). This date will be used for Start On, End On, Due On, and Expiry On."}

        task_dt = _task_parse_date(task_state["slots"]["TaskDate"])
        if not task_dt:
            task_state["slots"].pop("TaskDate", None)
            return {"status": "success", "message": "That date wasn't recognized. Please provide it again (e.g. 20-07-2026)."}

        try:
            daily_check = check_daily_hours_available(login, task_dt, hours_minutes)
        except Exception:
            return {"status": "error", "message": "Couldn't verify today's already-logged hours. Please try again."}
        if not daily_check.get("ok"):
            task_state["slots"].pop("Hours", None)
            return {"status": "error", "message": daily_check.get("reason", "Daily hour limit exceeded.")}

        result = apply_task_flow(task_state, login)

        if result.get("status") == "success":
            TASK_STATE.pop(state_key, None)

        return result

    return {
        "status": "success",
        "message": "I can help you apply Leave, submit a Time Slip, view Daily Attendance, create a Purchase Order, create a Task, or create a Pack."
    }
