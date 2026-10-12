import json
import re
from datetime import datetime

from pack_bot.agent import get_llm

try:
    from langchain_core.messages import SystemMessage, HumanMessage
except Exception:
    from langchain_core.messages import SystemMessage, HumanMessage


ATTENDANCE_SYSTEM_PROMPT = """
You are the GoodBooks Daily Attendance Assistant.
Your job is to classify user messages related to daily attendance and extract the pay period name if provided.

You MUST ALWAYS return a JSON object with this structure:

{
  "response": "<natural language reply to user>",
  "action": {
    "intent": "get | view | none",
    "slots": {
      "PayPeriodName": "",
      "PayPeriodId": 0
    }
  }
}

RULES:
1. Never guess or invent slot values.
2. If the user is asking to see attendance, use intent = get or view.
3. Keep the response short and friendly.
4. Never include explanation, markdown, code blocks, or extra text outside the JSON.
"""


def call_attendance_chat(message: str):
    llm = get_llm()
    current_date_str = datetime.now().strftime("%A, %d-%m-%Y")
    system_prompt_with_date = f"{ATTENDANCE_SYSTEM_PROMPT}\n\nCurrent Date: {current_date_str}"

    if not llm:
        return {"response": "I can help you with daily attendance.", "action": regex_extract(message)}

    messages = [SystemMessage(content=system_prompt_with_date), HumanMessage(content=message)]

    try:
        raw = llm.invoke(messages)
        content = raw.content.strip()
    except Exception:
        return {"response": "I can help you with daily attendance.", "action": regex_extract(message)}

    content = re.sub(r"^```json|```$", "", content).strip()

    try:
        data = json.loads(content)
    except Exception:
        try:
            json_block = re.search(r"\{[\s\S]*\}", content).group()
            data = json.loads(json_block)
        except Exception:
            return {"response": content, "action": regex_extract(message)}

    data.setdefault("action", {})
    action = data["action"]
    slots = action.setdefault("slots", {})
    slots.setdefault("PayPeriodName", "")
    slots.setdefault("PayPeriodId", 0)
    return data


def regex_extract(text: str):
    t = text.lower()
    if re.search(r"\b(attendance|daily attendance|check[- ]?in|punch)\b", t):
        intent = "get"
    else:
        intent = "none"

    return {
        "intent": intent,
        "slots": {
            "PayPeriodName": "",
            "PayPeriodId": 0
        }
    }


def normalize_attendance_slots(s: dict):
    return {
        "PayPeriodName": s.get("PayPeriodName", "").strip(),
        "PayPeriodId": int(s.get("PayPeriodId", 0)) if s.get("PayPeriodId") else 0
    }
