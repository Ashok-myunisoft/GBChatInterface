import json
import re
from datetime import datetime
from utils.date_parser import parse_date
from pack_bot.agent import get_llm

try:
    from langchain_core.messages import SystemMessage, HumanMessage
except Exception:
    from langchain_core.messages import SystemMessage, HumanMessage

from purchase_bot.purchase_prompt import PURCHASE_SYSTEM_PROMPT


# ============================================================
# MAIN PURCHASE CHAT HANDLER
# ============================================================

def call_purchase_chat(message: str):
    """
    Purchase Intent Resolver
    """

    llm = get_llm()

    # ----------- Fallback if LLM not available -----------
    if not llm:
        return {
            "response": "I can help you with purchase orders.",
            "action": regex_extract(message)
        }

    # Inject Current Date for context
    current_date_str = datetime.now().strftime("%A, %d-%m-%Y")
    system_prompt_with_date = f"{PURCHASE_SYSTEM_PROMPT}\n\nCurrent Date: {current_date_str}"

    messages = [
        SystemMessage(content=system_prompt_with_date),
        HumanMessage(content=message)
    ]

    try:
        raw = llm.invoke(messages)
        result = raw.content.strip()
    except Exception:
        return {
            "response": "I can help you with purchase orders.",
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

    for key in ["PartyName", "ItemName", "Quantity", "Rate", "ReferenceNumber", "Remarks", "StoreName", "MMHeadDate", "MMHeadReferenceDate"]:
        slots.setdefault(key, "")
    slots.setdefault("POId", 0)

    return data


# ============================================================
# REGEX FALLBACK (NO LLM / FAIL SAFE)
# ============================================================

def regex_extract(text: str):
    t = text.lower()

    # -------- Intent --------
    if re.search(r"\b(cancel|withdraw)\b", t):
        intent = "cancel"
    elif re.search(r"\b(create|new|make|buy|purchase)\b", t):
        intent = "create"
    elif re.search(r"\b(show|list|get|my po)\b", t):
        intent = "get"
    else:
        intent = "none"

    slots = {
        "PartyName": "",
        "ItemName": "",
        "Quantity": "",
        "Rate": "",
        "ReferenceNumber": "",
        "Remarks": "",
        "StoreName": "",
        "MMHeadDate": "",
        "MMHeadReferenceDate": "",
        "POId": 0
    }

    return {"intent": intent, "slots": slots}


# ============================================================
# SLOT NORMALIZATION
# ============================================================

def normalize_purchase_slots(s: dict):
    return {
        "PartyName": s.get("PartyName", "").strip(),
        "ItemName": s.get("ItemName", "").strip(),
        "Quantity": s.get("Quantity", "").strip(),
        "Rate": s.get("Rate", "").strip(),
        "ReferenceNumber": s.get("ReferenceNumber", "").strip(),
        "Remarks": s.get("Remarks", "").strip(),
        "StoreName": s.get("StoreName", "").strip(),
        "MMHeadDate": parse_date(s.get("MMHeadDate", "")) or "",
        "MMHeadReferenceDate": parse_date(s.get("MMHeadReferenceDate", "")) or "",
        "POId": int(s.get("POId", 0)) if s.get("POId") else 0
    }
