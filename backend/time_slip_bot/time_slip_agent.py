import json
import re
from datetime import datetime
from utils.date_parser import parse_date
from pack_bot.agent import get_llm

try:
    from langchain_core.messages import SystemMessage, HumanMessage
except Exception:
    from langchain_core.messages import SystemMessage, HumanMessage

# ============================================================
# SYSTEM PROMPT
# ============================================================

TIME_SLIP_SYSTEM_PROMPT = """
You are the GoodBooks Time Slip Assistant.
Your job is to classify user messages into time slip actions and extract time slip information.

You MUST ALWAYS return a JSON object with this structure:

{
  "response": "<natural language reply to user>",
  "action": {
    "intent": "apply | cancel | get | update | none",
    "slots": {
      "TimeSlipDate": "",
      "FromTime": "",
      "ToTime": "",
      "Duration": "",
      "TimeSlipReason": "",
      "PermissionReason": "",
      "TimeSlipId": 0
    }
  }
}

================ RULES ================
1. Never guess or invent slot values. If the user does not provide something, leave it as empty string or zero.
2. Intent classification:
   - "apply", "request", "take" → intent = apply
   - "cancel", "withdraw" → intent = cancel
   - "show", "list", "get" → intent = get
   - "update", "change", "modify" → intent = update
   - Simple greetings → intent = none
3. The "response" field should contain a short friendly assistant reply.
4. Never include explanation, markdown, code blocks, or extra text outside the JSON.
"""

# ============================================================
# MAIN TIME SLIP CHAT HANDLER
# ============================================================

def call_time_slip_chat(message: str):
    """
    Resolves Time Slip intent and slots using OpenAI.
    Function signature is NOT changed.
    """

    llm = get_llm()

    # Inject Current Date for context
    current_date_str = datetime.now().strftime("%A, %d-%m-%Y")
    system_prompt_with_date = f"{TIME_SLIP_SYSTEM_PROMPT}\n\nCurrent Date: {current_date_str}"

    messages = [
        SystemMessage(content=system_prompt_with_date),
        HumanMessage(content=message)
    ]

    try:
        raw = llm.invoke(messages)
        content = raw.content.strip()
    except Exception:
        return {
            "response": "I can help with time slip.",
            "action": regex_extract(message)
        }

    # Remove ```json wrappers if any
    content = re.sub(r"^```json|```$", "", content).strip()

    # Safe JSON parsing
    try:
        data = json.loads(content)
    except Exception:
        try:
            json_block = re.search(r"\{[\s\S]*\}", content).group()
            data = json.loads(json_block)
        except Exception:
            return {
                "response": content,
                "action": regex_extract(message)
            }

    # Defensive normalization
    data.setdefault("action", {})
    action = data["action"]
    slots = action.setdefault("slots", {})

    for key in ["TimeSlipDate", "FromTime", "ToTime", "Duration", "TimeSlipReason", "PermissionReason"]:
        slots.setdefault(key, "")
    slots.setdefault("TimeSlipId", 0)

    return data


# ============================================================
# REGEX FALLBACK (NO LLM / FAIL SAFE)
# ============================================================

def regex_extract(text: str):
    t = text.lower()

    # -------- Intent --------
    if re.search(r"\b(cancel|withdraw)\b", t):
        intent = "cancel"
    elif re.search(r"\b(apply|request|take)\b", t):
        intent = "apply"
    elif re.search(r"\b(update|change|modify)\b", t):
        intent = "update"
    elif re.search(r"\b(show|list|get)\b", t):
        intent = "get"
    else:
        intent = "none"

    slots = {
        "TimeSlipDate": "",
        "FromTime": "",
        "ToTime": "",
        "Duration": "",
        "TimeSlipReason": "",
        "PermissionReason": "",
        "TimeSlipId": 0
    }

    return {"intent": intent, "slots": slots}

# ============================================================
# SLOT NORMALIZATION (USED IN main.py)
# ============================================================

def normalize_time_slip_slots(s: dict):
    return {
        "TimeSlipDate": parse_date(s.get("TimeSlipDate", "")) or "",
        "FromTime": s.get("FromTime", "").strip(),
        "ToTime": s.get("ToTime", "").strip(),
        "Duration": s.get("Duration", "").strip(),
        "TimeSlipReason": s.get("TimeSlipReason", "").strip(),
        "PermissionReason": s.get("PermissionReason", "").strip(),
        "TimeSlipId": int(s.get("TimeSlipId", 0)) if s.get("TimeSlipId") else 0
    }
