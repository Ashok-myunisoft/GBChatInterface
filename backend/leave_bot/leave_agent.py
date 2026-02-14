import json
import re
from typing import Dict

from config import settings
from leave_bot.leave_prompt import LEAVE_SYSTEM_PROMPT

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage


# ============================================================
# LLM INITIALIZATION (OPENAI)
# ============================================================

_llm = None


def get_llm():
    """
    Returns a singleton ChatOpenAI client.
    """
    global _llm
    try:
        if not _llm:
            _llm = ChatOpenAI(
                model=settings.OPENAI_MODEL,
                temperature=0.3
            )
        return _llm
    except Exception as e:
        print("LLM load failed:", e)
        return None


def clean_llm_response(res):
    if hasattr(res, "content"):
        return res.content
    return str(res)


# ============================================================
# DATE CLEANER (CRITICAL FIX – KEPT)
# ============================================================

def _clean_date(val: str) -> str:
    """
    Extract clean date (DD-MM-YY / DD-MM-YYYY) from messy text
    Also handles "jan 2" style dates
    """
    if not val:
        return ""
    
    val = val.lower().strip()
    
    # Check for DD-MM-YYYY or DD-MM-YY
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", val)
    if m:
        day, month, year = m.groups()
        if len(year) == 2:
            year = "20" + year
        return f"{int(day):02d}-{int(month):02d}-{year}"

    # Check for "Jan 2" or "2 Jan"
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }
    
    # Style: "Jan 2" or "January 2"
    m = re.search(r"([a-z]{3,})\s*(\d{1,2})", val)
    if m:
        month_str, day = m.groups()
        month_key = month_str[:3].lower()
        if month_key in months:
            from datetime import datetime
            year = datetime.now().year
            return f"{int(day):02d}-{months[month_key]:02d}-{year}"

    # Style: "2 Jan" or "2nd January"
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s*([a-z]{3,})", val)
    if m:
        day, month_str = m.groups()
        month_key = month_str[:3].lower()
        if month_key in months:
            from datetime import datetime
            year = datetime.now().year
            return f"{int(day):02d}-{months[month_key]:02d}-{year}"

    return ""


# ============================================================
# MAIN LEAVE INTENT RESOLVER
# ============================================================

def call_leave_chat(message: str):
    """
    Leave Intent Resolver
    """

    llm = get_llm()

    # ----------- Fallback if LLM not available -----------
    if not llm:
        return {
            "response": "I can help you with leave.",
            "action": regex_extract(message)
        }

    messages = [
        SystemMessage(content=LEAVE_SYSTEM_PROMPT),
        HumanMessage(content=message)
    ]

    # ----------- LLM Call -----------
    try:
        raw = llm.invoke(messages)
        result = clean_llm_response(raw)
    except Exception as e:
        return {
            "response": f"LLM call failed: {e}",
            "action": regex_extract(message)
        }

    # ----------- Clean response -----------
    result = re.sub(r"^```json|```$", "", result).strip()

    # ----------- Safe JSON parse -----------
    try:
        data = json.loads(result)
    except Exception:
        try:
            json_block = re.search(r"\{[\s\S]*\}", result).group()
            data = json.loads(json_block)
        except Exception:
            return {
                "response": result,
                "action": regex_extract(message)
            }

    # ----------- Ensure structure -----------
    data.setdefault("action", {})
    action = data["action"]
    slots = action.setdefault("slots", {})

    for k in [
        "EmployeeName",
        "LeaveType",
        "FromDate",
        "ToDate",
        "NumberOfDays",
        "Reason",
        "LeaveId"
    ]:
        slots.setdefault(k, "" if k != "LeaveId" else 0)

    return data


# ============================================================
# REGEX FALLBACK (LLM FAIL SAFE)
# ============================================================

def regex_extract(text: str):
    t = text.lower()

    # ---------------- INTENT ----------------
    if "cancel" in t or "withdraw" in t:
        intent = "cancel"
    elif "status" in t or "check" in t:
        intent = "status"
    elif "leave" in t or "apply" in t:
        intent = "apply"
    else:
        intent = "none"

    # ---------------- SLOTS ----------------
    slots = {
        "EmployeeName": "",
        "LeaveType": "",
        "FromDate": "",
        "ToDate": "",
        "NumberOfDays": "",
        "Reason": "",
        "LeaveId": 0
    }

    # ---------------- LEAVE TYPE ----------------
    if "sick" in t:
        slots["LeaveType"] = "Sick Leave"
    elif "casual" in t:
        slots["LeaveType"] = "Casual Leave"
    elif "loss of pay" in t or "lop" in t:
        slots["LeaveType"] = "Loss of Pay"

    # ---------------- FROM DATE ----------------
    m = re.search(r"from\s+(\d{2}[-/]\d{2}[-/]\d{2,4})", t)
    if m:
        slots["FromDate"] = m.group(1)

    # ---------------- TO DATE ----------------
    m = re.search(r"to\s+(\d{2}[-/]\d{2}[-/]\d{2,4})", t)
    if m:
        slots["ToDate"] = m.group(1)

    # ---------------- NUMBER OF DAYS ----------------
    m = re.search(r"(\d+)\s*(day|days)", t)
    if m:
        slots["NumberOfDays"] = m.group(1)

    # ---------------- REASON ----------------
    m = re.search(r"because\s+(.*)", text, re.I)
    if m:
        slots["Reason"] = m.group(1).strip()

    # ---------------- LEAVE ID ----------------
    m = re.search(r"(leave\s*id|reference)[\s:]*([0-9]+)", t)
    if m:
        slots["LeaveId"] = int(m.group(2))

    return {
        "intent": intent,
        "slots": slots
    }


# ============================================================
# NORMALIZATION (FINAL – SINGLE SOURCE OF TRUTH)
# ============================================================

def normalize_leave_slots(s: Dict):
    return {
        "EmployeeName": s.get("EmployeeName", "").strip(),
        "LeaveType": s.get("LeaveType", "").strip(),
        "LeaveTypeId": s.get("LeaveTypeId", ""),
        "FromDate": _clean_date(s.get("FromDate", "")),
        "ToDate": _clean_date(s.get("ToDate", "")),
        "NumberOfDays": s.get("NumberOfDays", "").strip(),
        "Reason": s.get("Reason", "").strip(),
        "ReasonId": s.get("ReasonId", ""),
        "LeaveId": int(s.get("LeaveId", 0)) if s.get("LeaveId") else 0
    }
