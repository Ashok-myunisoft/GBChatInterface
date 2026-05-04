import re
import json
import base64
import time
import difflib
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from utils.holiday_checker import check_date_warning

# ---------------- PACK ----------------
from pack_bot.agent import call_ollama_chat, normalize_slots
from pack_bot.pack_client import save_pack, get_gb_timestamp

# ---------------- LEAVE ----------------
from leave_bot.leave_agent import call_leave_chat, normalize_leave_slots
from leave_bot.leave_client import (
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

# ---------------- PURCHASE ORDER ----------------
from purchase_bot.purchase_agent import call_purchase_chat, normalize_purchase_slots
from purchase_bot.purchase_client import create_purchase_order, get_parties, get_items, get_stores


# ============================================================
# FASTAPI SETUP
# ============================================================

app = FastAPI()

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
PURCHASE_STATE = {}
GREETED_USERS: set = set()

# Last-active timestamp per user (for TTL-based expiry)
_STATE_TS: dict = {}
STATE_TTL = 1800  # seconds — abandon after 30 min of inactivity

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
        PURCHASE_STATE.pop(uid, None)
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

# Maps keywords/aliases a user might say → canonical leave type name fragment
# Used for partial matching against the API's leave type name field
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
    """Return True if the message is asking about leave balance/availability."""
    msg = message.lower()
    words = re.findall(r'\b\w+\b', msg)
    has_leave = "leave" in msg or _fuzzy_in(words, "leave")
    if not has_leave:
        return False
    return (
        any(k in msg for k in _BALANCE_KEYWORDS) or
        any(_fuzzy_in(words, k) for k in _BALANCE_KEYWORDS)
    )


def _is_permission_balance_query(message: str) -> bool:
    """Return True if the message is asking about permission/time slip balance."""
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
    """
    If the user named a specific leave type (e.g. 'sick leave balance'),
    return the canonical name fragment to filter on (e.g. 'Sick Leave').
    Returns None if no specific type was mentioned.
    """
    msg = message.lower()
    # Check multi-word aliases first (e.g. "loss of pay", "comp off")
    for alias, canonical in _LEAVE_TYPE_ALIASES.items():
        if alias in msg:
            return canonical
    # Fuzzy single-word check
    words = re.findall(r'\b\w+\b', msg)
    for alias, canonical in _LEAVE_TYPE_ALIASES.items():
        if " " not in alias and _fuzzy_in(words, alias, cutoff=0.85):
            return canonical
    return None


def _format_balance_response(balances: list, filter_type: Optional[str] = None) -> str:
    """
    Format the leave balance API result into a readable message.
    If filter_type is given, show only the matching leave type.
    """
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


# ============================================================
# INTENT RESOLUTION
# ============================================================

def _fuzzy_in(words: list, keyword: str, cutoff: float = 0.82) -> bool:
    """Return True if any word in `words` closely matches `keyword`."""
    return bool(difflib.get_close_matches(keyword, words, n=1, cutoff=cutoff))


def resolve_intent(message: str) -> str:
    msg = message.lower()
    words = re.findall(r'\b\w+\b', msg)  # tokenize for fuzzy matching

    leave_score = 0
    ts_score = 0

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

    # -------- PURCHASE SIGNALS --------
    purchase_score = 0
    if any(k in msg for k in ["purchase", "po", "purchase order", "buy", "procure"]):
        purchase_score += 3
    if any(k in msg for k in ["vendor", "supplier", "party", "item", "material", "goods"]):
        purchase_score += 2

    if leave_score > ts_score and leave_score > purchase_score:
        return "leave"
    if ts_score > leave_score and ts_score > purchase_score:
        return "time_slip"
    if purchase_score > leave_score and purchase_score > ts_score:
        return "purchase"

    return "unknown"


# ============================================================
# BUSINESS FUNCTIONS
# ============================================================

def create_pack(slots, login):
    """
    Creates a pack. Requires explicit PackCode and PackName.
    No auto-generation logic here.
    """
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
    """Extract leave number from apply_leave API response."""
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


def _format_service_body(body) -> str:
    """Convert a raw service response into readable text for chat output."""
    if body in (None, "", [], {}):
        return ""
    try:
        return json.dumps(body, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(body)


def apply_leave_flow(slots, login):
    try:
        result = apply_leave(slots, login)
        leave_number = _extract_leave_number(result)
        number_text = f" (Leave No: {leave_number})" if leave_number else ""
        body_text = _format_service_body(result)
        message = f"Leave applied successfully ✅ for {slots.get('EmployeeName')}{number_text}"
        if body_text:
            message += f"\n\nService response:\n{body_text}"
        return {
            "status": "success",
            "message": message
        }
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            return {
                "status": "error",
                "message": "Server is taking too long to respond. Please try again later."
            }
        return {
            "status": "error",
            "message": f"Failed to apply leave: {error_msg}"
        }


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

        number_text = f" (Permission No: {ts_number})" if ts_number else ""
        body_text = _format_service_body(body)
        message = f"Permission applied successfully ✅{number_text}"
        if body_text:
            message += f"\n\nService response:\n{body_text}"
        elif body is None:
            message += "\n\nService response: No response body returned by service."
        return {
            "status": "success",
            "message": message
        }
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            return {
                "status": "error",
                "message": "Server is taking too long to respond. Please try again later."
            }
        return {
            "status": "error",
            "message": f"Failed to apply time slip: {error_msg}"
        }


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
    user_id = str(login.get("UserId", "default"))
    message = req.message.strip()

    # Clean up abandoned sessions, then refresh this user's timestamp
    _cleanup_expired()
    _STATE_TS[user_id] = time.time()

    # Initialize States with 'awaiting' field for Pack
    PACK_STATE.setdefault(user_id, {"intent": None, "slots": {}, "awaiting": None})
    LEAVE_STATE.setdefault(user_id, {"intent": None, "slots": {}})
    TIME_SLIP_STATE.setdefault(user_id, {"intent": None, "slots": {}})
    PURCHASE_STATE.setdefault(user_id, {"intent": None, "slots": {}})

    pack_state = PACK_STATE[user_id]
    leave_state = LEAVE_STATE[user_id]
    ts_state = TIME_SLIP_STATE[user_id]
    purchase_state = PURCHASE_STATE[user_id]

    # ========================================================
    # FIRST MESSAGE GREETING — show leave balance once per session
    # ========================================================

    if user_id not in GREETED_USERS:
        GREETED_USERS.add(user_id)
        try:
            balances = get_leave_balance(login)
            perm_records = get_time_slip_balance(login)

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

            greeting += "\n\nI can help you apply Leave, submit a Time Slip, or create a Pack."
            return {"status": "success", "message": greeting.strip()}

        except Exception:
            return {
                "status": "success",
                "message": "Hello! 👋 I can help you apply Leave, submit a Time Slip, or create a Pack."
            }

    # ========================================================
    # LEAVE BALANCE QUERY — handled immediately, no state change
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
            return {
                "status": "error",
                "message": "Unable to fetch leave balance at the moment. Please try again later."
            }

    if _is_permission_balance_query(message):
        try:
            records = get_time_slip_balance(login)
            return {
                "status": "success",
                "message": _format_permission_balance_response(records)
            }
        except Exception:
            return {
                "status": "error",
                "message": "Unable to fetch permission balance at the moment. Please try again later."
            }

    # ========================================================
    # ROUTE NEW REQUESTS INTO A FLOW WHEN NO ACTIVE STATE EXISTS
    # ========================================================

    if not any([
        pack_state.get("intent"),
        leave_state.get("intent"),
        ts_state.get("intent"),
        purchase_state.get("intent"),
    ]):
        root_intent = resolve_intent(message)
        if root_intent == "leave":
            leave_state["intent"] = "apply"
        elif root_intent == "time_slip":
            ts_state["intent"] = "apply"
        elif root_intent == "purchase":
            purchase_state["intent"] = "create"

    # ========================================================
    # CONTINUE ACTIVE LEAVE FLOW
    # ========================================================

    if leave_state["intent"] == "apply":
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
            has_date_pattern = bool(re.search(r"\d{1,2}[-/]\d{1,2}", message))

            if (
                leave_state["slots"].get("LeaveType")
                and leave_state["slots"].get("FromDate")
                and leave_state["slots"].get("ToDate")
                and not leave_state["slots"].get("Reason")
                and message
                and not has_date_pattern
            ):
                leave_state["slots"]["Reason"] = message.strip()
            else:
                ai = call_leave_chat(message)
                slots = normalize_leave_slots(ai["action"]["slots"])
                for k, v in slots.items():
                    if v:
                        leave_state["slots"][k] = v

        leave_state["slots"].setdefault("EmployeeName", login.get("UserName"))

        if not leave_state["slots"].get("LeaveType"):
            types = get_leave_types(login)
            if not types:
                return {
                    "status": "error",
                    "message": "Unable to fetch leave types at the moment. Please try again later."
                }
            options = [{"label": t.get("Name") or t.get("TLeaveTypeName"), "value": t.get("Id") or t.get("TLeaveTypeId")} for t in types]
            leave_state["last_options"] = options
            leave_state["awaiting_field"] = "LeaveType"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            warning = check_date_warning(leave_state["slots"].get("FromDate", ""))
            prefix = f"{warning}\n\n" if warning else ""
            return {
                "status": "success",
                "message": f"{prefix}Please select Leave Type from the options below:\n{options_text}\n\nReply with the number of your choice."
            }

        if not leave_state["slots"].get("FromDate"):
            return {
                "status": "success",
                "message": "Please provide From Date (e.g., Jan 2 or 02-01-2024)."
            }

        if not leave_state["slots"].get("ToDate"):
            return {
                "status": "success",
                "message": "Please provide To Date."
            }

        if not leave_state["slots"].get("Reason"):
            reasons = get_leave_reasons(login)
            if not reasons:
                return {
                    "status": "error",
                    "message": "Unable to fetch leave reasons."
                }
            options = [{"label": r.get("Name") or r.get("TLeaveReasonName"), "value": r.get("Id") or r.get("TLeaveReasonId")} for r in reasons]
            leave_state["last_options"] = options
            leave_state["awaiting_field"] = "Reason"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {
                "status": "success",
                "message": f"Please select the reason for your leave from the options below:\n{options_text}\n\nReply with the number of your choice."
            }

        if not leave_state["slots"].get("TLeaveDayType"):
            options = [{"label": o["label"], "value": o["value"], "code": o["code"]} for o in LEAVE_DAY_TYPE_OPTIONS]
            leave_state["last_options"] = options
            leave_state["awaiting_field"] = "TLeaveDayType"
            options_text = "\n".join(f"{i}. {o['label']}" for i, o in enumerate(options))
            return {
                "status": "success",
                "message": f"Please select the Day Type:\n{options_text}\n\nReply with the number of your choice."
            }

        days = _calculate_days(
            leave_state["slots"]["FromDate"],
            leave_state["slots"]["ToDate"]
        )

        if not days:
            return {
                "status": "success",
                "message": "Invalid date range. Ensure end date is after start date."
            }

        leave_state["slots"]["NumberOfDays"] = days

        result = apply_leave_flow(leave_state["slots"], login)

        if result.get("status") == "success":
            LEAVE_STATE.pop(user_id, None)

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
            return {
                "status": "success",
                "message": "Please provide the Time Slip date (for example, 02-01-2024)."
            }

        if not ts_state["slots"].get("FromTime"):
            ts_state["awaiting_field"] = "FromTime"
            return {
                "status": "success",
                "message": "Please provide From Time (for example, 09:00)."
            }

        if not ts_state["slots"].get("ToTime"):
            ts_state["awaiting_field"] = "ToTime"
            return {
                "status": "success",
                "message": "Please provide To Time (for example, 11:00)."
            }

        if not ts_state["slots"].get("TimeSlipReason"):
            reasons = get_time_slip_reasons(login)
            if not reasons:
                return {
                    "status": "error",
                    "message": "Unable to fetch permission reasons at the moment. Please try again later."
                }
            options = [{
                "label": r.get("Name") or r.get("TLeaveReasonName"),
                "value": r.get("Id") or r.get("TLeaveReasonId")
            } for r in reasons]
            ts_state["last_options"] = options
            ts_state["awaiting_field"] = "TimeSlipReason"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {
                "status": "success",
                "message": f"Please select the permission reason from the options below:\n{options_text}\n\nReply with the number of your choice."
            }

        result = apply_time_slip_flow(ts_state["slots"], login)

        if result.get("status") == "success":
            TIME_SLIP_STATE.pop(user_id, None)

        return result

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
                get_parties(login),
                purchase_state["slots"]["PartyName"],
                name_keys=("Name", "PartyName"),
                code_keys=("Code", "PartyCode"),
                id_keys=("Id", "PartyId")
            )
            if party:
                purchase_state["slots"]["PartyName"] = str(_first_non_empty(party, "Name", "PartyName", default="")).strip()
                purchase_state["slots"]["PartyId"] = str(_first_non_empty(party, "Id", "PartyId", default=""))
                purchase_state["slots"]["PartyCode"] = str(_first_non_empty(party, "Code", "PartyCode", default=""))
                purchase_state["slots"]["PartyBranchId"] = str(_first_non_empty(party, "PartyBranchId", default="-1"))

        if purchase_state["slots"].get("ItemName") and not purchase_state["slots"].get("ItemId"):
            item = _best_match_record(
                get_items(login),
                purchase_state["slots"]["ItemName"],
                name_keys=("Name", "ItemName"),
                code_keys=("Code", "ItemCode"),
                id_keys=("Id", "ItemId")
            )
            if item:
                purchase_state["slots"]["ItemName"] = str(_first_non_empty(item, "Name", "ItemName", default="")).strip()
                purchase_state["slots"]["ItemId"] = str(_first_non_empty(item, "Id", "ItemId", default=""))
                purchase_state["slots"]["ItemCode"] = str(_first_non_empty(item, "Code", "ItemCode", default=""))

        if purchase_state["slots"].get("StoreName") and not purchase_state["slots"].get("StoreId"):
            store = _best_match_record(
                get_stores(login),
                purchase_state["slots"]["StoreName"],
                name_keys=("Name", "StoreName"),
                code_keys=("Code", "StoreCode"),
                id_keys=("Id", "StoreId")
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
                return {
                    "status": "error",
                    "message": "Unable to fetch purchase parties at the moment. Please try again later."
                }
            options = _build_options(
                parties,
                label_keys=("Name", "PartyName"),
                value_keys=("Id", "PartyId"),
                code_keys=("Code", "PartyCode")
            )
            purchase_state["last_options"] = options
            purchase_state["awaiting_field"] = "Party"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {
                "status": "success",
                "message": f"Please select the party from the options below:\n{options_text}\n\nReply with the number of your choice."
            }

        if not purchase_state["slots"].get("ItemId"):
            items = get_items(login)
            if not items:
                return {
                    "status": "error",
                    "message": "Unable to fetch purchase items at the moment. Please try again later."
                }
            options = _build_options(
                items,
                label_keys=("Name", "ItemName"),
                value_keys=("Id", "ItemId"),
                code_keys=("Code", "ItemCode")
            )
            purchase_state["last_options"] = options
            purchase_state["awaiting_field"] = "Item"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {
                "status": "success",
                "message": f"Please select the item from the options below:\n{options_text}\n\nReply with the number of your choice."
            }

        if not purchase_state["slots"].get("StoreId"):
            stores = get_stores(login)
            if not stores:
                return {
                    "status": "error",
                    "message": "Unable to fetch purchase stores at the moment. Please try again later."
                }
            options = _build_options(
                stores,
                label_keys=("Name", "StoreName"),
                value_keys=("Id", "StoreId"),
                code_keys=("Code", "StoreCode")
            )
            purchase_state["last_options"] = options
            purchase_state["awaiting_field"] = "Store"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            return {
                "status": "success",
                "message": f"Please select the store from the options below:\n{options_text}\n\nReply with the number of your choice."
            }

        if not purchase_state["slots"].get("Quantity"):
            return {
                "status": "success",
                "message": "Please provide the purchase quantity."
            }

        if not purchase_state["slots"].get("Rate"):
            return {
                "status": "success",
                "message": "Please provide the purchase rate."
            }

        try:
            float(purchase_state["slots"].get("Quantity", 0))
            float(purchase_state["slots"].get("Rate", 0))
        except Exception:
            return {
                "status": "success",
                "message": "Quantity and Rate must be numeric. Please provide valid values."
            }

        try:
            result = create_purchase_order(purchase_state["slots"], login)
            PURCHASE_STATE.pop(user_id, None)
            return {
                "status": "success",
                "message": "Purchase order created successfully."
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to create purchase order: {e}"
            }

    return {
        "status": "success",
        "message": "I can help you apply Leave, submit a Time Slip, or create a Purchase Order."
    }
