import re
import json
import base64
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings

# ---------------- PACK ----------------
from pack_bot.agent import call_ollama_chat, normalize_slots
from pack_bot.pack_client import save_pack, get_gb_timestamp

# ---------------- LEAVE ----------------
from leave_bot.leave_agent import call_leave_chat, normalize_leave_slots
from leave_bot.leave_client import (
    apply_leave, 
    get_leave_types_with_fallback as get_leave_types, 
    get_leave_reasons
)

# ---------------- TIME SLIP ----------------
from time_slip_bot.time_slip_agent import (
    call_time_slip_chat,
    normalize_time_slip_slots
)
from time_slip_bot.time_slip_client import (
    apply_time_slip,
    get_time_slip_reasons
)


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
        return json.loads(header)


class ChatRequest(BaseModel):
    message: str


# ============================================================
# CONVERSATION STATE
# ============================================================

PACK_STATE = {}
LEAVE_STATE = {}
TIME_SLIP_STATE = {}


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


# ============================================================
# INTENT RESOLUTION
# ============================================================

def resolve_intent(message: str) -> str:
    msg = message.lower()

    leave_score = 0
    ts_score = 0

    # -------- LEAVE SIGNALS --------
    if "leave" in msg:
        leave_score += 3
    if any(k in msg for k in ["sick", "casual", "lop"]):
        leave_score += 2
    if re.search(r"\b\d+\s*(day|days)\b", msg):
        leave_score += 2
    if re.search(r"from\s+\d{1,2}[-/]\d{1,2}", msg):
        leave_score += 2

    # -------- TIME SLIP SIGNALS --------
    if "permission" in msg or "time slip" in msg:
        ts_score += 3
    if _extract_time(msg):
        ts_score += 3
    if "today" in msg:
        ts_score += 1

    if leave_score > ts_score:
        return "leave"
    if ts_score > leave_score:
        return "time_slip"

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


def apply_leave_flow(slots, login):
    try:
        apply_leave(slots, login)
        return {
            "status": "success",
            "message": f"Leave applied successfully ✅ for {slots.get('EmployeeName')}"
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
        apply_time_slip(slots, login)
        return {
            "status": "success",
            "message": "Time Slip applied successfully ✅"
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
# CHAT ENDPOINT
# ============================================================

@app.post("/gbaiapi/chat_Interface")
async def chat(req: ChatRequest, Login: Optional[str] = Header(None)):
    login = parse_login(Login)
    user_id = str(login.get("UserId", "default"))
    message = req.message.strip()

    # Initialize States with 'awaiting' field for Pack
    PACK_STATE.setdefault(user_id, {"intent": None, "slots": {}, "awaiting": None})
    LEAVE_STATE.setdefault(user_id, {"intent": None, "slots": {}})
    TIME_SLIP_STATE.setdefault(user_id, {"intent": None, "slots": {}})

    pack_state = PACK_STATE[user_id]
    leave_state = LEAVE_STATE[user_id]
    ts_state = TIME_SLIP_STATE[user_id]

    # ========================================================
    # CONTINUE ACTIVE LEAVE FLOW
    # ========================================================

    if leave_state["intent"] == "apply":
        # 1. Handle Selection from SelectList
        is_selection = False
        
        if "last_options" in leave_state:
            options = leave_state["last_options"]
            selected = None
            
            # Check 1: User typed a Number (1, 2, 3...)
            if message.isdigit():
                idx = int(message) - 1
                if 0 <= idx < len(options):
                    selected = options[idx]

            # Check 2: User typed the Label (e.g. "Sick Leave")
            if not selected:
                selected = next((o for o in options if o["label"].lower() == message.lower()), None)

            # APPLY SELECTION
            if selected:
                target_field = leave_state.get("awaiting_field")
                
                if target_field == "LeaveType":
                    leave_state["slots"]["LeaveTypeId"] = str(selected["value"])
                    leave_state["slots"]["LeaveType"] = selected["label"]
                elif target_field == "Reason":
                    leave_state["slots"]["ReasonId"] = str(selected["value"])
                    leave_state["slots"]["Reason"] = selected["label"]
                
                is_selection = True
                leave_state.pop("last_options", None)
                leave_state.pop("awaiting_field", None)

        if not is_selection:
            # Check if the current message is the reason (not a date)
            has_date_pattern = bool(re.search(r"\d{1,2}[-/]\d{1,2}", message))
            
            if (leave_state["slots"].get("LeaveType") and 
                leave_state["slots"].get("FromDate") and 
                leave_state["slots"].get("ToDate") and 
                not leave_state["slots"].get("Reason") and 
                message and not has_date_pattern):
                leave_state["slots"]["Reason"] = message.strip()
            else:
                ai = call_leave_chat(message)
                slots = normalize_leave_slots(ai["action"]["slots"])
                for k, v in slots.items():
                    if v:
                        leave_state["slots"][k] = v

        # Default employee name
        leave_state["slots"].setdefault("EmployeeName", login.get("UserName"))

        # ---------------- REQUIRED FIELDS WITH SELECTLIST ----------------

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
            return {
                "status": "success",
                "message": f"Please select Leave Type from the options below:\n{options_text}\n\nReply with the number of your choice."
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

        # ---------------- CALCULATE DAYS ----------------
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

        # ---------------- APPLY LEAVE ----------------
        result = apply_leave_flow(leave_state["slots"], login)
        
        if result.get("status") == "success":
            LEAVE_STATE.pop(user_id, None)
        
        return result

    # ========================================================
    # CONTINUE ACTIVE TIME SLIP FLOW
    # ========================================================

    if ts_state["intent"] == "apply":
        # 1. Handle Selection from SelectList (Time Slip Reasons)
        is_selection = False
        
        if "last_options" in ts_state:
            options = ts_state["last_options"]
            selected = None
            
            # Check 1: User typed a Number (1, 2, 3...)
            if message.isdigit():
                idx = int(message) - 1
                if 0 <= idx < len(options):
                    selected = options[idx]

            # Check 2: User typed the Label
            if not selected:
                selected = next((o for o in options if o["label"].lower() == message.lower()), None)

            # APPLY SELECTION
            if selected:
                target_field = ts_state.get("awaiting_field")
                if target_field == "TimeSlipReason":
                    # We store the selected label as the reason name 
                    # The client side will resolve ID based on Name or we pass ID if client supports it
                    # But client currently resolves by matching Name again in apply_time_slip 
                    # OR fallback to map_leave_reason. 
                    # To be robust, we'll store the Label.
                    ts_state["slots"]["TimeSlipReason"] = selected["label"]
                
                is_selection = True
                ts_state.pop("last_options", None)
                ts_state.pop("awaiting_field", None)

        if not is_selection:
            has_time_pattern = bool(_extract_time(message))
            
            if (ts_state["slots"].get("TimeSlipDate") and 
                ts_state["slots"].get("FromTime") and 
                ts_state["slots"].get("ToTime") and 
                not ts_state["slots"].get("TimeSlipReason") and 
                message and not has_time_pattern):
                ts_state["slots"]["TimeSlipReason"] = message.strip()
            else:
                ai = call_time_slip_chat(message)
                slots = normalize_time_slip_slots(ai["action"]["slots"])
                ts_state["slots"].update({k: v for k, v in slots.items() if v})

        ts_state["slots"]["EmployeeId"] = login.get("UserId")
        ts_state["slots"]["EmployeeName"] = login.get("UserName")
        ts_state["slots"]["EmployeeCode"] = login.get("UserCode")

        if not ts_state["slots"].get("TimeSlipDate"):
            return {"status": "success", "message": "Please provide Time Slip Date."}

        if not ts_state["slots"].get("FromTime"):
            return {"status": "success", "message": "Please provide From Time (HH:MM)."}

        if not ts_state["slots"].get("ToTime"):
            return {"status": "success", "message": "Please provide To Time (HH:MM)."}

        if not ts_state["slots"].get("TimeSlipReason"):
            reasons = get_time_slip_reasons(login)
            if not reasons:
                return {
                    "status": "error",
                    "message": "Unable to fetch time slip reasons."
                }
            options = [{"label": r.get("Name") or r.get("ReasonName"), "value": r.get("Id") or r.get("ReasonId")} for r in reasons]
            ts_state["last_options"] = options
            ts_state["awaiting_field"] = "TimeSlipReason"
            options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
            
            return {
                "status": "success",
                "message": f"Please select the reason for the Time Slip from the options below:\n{options_text}\n\nReply with the number of your choice."
            }

        result = apply_time_slip_flow(ts_state["slots"], login)
        if result.get("status") == "success":
            TIME_SLIP_STATE.pop(user_id, None)
        return result

    # ========================================================
    # CONTINUE ACTIVE PACK FLOW
    # ========================================================

    if pack_state["intent"] == "create":
        # 1. Attempt to extract slots using AI
        ai = call_ollama_chat(message)
        slots = normalize_slots(ai["action"]["slots"])
        pack_state["slots"].update({k: v for k, v in slots.items() if v})

        # 2. Check and ask for Pack Name
        if not pack_state["slots"].get("PackName"):
            # If we were explicitly waiting for Name, accept this message as the name
            if pack_state.get("awaiting") == "PackName":
                pack_state["slots"]["PackName"] = message.strip()
                pack_state["awaiting"] = None # Reset
            else:
                pack_state["awaiting"] = "PackName"
                return {"status": "success", "message": "Please provide Pack Name."}

        # 3. Check and ask for Pack Code
        if pack_state["slots"].get("PackName") and not pack_state["slots"].get("PackCode"):
            # If we were explicitly waiting for Code, accept this message as the code
            if pack_state.get("awaiting") == "PackCode":
                pack_state["slots"]["PackCode"] = message.strip()
                pack_state["awaiting"] = None # Reset
            else:
                pack_state["awaiting"] = "PackCode"
                return {"status": "success", "message": "Please provide Pack Code."}

        # 4. Final Execution - Only if both exist
        if pack_state["slots"].get("PackName") and pack_state["slots"].get("PackCode"):
            result = create_pack(pack_state["slots"], login)
            if result.get("status") == "success":
                PACK_STATE.pop(user_id, None)
            return result
        
        # Fallback safety (should not happen with logic above)
        return {"status": "success", "message": "Please provide Pack Code."}


    # ========================================================
    # NEW INTENT RESOLUTION
    # ========================================================

    intent = resolve_intent(message)

    if intent == "leave":
        leave_state["intent"] = "apply"
        
        # Fetch types immediately
        types = get_leave_types(login)
        if not types:
            return {
                "status": "error",
                "message": "Unable to fetch leave types from the service."
            }
        
        options = [{"label": t.get("Name") or t.get("TLeaveTypeName"), "value": t.get("Id") or t.get("TLeaveTypeId")} for t in types]
        
        leave_state["last_options"] = options
        leave_state["awaiting_field"] = "LeaveType"
        
        options_text = "\n".join(f"{i+1}. {o['label']}" for i, o in enumerate(options))
        return {
            "status": "success",
            "message": f"Sure 👍 Please select Leave Type from the options below:\n{options_text}\n\nReply with the number of your choice."
        }

    if intent == "time_slip":
        ts_state["intent"] = "apply"
        return {"status": "success", "message": "Sure 👍 Please provide Time Slip Date."}


    # ========================================================
    # PACK TRIGGER
    # ========================================================

    ai = call_ollama_chat(message)
    if ai["action"]["intent"] == "create":
        pack_state["intent"] = "create"
        pack_state["awaiting"] = "PackName"  # Set initial expectation
        return {"status": "success", "message": "Sure 👍 Please provide Pack Name."}

    return {
        "status": "success",
        "message": "Hello 👋 I can help you apply Leave, submit Time Slip, or create a Pack."
    }


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

    