import json
import re
from typing import Dict

from config import settings
from time_slip_bot.time_slip_prompt import TIME_SLIP_SYSTEM_PROMPT

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage


# ============================================================
# OPENAI LLM SINGLETON
# ============================================================

_llm = None


def get_llm():
    global _llm
    if not _llm:
        _llm = ChatOpenAI(
            model=settings.OPENAI_MODEL,
            temperature=0.2
        )
    return _llm


# ============================================================
# MAIN TIME SLIP CHAT HANDLER
# ============================================================

def call_time_slip_chat(message: str):
    """
    Resolves Time Slip intent and slots using OpenAI.
    Function signature is NOT changed.
    """

    llm = get_llm()

    messages = [
        SystemMessage(content=TIME_SLIP_SYSTEM_PROMPT),
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
    content = re.sub(r"^```json|```$", "", content)

    # Safe JSON parse
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

    # Defensive structure normalization
    data.setdefault("action", {})
    action = data["action"]
    slots = action.setdefault("slots", {})

    for k in [
        "TimeSlipDate",
        "FromTime",
        "ToTime",
        "Duration",
        "TimeSlipReason",
        "PermissionReason",
        "TimeSlipId"
    ]:
        slots.setdefault(k, "" if k != "TimeSlipId" else 0)

    return data


# ============================================================
# REGEX FALLBACK (LLM FAIL SAFE)
# ============================================================

def regex_extract(text: str):
    slots = {
        "TimeSlipDate": "",
        "FromTime": "",
        "ToTime": "",
        "Duration": "",
        "TimeSlipReason": "",
        "PermissionReason": "",
        "TimeSlipId": 0
    }

    t = text.lower()

    if "time slip" in t or "permission" in t:
        intent = "apply"
    else:
        intent = "none"

    m = re.search(r"\d{2}/\d{2}/\d{2,4}", text)
    if m:
        slots["TimeSlipDate"] = m.group()

    m = re.search(r"from\s*(\d{1,2}:\d{2})", t)
    if m:
        slots["FromTime"] = m.group(1)

    m = re.search(r"to\s*(\d{1,2}:\d{2})", t)
    if m:
        slots["ToTime"] = m.group(1)

    m = re.search(r"(\d+)\s*(hour|hours)", t)
    if m:
        slots["Duration"] = m.group(1)

    if "personal" in t:
        slots["TimeSlipReason"] = "Personal Urgency"

    return {
        "intent": intent,
        "slots": slots
    }


# ============================================================
# SLOT NORMALIZATION (USED IN main.py)
# ============================================================

def normalize_time_slip_slots(s: Dict):
    return {
        "TimeSlipDate": s.get("TimeSlipDate", "").strip(),
        "FromTime": s.get("FromTime", "").strip(),
        "ToTime": s.get("ToTime", "").strip(),
        "Duration": s.get("Duration", "").strip(),
        "TimeSlipReason": s.get("TimeSlipReason", "").strip(),
        "PermissionReason": s.get("PermissionReason", "").strip(),
        "TimeSlipId": int(s.get("TimeSlipId", 0)) if s.get("TimeSlipId") else 0
    }
