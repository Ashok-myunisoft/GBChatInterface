import json
import re
from typing import Dict
from config import settings

# ------------ Import handling for both LC versions ------------
try:
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
    from langchain_openai import ChatOpenAI
except:
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
    from langchain_community.chat_models import ChatOpenai


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are the GoodBooks Pack Assistant. 
Your job is to classify user messages into actions and extract only provided pack information.

You MUST ALWAYS return a JSON object with this structure:

{
  "response": "<natural language reply to user>",
  "action": {
    "intent": "create | delete | update | get | none",
    "slots": {
        "PackName": "",
        "PackCode": "",
        "ConversionType": "",
        "ConversionFactor": "",
        "PackId": 0
    }
  }
}

================ RULES ================

1. Never guess or invent PackName, PackCode, ConversionType, ConversionFactor, or PackId.  
   If the user does not provide something, leave it as an empty string or zero.

2. Intent classification:
   - "delete", "remove", "erase" → intent = delete
   - Mentions BOTH PackName & PackCode → intent = create
   - "create", "add pack", "new pack" → intent = create
   - "show packs", "list packs", "get packs", "show all" → intent = get
   - Simple greetings (“hi”, “hello”, etc.) → intent = none
   - If no clear instruction → intent = none

3. For delete intent:
   - If only PackName is given → fill PackName only
   - If only PackCode is given → fill PackCode only
   - If neither → intent=delete but response should ask user to provide a name or code.

4. The "response" field should contain a short friendly assistant reply.

5. Never include explanation, markdown, code blocks, or extra text outside the JSON.
"""

# ============================================================
# LLM SINGLETON
# ============================================================

_llm = None


def get_llm():
    global _llm
    if not _llm:
        _llm = ChatOpenAI(
            model=settings.OPENAI_MODEL,
            temperature=0.3
        )
    return _llm


# ============================================================
# LLM RESPONSE CLEANER
# ============================================================

def clean_llm_response(res):
    if hasattr(res, "content"):
        return res.content
    return str(res)


# ============================================================
# MAIN AGENT ENTRY (DO NOT RENAME)
# ============================================================

def call_ollama_chat(text: str):
    """
    NOTE:
    Function name is intentionally NOT changed
    to avoid breaking main.py imports.
    """

    llm = get_llm()

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=text)
    ]

    try:
        raw = llm.invoke(messages)
        result = clean_llm_response(raw)
    except Exception as e:
        return {
            "response": f"LLM error: {e}",
            "action": regex_extract(text)
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
                "action": regex_extract(text)
            }

    # Defensive normalization
    data.setdefault("action", {})
    action = data["action"]
    slots = action.setdefault("slots", {})

    for key in ["PackName", "PackCode", "ConversionType", "ConversionFactor", "PackId"]:
        slots.setdefault(key, "" if key != "PackId" else 0)

    # Delete intent priority
    if re.search(r"\b(delete|remove)\b", text, re.I):
        action["intent"] = "delete"

    return data


# ============================================================
# REGEX FALLBACK (NO LLM / FAIL SAFE)
# ============================================================

def regex_extract(text: str):
    t = text.lower()

    # -------- Intent --------
    if "delete" in t or "remove" in t:
        intent = "delete"
    elif "update" in t:
        intent = "update"
    elif "create" in t or "add pack" in t:
        intent = "create"
    elif re.search(r"show|list|get|all packs", t):
        intent = "get"
    else:
        intent = "none"

    slots = {
        "PackName": "",
        "PackCode": "",
        "ConversionType": "",
        "ConversionFactor": "",
        "PackId": 0
    }

    # -------- PackCode --------
    m = re.search(r"(packcode|code)[\s:]*([A-Za-z0-9]+)", text, re.I)
    if m:
        slots["PackCode"] = m.group(2).upper()

    # -------- PackName --------
    m = re.search(r"(packname|name)[\s:]*([A-Za-z]+)", text, re.I)
    if m:
        slots["PackName"] = m.group(2)

    return {
        "intent": intent,
        "slots": slots
    }


# ============================================================
# SLOT NORMALIZATION (USED IN main.py)
# ============================================================

def normalize_slots(s: Dict):
    return {
        "PackName": s.get("PackName", "").strip(),
        "PackCode": s.get("PackCode", "").upper().strip(),
        "ConversionType": s.get("ConversionType", "").strip(),
        "ConversionFactor": s.get("ConversionFactor", "").strip(),
        "PackId": int(s.get("PackId", 0)) if s.get("PackId") else 0
    }
