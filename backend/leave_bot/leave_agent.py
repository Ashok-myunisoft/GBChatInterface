import json
import re
from datetime import datetime
from utils.date_parser import parse_date
from config import settings
from pack_bot.agent import get_llm

try:
    from langchain_core.messages import SystemMessage, HumanMessage
except Exception:
    from langchain_community.chat_models import ChatOpenAI
    from langchain_core.messages import SystemMessage, HumanMessage

# ============================================================
# SYSTEM PROMPT
# ============================================================

LEAVE_SYSTEM_PROMPT = """
You are the GoodBooks Leave Assistant.
Your job is to classify user messages into leave-related actions and extract leave information.

You MUST ALWAYS return a JSON object with this structure:

{
  "response": "<natural language reply to user>",
  "action": {
    "intent": "apply | cancel | get | update | none",
    "slots": {
      "EmployeeName": "",
      "LeaveType": "",
      "LeaveTypeId": "",
      "FromDate": "",
      "ToDate": "",
      "NumberOfDays": "",
      "Reason": "",
      "ReasonId": "",
      "LeaveId": 0
    }
  }
}

================ RULES ================
1. Never guess or invent slot values. If the user does not provide something, leave it as empty string or zero.
2. Intent classification:
   - "apply", "request", "take leave" → intent = apply
   - "cancel", "withdraw" → intent = cancel
   - "show", "list", "get" → intent = get
   - "update", "change", "modify" → intent = update
   - Simple greetings → intent = none
3. The "response" field should contain a short friendly assistant reply.
4. Never include explanation, markdown, code blocks, or extra text outside the JSON.
"""

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

    # Inject Current Date for context
    current_date_str = datetime.now().strftime("%A, %d-%m-%Y")
    system_prompt_with_date = f"{LEAVE_SYSTEM_PROMPT}\n\nCurrent Date: {current_date_str}"

    messages = [
        SystemMessage(content=system_prompt_with_date),
        HumanMessage(content=message)
    ]

    try:
        raw = llm.invoke(messages)
        result = raw.content.strip()
    except Exception:
        return {
            "response": "I can help you with leave.",
            "action": regex_extract(message)
        }

    # Remove ```json wrappers if any
    result = re.sub(r"^```json|```$", "", result).strip()

    # Safe JSON parsing
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

    # Defensive normalization
    data.setdefault("action", {})
    action = data["action"]
    slots = action.setdefault("slots", {})

    for key in ["EmployeeName", "LeaveType", "LeaveTypeId", "FromDate", "ToDate", "NumberOfDays", "Reason", "ReasonId"]:
        slots.setdefault(key, "")
    slots.setdefault("LeaveId", 0)

    return data


# ============================================================
# REGEX FALLBACK (NO LLM / FAIL SAFE)
# ============================================================

def regex_extract(text: str):
    t = text.lower()

    # -------- Intent --------
    if re.search(r"\b(cancel|withdraw)\b", t):
        intent = "cancel"
    elif re.search(r"\b(apply|request|take leave)\b", t):
        intent = "apply"
    elif re.search(r"\b(update|change|modify)\b", t):
        intent = "update"
    elif re.search(r"\b(show|list|get)\b", t):
        intent = "get"
    else:
        intent = "none"

    slots = {
        "EmployeeName": "",
        "LeaveType": "",
        "LeaveTypeId": "",
        "FromDate": "",
        "ToDate": "",
        "NumberOfDays": "",
        "Reason": "",
        "ReasonId": "",
        "LeaveId": 0
    }

    return {"intent": intent, "slots": slots}

# ============================================================
# NORMALIZATION (FINAL â€“ SINGLE SOURCE OF TRUTH)
# ============================================================

def normalize_leave_slots(s: dict):
    from_date = parse_date(s.get("FromDate", ""))
    to_date = parse_date(s.get("ToDate", ""))

    return {
        "EmployeeName": s.get("EmployeeName", "").strip(),
        "LeaveType": s.get("LeaveType", "").strip(),
        "LeaveTypeId": s.get("LeaveTypeId", ""),
        "FromDate": from_date or "",
        "ToDate": to_date or "",
        "NumberOfDays": s.get("NumberOfDays", "").strip(),
        "Reason": s.get("Reason", "").strip(),
        "ReasonId": s.get("ReasonId", ""),
        "LeaveId": int(s.get("LeaveId", 0)) if s.get("LeaveId") else 0
    }
