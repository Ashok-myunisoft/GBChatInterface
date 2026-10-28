import json
import os
import re
import subprocess
import uuid
from datetime import datetime
from typing import Dict, Any, Optional

from utils.date_parser import parse_date
from task_bot.task_prompt import TASK_SYSTEM_PROMPT
from task_bot import task_client

OPENCLAW_AGENT_ID = "task-bot"

_VALIDATED_WORK_FILE = os.path.join(os.path.dirname(__file__), ".validated_work.json")
_PENDING_TASK_FILE = os.path.join(os.path.dirname(__file__), ".pending_task.json")

_REQUIRED_SLOT_ORDER = ["WorkNumber", "TaskName", "Description"]

_SLOT_QUESTIONS = {
    "WorkNumber": "What's the Work Number for this task? (e.g. W0092618)",
    "TaskName": "What's the task name?",
    "Description": "What's the task description?",
}

_EMPTY_SLOTS = {
    "WorkNumber": "", "TaskName": "", "Description": "", "Activity": "",
    "ProjectSequence": "", "ResourceSequence": "", "PlanDays": "",
    "Hours": "", "TaskDate": ""
}

_DATE_WORDS = {
    "today", "tomorrow", "tmrw", "yesterday", "day", "after", "next", "last", "this",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december"
}


def _get_pending_task() -> dict:
    try:
        with open(_PENDING_TASK_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return dict(_EMPTY_SLOTS)


def _set_pending_task(slots: dict):
    with open(_PENDING_TASK_FILE, "w") as f:
        json.dump(slots, f)


def clear_pending_task():
    _set_pending_task(dict(_EMPTY_SLOTS))


def _merge_and_persist_slots(new_values: dict) -> dict:
    pending = _get_pending_task()
    for key, value in new_values.items():
        if value:
            pending[key] = value
    _set_pending_task(pending)
    return pending


def _next_missing_required_slot(slots: dict) -> Optional[str]:
    for field in _REQUIRED_SLOT_ORDER:
        if not slots.get(field, "").strip():
            return field
    return None


def _ask_next(pending: dict, note: str = "") -> dict:
    missing = _next_missing_required_slot(pending)
    if missing:
        question = _SLOT_QUESTIONS[missing]
        response = f"{note} {question}".strip() if note else question
    else:
        response = note or "Got it. Anything else to add (activity, hours, date)?"
    return {
        "response": response,
        "action": {"intent": "create", "slots": dict(pending)}
    }


def _build_context_block(pending: dict) -> str:
    collected = [f"- {k}: {v}" for k, v in pending.items() if v]
    missing = _next_missing_required_slot(pending)

    lines = []
    if collected:
        lines.append("Already collected task details:")
        lines.extend(collected)
    if missing:
        if lines:
            lines.append("")
        lines.append(f"Currently awaiting: {missing}")

    return "\n".join(lines)


def _get_validated_work():
    try:
        with open(_VALIDATED_WORK_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _set_validated_work(value: Optional[dict]):
    with open(_VALIDATED_WORK_FILE, "w") as f:
        json.dump(value, f)


def _is_bare_date(message: str) -> bool:
    t = message.strip().lower()
    if not t:
        return False
    cleaned = re.sub(r"(st|nd|rd|th)\b", "", t)
    tokens = re.findall(r"[a-z]+|\d+", cleaned)
    if not tokens:
        return False
    return all(tok.isdigit() or tok in _DATE_WORDS for tok in tokens)


def _is_bare_hhmm(message: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}$", message.strip()))


def _is_bare_work_number(message: str) -> bool:
    return bool(re.match(r"^W\d+$", message.strip(), re.IGNORECASE))


# Matches phrasing like "I want to create a task" / "new task" / "add a task" / "start a
# task" — used to detect a fresh task request so leftover state from a previous,
# never-finished task doesn't leak into a brand new one.
_NEW_TASK_TRIGGER_RE = re.compile(r"\b(create|add|new|start)\b.*\btask\b", re.IGNORECASE)


def call_openclaw_task_agent(message: str, current_date_str: str, context_block: str = "") -> str:
    context_part = f"{context_block}\n\n" if context_block else ""
    full_message = (
        f"{TASK_SYSTEM_PROMPT}\n\n"
        f"Current Date: {current_date_str}\n\n"
        f"{context_part}"
        f"IMPORTANT: Treat this as a completely new, isolated request. Do NOT use "
        f"memory_search, memory_get, sessions_history, or any tool to recall "
        f"information from previous conversations or sessions. Base your response "
        f"ONLY on the user message below and the 'Already collected task details' "
        f"section above, if present.\n\n"
        f"User message: {message}"
    )

    session_key = f"agent:task-bot:oneshot-{uuid.uuid4()}"

    result = subprocess.run(
        [
            "openclaw", "agent",
            "--agent", OPENCLAW_AGENT_ID,
            "--local",
            "--session-key", session_key,
            "--message", full_message,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        raise RuntimeError(f"openclaw agent failed: {result.stderr.strip()}")

    payload = json.loads(result.stdout)
    payloads = payload.get("payloads", [])
    if not payloads or "text" not in payloads[0]:
        raise RuntimeError(f"Unexpected openclaw response shape: {result.stdout}")

    return payloads[0]["text"]


def _fallback_from_raw_message(message: str, awaited_before: Optional[str],
                                default_response: str) -> dict:
    """
    Used when the openclaw agent call fails or doesn't return usable JSON. Without this,
    a reply like "Team Meeting" given while TaskName/Description was awaited would be
    silently dropped, and the same question would be asked again forever. Mirrors the
    same "treat the raw message as the awaited slot's value" fallback used on the normal
    success path.

    Also handles the case where the message contains a work number and/or task name
    inline (e.g. "create task for W0092573 with name General Meeting") so those values
    are not lost when openclaw fails.
    """
    stripped = message.strip()
    extracted = {}

    # Try to extract WorkNumber from message (W followed by digits)
    wn_match = re.search(r"\bW\d+\b", stripped, re.IGNORECASE)
    if wn_match:
        extracted["WorkNumber"] = wn_match.group().upper()

    # Try to extract TaskName from patterns like "task name <X>" / "name <X>"
    tn_match = re.search(r"\btask\s+name\s+(.+?)(?:\s+with\s+(?:description|activity|hours|date|project|resource)|\s+(?:description|activity|hours|date|project|resource)\s|$)",
                         stripped, re.IGNORECASE)
    if not tn_match:
        tn_match = re.search(r"\bnamed?\s+(.+?)(?:\s+with\s+(?:description|activity|hours|date|project|resource)|\s+(?:description|activity|hours|date|project|resource)\s|$)",
                             stripped, re.IGNORECASE)
    if tn_match:
        extracted["TaskName"] = tn_match.group(1).strip()

    # Try to extract Description from patterns like "description <X>" / "with description <X>"
    desc_match = re.search(r"\bdescription\s+(.+?)(?:\s+with\s+(?:task|activity|hours|date|project|resource)|\s+(?:activity|hours|date|project|resource)\s|$)",
                           stripped, re.IGNORECASE)
    if desc_match:
        extracted["Description"] = desc_match.group(1).strip()

    # Try to extract ProjectSequence from patterns like "project sequence <N>"
    ps_match = re.search(r"\bproject\s+sequence\s+(\d+)", stripped, re.IGNORECASE)
    if ps_match:
        extracted["ProjectSequence"] = ps_match.group(1).strip()

    # Try to extract ResourceSequence from patterns like "resource sequence <N>"
    rs_match = re.search(r"\bresource\s+sequence\s+(\d+)", stripped, re.IGNORECASE)
    if rs_match:
        extracted["ResourceSequence"] = rs_match.group(1).strip()

    if extracted:
        pending = _merge_and_persist_slots(extracted)
        return _ask_next(pending)

    if awaited_before in ("TaskName", "Description") and stripped and not _is_bare_work_number(stripped):
        pending = _merge_and_persist_slots({awaited_before: stripped})
        return _ask_next(pending)
    return {
        "response": default_response,
        "action": regex_extract(message)
    }


def call_task_chat(message: str, login: Dict[str, Any] = None):
    if _NEW_TASK_TRIGGER_RE.search(message) and not _is_bare_work_number(message):
        clear_pending_task()

    if _is_bare_work_number(message):
        return _handle_work_number(message.strip().upper(), login)

    if _is_bare_date(message):
        parsed = parse_date(message)
        if parsed:
            pending = _merge_and_persist_slots({"TaskDate": parsed})
            return _ask_next(
                pending,
                note=f"Got it, task date noted as {parsed}. That'll be used for "
                     f"Start On, End On, Due On and Expiry On."
            )

    if _is_bare_hhmm(message):
        minutes = task_client._parse_hhmm_to_minutes(message.strip())
        if minutes is not None:
            if minutes > task_client.MAX_HOURS_PER_TASK_MIN:
                return {
                    "response": f"A single task can't exceed 08:00. You entered "
                                f"{message.strip()} — please give a value up to 08:00.",
                    "action": {"intent": "create", "slots": dict(_get_pending_task())}
                }
            pending = _merge_and_persist_slots({"Hours": message.strip()})
            return _ask_next(pending, note=f"Got it, {message.strip()} hours noted.")

    pending_before = _get_pending_task()
    awaited_before = _next_missing_required_slot(pending_before)
    current_date_str = datetime.now().strftime("%A, %d-%m-%Y")
    context_block = _build_context_block(pending_before)

    try:
        result = call_openclaw_task_agent(message, current_date_str, context_block)
    except Exception:
        return _fallback_from_raw_message(
            message, awaited_before, "I can help you create a task."
        )

    result = re.sub(r"^```json|```$", "", result).strip()

    try:
        data = json.loads(result)
    except Exception:
        try:
            json_block = re.search(r"\{[\s\S]*\}", result).group()
            data = json.loads(json_block)
        except Exception:
            return _fallback_from_raw_message(message, awaited_before, result)

    data.setdefault("action", {})
    action = data["action"]
    slots = action.setdefault("slots", {})
    for key in _EMPTY_SLOTS:
        slots.setdefault(key, "")

    work_number = slots.get("WorkNumber", "").strip()

    # Persist all non-WorkNumber slots first, so they survive even if work number
    # validation fails — otherwise a message like "create task W1234 name Foo" loses
    # TaskName/Description when the API call fails/returns invalid.
    new_values = {k: v for k, v in slots.items() if v and k != "WorkNumber"}
    if new_values:
        _merge_and_persist_slots(new_values)

    if work_number:
        gate_result = _handle_work_number(work_number, login, merge_into=data)
        if gate_result is not None:
            return gate_result

    new_values = {k: v for k, v in slots.items() if v}

    # Fallback: if a TaskName/Description reply was awaited and the LLM didn't extract
    # it (returned it empty), treat the raw message itself as that field's value. Don't
    # rely solely on the LLM correctly reading the "Currently awaiting" hint.
    if awaited_before in ("TaskName", "Description") and not new_values.get(awaited_before):
        stripped = message.strip()
        if stripped and not _is_bare_work_number(stripped):
            new_values[awaited_before] = stripped

    # Inline regex rescue: if Description or ProjectSequence still missing from what the
    # LLM returned but the raw message contains them, extract directly so they are never
    # silently dropped regardless of whether openclaw succeeded or failed.
    if not new_values.get("Description"):
        desc_match = re.search(
            r"\bdescription\s+(.+?)(?:\s+with\s+(?:task|activity|hours|date|project|resource)"
            r"|\s+(?:activity|hours|date|project|resource)\s|$)",
            message, re.IGNORECASE)
        if desc_match:
            new_values["Description"] = desc_match.group(1).strip()

    if not new_values.get("ProjectSequence"):
        ps_match = re.search(r"\bproject\s+sequence\s+(\d+)", message, re.IGNORECASE)
        if ps_match:
            new_values["ProjectSequence"] = ps_match.group(1).strip()

    if not new_values.get("ResourceSequence"):
        rs_match = re.search(r"\bresource\s+sequence\s+(\d+)", message, re.IGNORECASE)
        if rs_match:
            new_values["ResourceSequence"] = rs_match.group(1).strip()

    pending = _merge_and_persist_slots(new_values)
    data["action"]["slots"] = dict(pending)

    missing = _next_missing_required_slot(pending)
    if missing:
        # Trust the LLM response — it already knows what was collected and what is missing.
        # Only fall back to the hardcoded question if the LLM returned nothing useful.
        if not data.get("response", "").strip():
            data["response"] = _SLOT_QUESTIONS[missing]
    else:
        data["response"] = "Got it — I have everything needed. Anything else to add (activity, hours, date)?"

    return data


def _handle_work_number(work_number: str, login: Optional[Dict[str, Any]],
                         merge_into: Optional[dict] = None):
    cached = _get_validated_work()
    if cached and cached.get("WorkNumber", "").upper() == work_number.upper() and cached.get("valid"):
        pending = _merge_and_persist_slots({"WorkNumber": work_number})
        if merge_into is not None:
            return None
        return _ask_next(pending)

    if login is None:
        return {
            "response": "I can't validate that Work Number right now (no session/login "
                        "available). Please try again once you're logged in.",
            "action": {"intent": "none", "slots": dict(_get_pending_task())}
        }

    result = task_client.validate_work_number(work_number, login)

    if not result.get("valid"):
        _set_validated_work(None)
        return {
            "response": result.get("reason", f"Work Number {work_number} couldn't be "
                                              f"validated."),
            "action": {"intent": "none", "slots": dict(_get_pending_task())}
        }

    _set_validated_work({
        "WorkNumber": work_number,
        "valid": True,
        "requester": result.get("requester", ""),
        "allocation_split": result.get("allocation_split", ""),
        "work_id": result.get("work_id"),
    })

    pending = _merge_and_persist_slots({"WorkNumber": work_number})

    if merge_into is not None:
        return None

    return _ask_next(pending, note=f"Work Number {work_number} confirmed.")


def regex_extract(text: str):
    t = text.lower()
    if re.search(r"\b(create|add|new)\b.*\btask\b", t) or _is_bare_work_number(text):
        intent = "create"
    else:
        intent = "none"
    return {"intent": intent, "slots": dict(_EMPTY_SLOTS)}


def normalize_task_slots(s: dict):
    task_date = parse_date(s.get("TaskDate", ""))
    return {
        "WorkNumber": s.get("WorkNumber", "").strip().upper(),
        "TaskName": s.get("TaskName", "").strip(),
        "Description": s.get("Description", "").strip(),
        "Activity": s.get("Activity", "").strip(),
        "ProjectSequence": s.get("ProjectSequence", "").strip(),
        "ResourceSequence": s.get("ResourceSequence", "").strip(),
        "PlanDays": s.get("PlanDays", "").strip(),
        "Hours": s.get("Hours", "").strip(),
        "TaskDate": task_date or "",
    }