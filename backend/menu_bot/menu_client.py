from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

_MENU_AGENT_DIR = Path(__file__).resolve().parent.parent.parent / "services" / "menu-agent"
if str(_MENU_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_MENU_AGENT_DIR))

from mcp_client import MenuAgent  # noqa: E402  (import must follow sys.path fix above)

from config import settings  # backend's own config.py -- unrelated to menu-agent's config/ package

_agents: dict[str, MenuAgent] = {}
_sessions: dict[str, list[dict]] = {}
_known_slots: dict[str, dict] = {}

_PICKLIST_MARKER = "__PICKLIST__"


def _split_picklist(raw: str):
    """Split a menu-bot reply into (display_text, picklist_dict_or_None).

    The menu bot appends a ``__PICKLIST__{...json...}`` marker to replies
    that contain a numbered list.  We strip it here (in the backend) so
    the frontend never needs to parse raw text -- it just reads the
    ``picklist`` field in the JSON response.
    """
    idx = raw.find(_PICKLIST_MARKER)
    if idx == -1:
        return raw.strip(), None
    text = raw[:idx].strip()
    try:
        pl = json.loads(raw[idx + len(_PICKLIST_MARKER):])
        if isinstance(pl, dict) and "items" in pl:
            return text, pl
    except (json.JSONDecodeError, ValueError):
        pass
    return text, None


def _session_key(login_header: str, parsed_login: dict) -> str:
    user_id = parsed_login.get("UserId")
    if user_id is not None:
        return f"user:{user_id}"
    return "blob:" + hashlib.sha256(login_header.encode()).hexdigest()[:16]


async def _get_agent(login_header: str) -> tuple[str, MenuAgent]:
    try:
        parsed = json.loads(login_header)
    except json.JSONDecodeError as e:
        raise ValueError(f"Login header is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("Login header must be a JSON object.")

    key = _session_key(login_header, parsed)
    existing = _agents.get(key)
    if existing is not None and existing.login_header == login_header:
        return key, existing

    if not getattr(settings, "OPENAI_API_KEY", None):
        raise ValueError("Missing required setting: OPENAI_API_KEY.")

    new_agent = MenuAgent(
        openai_api_key=settings.OPENAI_API_KEY,
        openai_model=getattr(settings, "OPENAI_MODEL", "gpt-4o-mini"),
        login_header=login_header,
        # Same source menu-agent's own config/settings.py read this from
        # (a bare OS env var, not backend's .env/Settings model) --
        # preserved as-is so behavior doesn't change with the move.
        base_url=os.environ.get("GOODBOOKS_BASE_URL", "").rstrip("/"),
    )
    await new_agent.start()

    if existing is not None:
        await existing.stop()
    _agents[key] = new_agent
    return key, new_agent


async def menu_chat(message: str, raw_login_header: str) -> tuple[str, dict | None]:
    """
    Direct in-process replacement for the old forward_to_menu_agent()
    HTTP call in backend/main.py. Returns (reply_text, picklist_or_None).

    ``picklist_or_None`` is a dict with "items" and "total" keys when the
    reply contains a numbered picklist; None otherwise.  The caller
    (main.py) includes it as the ``picklist`` field in the JSON response
    so the frontend can render a paginated widget without parsing text.
    """
    login_header = raw_login_header or "{}"
    try:
        key, agent = await _get_agent(login_header)
    except ValueError as e:
        return f"⚠️ Menu assistant error: {e}", None

    history = _sessions.get(key, [])
    known_slots = _known_slots.get(key, {})
    try:
        reply, new_history, new_known_slots = await agent.chat(history, known_slots, message)
    except Exception as e:
        return f"⚠️ Menu assistant error: {type(e).__name__}: {e}", None

    _sessions[key] = new_history
    _known_slots[key] = new_known_slots

    text, picklist = _split_picklist(reply or "")
    return text, picklist


async def shutdown_all_agents():
    """Call this from backend's own app shutdown, mirroring the
    lifespan() cleanup services/menu-agent/app.py used to do."""
    for agent in _agents.values():
        await agent.stop()
    _agents.clear()