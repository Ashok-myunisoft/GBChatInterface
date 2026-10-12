from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path

# Ensure services/menu-agent/ is on sys.path BEFORE any local imports so that
# "from services.extraction import ..." resolves correctly whether this module
# is run directly (as a standalone service) or imported in-process by
# backend/main.py (whose CWD is backend/, not services/menu-agent/).
# This must come before the mcp / openai / services imports below.
_MENU_AGENT_DIR = Path(__file__).resolve().parent
if str(_MENU_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_MENU_AGENT_DIR))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI, APIStatusError, APIConnectionError

from services.extraction import extract_known_fields, merge_known_slots, format_known_slots_reminder

logger = logging.getLogger("uvicorn.error")

# Anchored to this file's own location rather than left as bare relative
# paths, so this module (and the mcp_server.py subprocess it launches)
# works correctly regardless of which directory the *importing* process
# was started from -- e.g. when imported in-process by backend/main.py,
# whose own CWD is backend/, not services/menu-agent/.
MENU_AGENT_DIR = _MENU_AGENT_DIR  # already resolved at import time above

with open(MENU_AGENT_DIR / "prompts" / "menu_creation_prompt.md") as f:
    SYSTEM_PROMPT = f.read()

MAX_TOOL_ITERATIONS = 40

OPENAI_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
OPENAI_MAX_RETRIES = 3
OPENAI_RETRY_BASE_DELAY_SECONDS = 1.5


STEP_MODULE              = "module"
STEP_MENU_NAME           = "menu_name"
STEP_CODE                = "code"
STEP_CAPTION             = "caption"
STEP_SECTION             = "section"
STEP_UNDER               = "under"
STEP_TYPE                = "type"
STEP_NATURE              = "nature"
STEP_SORT_ORDER          = "sort_order"
STEP_STATUS              = "status"
STEP_FROM_VERSION        = "from_version"
STEP_TO_VERSION          = "to_version"
STEP_CONFIRM             = "confirm"
STEP_SAVE                = "save"
STEP_POST_SAVE           = "post_save"

# MenuSet steps -- entered automatically right after ModuleVsMenu is saved.
STEP_MS_COLLECT          = "ms_collect"
STEP_MS_PARENT           = "ms_parent"
STEP_MS_SUB              = "ms_sub"
STEP_MS_CHILD            = "ms_child"
STEP_MS_VISIBLE          = "ms_visible"
STEP_MS_CONFIRM          = "ms_confirm"

# Combined final confirmation step (new)
STEP_FINAL_CONFIRM       = "final_confirm"

MENU_CREATION_STEPS = [
    STEP_MODULE, STEP_MENU_NAME, STEP_CODE, STEP_CAPTION,
    STEP_SECTION, STEP_UNDER, STEP_TYPE, STEP_NATURE,
    STEP_SORT_ORDER, STEP_STATUS, STEP_FROM_VERSION, STEP_TO_VERSION,
    STEP_CONFIRM, STEP_SAVE,
]

PICKLIST_STEPS: dict[str, str] = {
    STEP_MODULE:  "list_modules",
    STEP_SECTION: "list_sections",
    STEP_UNDER:   "list_parent_menus",
    STEP_TYPE:    "list_menu_types",
    STEP_NATURE:  "list_menu_natures",
    STEP_STATUS:  "list_menu_statuses",
}

STEP_QUESTIONS: dict[str, str] = {
    STEP_MENU_NAME:   "Please enter menu name.",
    STEP_CAPTION:     "Please enter caption.",
    STEP_SORT_ORDER:  "Please enter sort order.",
    STEP_FROM_VERSION: "Please enter from version.",
    STEP_TO_VERSION:  "Please enter to version.",
}

PICKLIST_HEADERS: dict[str, tuple[str, str]] = {
    STEP_MODULE:  ("Please enter module name.", "Please enter the module number."),
    STEP_SECTION: ("Please enter section.", "Please enter the section number."),
    STEP_UNDER:   ("Please enter under (parent menu).", "Please enter the number."),
    STEP_TYPE:    ("Please enter type.", "Please enter the number."),
    STEP_NATURE:  ("Please enter nature.", "Please enter the number."),
    STEP_STATUS:  ("Please enter status.", "Please enter the number."),
}

STEP_RESOLVED_KEY: dict[str, str] = {
    STEP_MODULE:      "module",
    STEP_MENU_NAME:   "menu_name",
    STEP_CODE:        "menu_code",
    STEP_CAPTION:     "caption",
    STEP_SECTION:     "section",
    STEP_UNDER:       "under",
    STEP_TYPE:        "type",
    STEP_NATURE:      "nature",
    STEP_SORT_ORDER:  "sort_order",
    STEP_STATUS:      "status",
    STEP_FROM_VERSION: "from_version",
    STEP_TO_VERSION:  "to_version",
}

_BARE_NUMBER_RE = re.compile(r"^\d+$")
_AFFIRMATIVE_RE = re.compile(r"^(yes|y|yeah|yep|yup|confirm|confirmed)\b", re.IGNORECASE)
_NEGATIVE_RE    = re.compile(r"^(no|n|nope|nah|cancel)\b", re.IGNORECASE)
_CHANGE_KEYWORDS = ("change", "edit", "update", "modify", "different", "wrong", "incorrect", "redo", "fix", "correct")

_NAME_MATCH_PROMPT = """You are a name-matcher for GoodBooks.
The user typed an answer that is NOT a bare number. 
Match it to the best option from the provided list.
Return ONLY a JSON object: {"number": <matched_number_or_null>}
If no option is a clear match, return {"number": null}.
"""


def _is_affirmative(msg: str) -> bool:
    return bool(_AFFIRMATIVE_RE.match(msg.strip()))


def _is_negative(msg: str) -> bool:
    return bool(_NEGATIVE_RE.match(msg.strip()))


def _is_change_request(msg: str) -> bool:
    low = msg.strip().lower()
    return any(kw in low for kw in _CHANGE_KEYWORDS)


def _format_picklist(header: str, items: list, footer: str) -> str:
    lines = [header, ""]
    for it in items:
        label = it.get("name") or it.get("label") or ""
        lines.append(f"{it.get('number')}. {label}")
    if not items:
        lines.append("(no options found)")
    lines.append("")
    lines.append(footer)
    text = "\n".join(lines)

    # Append a structured marker so the frontend can render a paginated
    # picklist widget with ◀/▶ navigation.  The marker is stripped before
    # display, so if the frontend doesn't understand it the plain text
    # above is still readable.  No other bot ever calls this function,
    # so this marker never appears in any other bot's replies.
    if items:
        pl_payload = json.dumps({
            "items": [
                {"number": it.get("number"), "name": it.get("name") or it.get("label") or ""}
                for it in items
            ],
            "total": len(items),
        }, separators=(",", ":"))
        text += f"\n\n__PICKLIST__{pl_payload}"
    return text


def _tree_node_name(node: dict) -> str | None:
    """Same field-name handling as picklists._flatten_menu_tree: nodes
    carry their readable name in 'DisplayName' (or 'data')."""
    return node.get("DisplayName") or node.get("data")


def _menu_tree_level_items(nodes) -> list[dict]:
    """Return the real (named) menu nodes at one level of a MenuTree
    response, skipping any nameless container node by using its
    children instead -- one level only (not a full flatten)."""
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        return []
    out: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if _tree_node_name(node):
            out.append(node)
        else:
            out.extend(_menu_tree_level_items(node.get("children") or []))
    return out


def _numbered_tree_items(nodes: list[dict]) -> list[dict]:
    """Number a level of menu-tree nodes for display/matching, same
    shape (number/id/name/code) as every other picklist here, plus the
    raw 'children' so the next level can be expanded on selection."""
    out = []
    for i, node in enumerate(nodes, start=1):
        out.append({
            "number": i,
            "id": node.get("Id"),
            "name": _tree_node_name(node),
            "code": node.get("Code"),
            "children": node.get("children") or [],
        })
    return out


def _format_tree_picklist(header: str, items: list[dict]) -> str:
    lines = [header, ""]
    for it in items:
        lines.append(f"{it.get('number')}. {it.get('name')}")
    if not items:
        lines.append("(no options found)")
    text = "\n".join(lines)

    # Append the same structured marker as _format_picklist so the frontend
    # renders a paginated ◀/▶ widget instead of raw numbered text.
    # (Previously missing from this function -- that was the root cause of the
    # "raw JSON / plain list" display bug for the Under/parent-menu step and
    # all MenuSet tree steps.)
    if items:
        pl_payload = json.dumps(
            {
                "items": [
                    {"number": it.get("number"), "name": it.get("name") or ""}
                    for it in items
                ],
                "total": len(items),
            },
            separators=(",", ":"),
        )
        text += f"\n\n__PICKLIST__{pl_payload}"
    return text


_MS_REQUIRED_FIELDS = ("name", "client", "module")
_MS_FIELD_ORDER = ("applicable_type", "client", "name", "module")
_MS_FIELD_LABELS = {
    "applicable_type": "Applicable Type",
    "client": "Client",
    "name": "Name",
    "module": "Module",
}
_MS_APPLICABLE_TYPE_DEFAULT = {"code": "0", "label": "All Client"}

# header/footer for each MenuSet field's numbered picklist message, same
# style as PICKLIST_HEADERS used for the menu-creation steps above.
_MS_FIELD_HEADER_FOOTER: dict[str, tuple[str, str]] = {
    "applicable_type": (
        "Please enter Applicable Type.",
        "Please enter the Applicable Type number (or leave blank for default: All Client).",
    ),
    "client": ("Please enter Client.", "Please enter the Client number."),
    "name": ("Please enter the Menu Set Name.", "Please enter the Menu Set Name number."),  # FIX: was "Menu Name" — this field is the MenuSet's Name, not the menu's name
    "module": ("Please enter Module.", "Please enter the Module number."),
}

_MS_INTRO_HEADER = (
    "Now let's fill in the MenuSet details.\n\n"
    "If you know multiple details, you can give them all together — "
    "otherwise just answer the question below."
)

_MS_EXTRACTION_PROMPT = """You extract MenuSet details from a user's message for the GoodBooks Menu Agent.
Return ONLY a JSON object with exactly these keys, nothing else:
{"ApplicableType": "", "Client": "", "Name": "", "Module": ""}

RULES:
- Extract ONLY what the user explicitly states in this message. Never guess, infer, or default a value.
- Keep values exactly as the user wrote them -- do not resolve them against any picklist, do not invent ids.
- ApplicableType is the MenuSet's Applicable Type (e.g. "Client", "All Client").
- Client is the Client name for the MenuSet.
- Name is the MenuSet's Name.
- Module is the Module name for the MenuSet.
- If the message is a plain unlabeled list of values with no field names (one per line, or comma-separated), treat the FIRST value as the Name.
- If a field isn't mentioned, its value is "" (empty string). Never omit a key.
- Output must be valid JSON only -- no markdown, no code fences, no explanation.
"""

_MS_TREE_EXTRACTION_PROMPT = """You extract MenuSet menu-tree selections from a user's message for the GoodBooks Menu Agent.
Return ONLY a JSON object with exactly these keys, nothing else:
{"Parent": "", "SubMenu": "", "Child": "", "Visible": ""}

RULES:
- Extract ONLY what the user explicitly states in this message. Never guess, infer, or default a value.
- Keep values exactly as the user wrote them -- do not resolve them against any picklist, do not invent ids.
- Parent is the top-level (Parent) menu to select, e.g. "Master", "Report".
- SubMenu is the sub menu under the Parent, e.g. "Employee", "Department".
- Child is the child menu under the Sub Menu, e.g. "Employee Master".
- Visible is the user's answer to whether the menu should be visible, e.g. "Yes", "No", "Optional".
- If the message is a plain unlabeled list of values with no field names (one per line, or comma-separated), treat them in this fixed order: Name (ignore), Parent, SubMenu, Child, Visible -- i.e. the 2nd value is Parent, 3rd is SubMenu, 4th is Child, 5th is Visible.
- If a field isn't mentioned, its value is "" (empty string). Never omit a key.
- Output must be valid JSON only -- no markdown, no code fences, no explanation.
"""

_MS_VISIBLE_OPTIONAL_RE = re.compile(r"\boptional\b", re.IGNORECASE)
_MS_VISIBLE_NO_RE = re.compile(r"\b(no|hidden|hide|don'?t\s+show|do\s*not\s+show)\b", re.IGNORECASE)
_MS_VISIBLE_YES_RE = re.compile(r"\b(yes|visible|show|display)\b", re.IGNORECASE)


def _resolve_ms_visibility(text: str) -> tuple[int, str] | None:
    """Resolve free-text or numbered Visibility wording into (visible_code, label)."""
    stripped = text.strip()
    if stripped == "1":
        return 1, "Yes"
    if stripped == "2":
        return 2, "No"
    if stripped == "3":
        return 3, "Optional"
    if _MS_VISIBLE_OPTIONAL_RE.search(stripped):
        return 3, "Optional"
    if _MS_VISIBLE_NO_RE.search(stripped):
        return 2, "No"
    if _MS_VISIBLE_YES_RE.search(stripped):
        return 1, "Yes"
    return None


def _ms_missing_labels(resolved_ms: dict) -> list[str]:
    return [
        _MS_FIELD_LABELS[key]
        for key in _MS_REQUIRED_FIELDS
        if not resolved_ms.get(key)
    ]


def _ms_next_field(resolved_ms: dict) -> str | None:
    """Return the next MenuSet field to collect, skipping 'client' when
    applicable_type is All Client (code '0') since that field is not shown."""
    applicable_type_code = (resolved_ms.get("applicable_type") or {}).get("code", "0")
    for key in ("applicable_type", "client", "name", "module"):
        if key == "client" and str(applicable_type_code) == "0":
            continue  # Client not required for All Client
        if not resolved_ms.get(key):
            return key
    return None


def _build_combined_summary(resolved: dict, mvm_data: dict, resolved_ms: dict) -> str:
    """Build the combined Menu + ModuleVsMenu + MenuSet summary shown once before saving."""
    SEP = "─" * 40

    # --- Menu section ---
    r = resolved

    def _label(key: str, sub: str | None = None) -> str:
        if sub:
            d = r.get(key, {})
            return d.get(sub, "NONE") if isinstance(d, dict) else "NONE"
        v = r.get(key)
        if isinstance(v, dict):
            return v.get("name") or v.get("label") or "NONE"
        return str(v) if v is not None else "NONE"

    menu_lines = [
        "Menu",
        SEP,
        f"Module: {_label('module', 'name')}",
        f"Menu Name: {r.get('menu_name', 'NONE')}",
        f"Menu Display Name: {r.get('menu_name', 'NONE')}",
        f"Code: {r.get('menu_code', 'NONE')}",
        f"Help URL: NONE",
        f"Caption: {r.get('caption', 'NONE')}",
        f"Image: Image place holder",
        f"Section: {_label('section', 'name')}",
        f"Under (Parent Menu): {_label('under', 'name')}",
        f"List Menu: NONE",
        f"Type: {_label('type', 'label')}",
        f"Nature: {_label('nature', 'label')}",
        f"Page: NONE",
        f"Report: NONE",
        f"WebForm: NONE",
        f"Object Model: NONE",
        f"Icon Name: NONE",
        f"Save Web Service: NONE",
        f"Get Web Service: NONE",
        f"Delete Web Service: NONE",
        f"URI Parameter Value: NONE",
        f"Sort Order: {r.get('sort_order', 'NONE')}",
        f"BIZTransaction Class: NONE",
        f"Entity: NONE",
        f"GCM Type: NONE",
        f"Status: {_label('status', 'label')}",
        f"From Version: {r.get('from_version', 'NONE')}",
        f"To Version: {r.get('to_version', 'NONE')}",
    ]

    # --- ModuleVsMenu section ---
    mvm_lines = [
        "",
        "ModuleVsMenu",
        SEP,
        f"Module: {mvm_data.get('module_name', 'NONE')}",
        f"Menu: {mvm_data.get('menu_name', 'NONE')}",
        f"Parent Menu: {mvm_data.get('under_label', 'NONE')}",
        f"Sequence No: {mvm_data.get('seq_no', 'NONE')}",
    ]

    # --- MenuSet section ---
    applicable_type = resolved_ms.get("applicable_type") or {}
    client = resolved_ms.get("client") or {}
    name = resolved_ms.get("name") or {}
    module = resolved_ms.get("module") or {}
    ms_menu_name = resolved_ms.get("_menu_name", r.get("menu_name", "NONE"))
    visible_label = resolved_ms.get("visible_label", "NONE")

    ms_lines = [
        "",
        "MenuSet",
        SEP,
        f"Applicable Type: {applicable_type.get('label', 'NONE')}",
        f"Client: {client.get('name', 'NONE')}",
        f"Name: {name.get('name', 'NONE')}",
        f"Module: {module.get('name', 'NONE')}",
        f"Menu: {ms_menu_name}",
        f"Visible: {visible_label}",
    ]

    confirm_lines = [
        "",
        SEP,
        "Confirm and save all? (yes/no)",
    ]

    return "\n".join(menu_lines + mvm_lines + ms_lines + confirm_lines)


def _get_wf(known_slots: dict) -> dict:
    wf = known_slots.get("_wf")
    if not isinstance(wf, dict):
        wf = {
            "step": STEP_MODULE,
            "resolved": {},
            "picklist": [],
            "last_q": "",
            "phase": "menu_creation",
        }
    return wf


def _set_wf(known_slots: dict, wf: dict) -> dict:
    return {**known_slots, "_wf": wf}


def _build_summary(resolved: dict) -> str:
    """Build the STEP 24 confirmation summary."""
    r = resolved

    def _label(key: str, sub: str | None = None) -> str:
        if sub:
            d = r.get(key, {})
            return d.get(sub, "NONE") if isinstance(d, dict) else "NONE"
        v = r.get(key)
        if isinstance(v, dict):
            return v.get("name") or v.get("label") or "NONE"
        return str(v) if v is not None else "NONE"

    module_name = _label("module", "name")
    menu_name   = r.get("menu_name", "NONE")
    menu_code   = r.get("menu_code", "NONE")
    caption     = r.get("caption", "NONE")
    section_name = _label("section", "name")
    under_name  = _label("under", "name")
    type_label  = _label("type", "label")
    nature_label= _label("nature", "label")
    sort_order  = r.get("sort_order", "NONE")
    status_label= _label("status", "label")
    from_ver    = r.get("from_version", "NONE")
    to_ver      = r.get("to_version", "NONE")

    lines = [
        f"Module: {module_name}",
        f"Menu Name: {menu_name}",
        f"Menu Display Name: {menu_name}",
        f"Code: {menu_code}",
        f"Help URL: NONE",
        f"Caption: {caption}",
        f"Image: Image place holder",
        f"Section: {section_name}",
        f"Under (Parent Menu): {under_name}",
        f"List Menu: NONE",
        f"Type: {type_label}",
        f"Nature: {nature_label}",
        f"Page: NONE",
        f"Report: NONE",
        f"WebForm: NONE",
        f"Object Model: NONE",
        f"Icon Name: NONE",
        f"Save Web Service: NONE",
        f"Get Web Service: NONE",
        f"Delete Web Service: NONE",
        f"URI Parameter Value: NONE",
        f"Sort Order: {sort_order}",
        f"BIZTransaction Class: NONE",
        f"Entity: NONE",
        f"GCM Type: NONE",
        f"Status: {status_label}",
        f"From Version: {from_ver}",
        f"To Version: {to_ver}",
        "",
        "Confirm and save? (yes/no)",
    ]
    return "\n".join(lines)


def _build_create_fields(resolved: dict) -> dict:
    """Build the `fields` dict for create_menu."""
    mod = resolved.get("module", {})
    sec = resolved.get("section", {})
    under = resolved.get("under", {})
    typ = resolved.get("type", {})
    nat = resolved.get("nature", {})
    sta = resolved.get("status", {})

    fields: dict = {
        "menu_name":   resolved.get("menu_name"),
        "menu_code":   resolved.get("menu_code"),
        "module_id":   mod.get("id") if isinstance(mod, dict) else None,
        "module_name": mod.get("name") if isinstance(mod, dict) else None,
        "module_code": mod.get("code") if isinstance(mod, dict) else None,
        "type_code":   typ.get("code") if isinstance(typ, dict) else None,
        "nature_code": nat.get("code") if isinstance(nat, dict) else None,
    }
    if isinstance(sec, dict) and sec.get("id"):
        fields["section_id"] = sec["id"]
    if isinstance(under, dict) and under.get("id"):
        fields["under_id"]   = under["id"]
        fields["under_code"] = under.get("code")
        fields["under_name"] = under.get("name")
    caption = resolved.get("caption")
    if caption:
        fields["caption"] = caption
    sort_order = resolved.get("sort_order")
    if sort_order:
        fields["sort_order"] = sort_order
    if isinstance(sta, dict) and sta.get("code"):
        fields["status_code"] = sta["code"]
    from_ver = resolved.get("from_version")
    if from_ver:
        fields["from_version"] = from_ver
    to_ver = resolved.get("to_version")
    if to_ver:
        fields["to_version"] = to_ver
    return fields



class MenuAgent:
    def __init__(
        self,
        *,
        openai_api_key: str,
        openai_model: str,
        login_header: str,
        base_url: str = "",
    ):
        if not openai_api_key:
            raise ValueError("openai_api_key is required")
        if not login_header:
            raise ValueError("Missing Login header")
        try:
            json.loads(login_header)
        except json.JSONDecodeError as e:
            raise ValueError(f"Login header is not valid JSON: {e}") from e

        self.session: ClientSession | None = None
        self.openai_model = openai_model
        self.openai = AsyncOpenAI(
            api_key=openai_api_key,
        )
        self.tool_schemas: list[dict] = []
        self.login_header = login_header

        self._subprocess_env = {
            **os.environ,
            "GOODBOOKS_LOGIN_JSON": login_header,
            "GOODBOOKS_BASE_URL": base_url,
        }

        self._ready_event: asyncio.Event | None = None
        self._stop_event: asyncio.Event | None = None
        self._runner_task: asyncio.Task | None = None
        self._start_error: BaseException | None = None

    async def start(self):
        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._start_error = None
        self._runner_task = asyncio.create_task(self._run())
        await self._ready_event.wait()
        if self._start_error is not None:
            raise self._start_error

    async def _run(self):
        try:
            async with AsyncExitStack() as stack:
                server_params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(MENU_AGENT_DIR / "mcp_server.py")],
                    cwd=str(MENU_AGENT_DIR),
                    env=self._subprocess_env,
                )
                read, write = await stack.enter_async_context(stdio_client(server_params))
                self.session = await stack.enter_async_context(ClientSession(read, write))
                await self.session.initialize()
                listed = await self.session.list_tools()
                self.tool_schemas = _mcp_tools_to_openai(listed.tools)

                self._ready_event.set()
                await self._stop_event.wait()
        except BaseException as e:
            self._start_error = e
            self._ready_event.set()

    async def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._runner_task is not None:
            await self._runner_task
            self._runner_task = None


    async def _call_tool(self, name: str, arguments: dict):
        logger.debug("[TOOL CALL] %s(%s)", name, arguments)
        result = await self.session.call_tool(name, arguments)
        text_parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        joined = "\n".join(text_parts) if text_parts else ""

        if getattr(result, "isError", False):
            error_msg = joined or f"Tool '{name}' failed with no error message."
            logger.error("[TOOL ERROR] %s(%s) -> %s", name, arguments, error_msg)
            return {"error": error_msg}

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            if set(structured.keys()) == {"result"}:
                parsed = structured["result"]
            else:
                parsed = structured
        else:
            try:
                parsed = json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                items = []
                ok = True
                for part in text_parts:
                    try:
                        items.append(json.loads(part))
                    except (json.JSONDecodeError, TypeError):
                        ok = False
                        break
                parsed = items if ok else joined

        logger.debug("[TOOL RESULT] %s -> %s", name, parsed)
        return parsed

    async def _create_completion(self, messages: list[dict]):
        last_error: Exception | None = None
        for attempt in range(1, OPENAI_MAX_RETRIES + 1):
            try:
                return await self.openai.chat.completions.create(
                    model=self.openai_model,
                    messages=messages,
                    tools=self.tool_schemas,
                    tool_choice="auto",
                )
            except (APIStatusError, APIConnectionError) as e:
                status_code = getattr(e, "status_code", None)
                is_retryable = isinstance(e, APIConnectionError) or status_code in OPENAI_RETRYABLE_STATUS_CODES
                last_error = e
                if not is_retryable or attempt == OPENAI_MAX_RETRIES:
                    raise
                delay = OPENAI_RETRY_BASE_DELAY_SECONDS * attempt
                logger.warning(
                    "[OPENAI RETRY] attempt %s/%s failed (%s) -- retrying in %.1fs",
                    attempt, OPENAI_MAX_RETRIES, e, delay,
                )
                await asyncio.sleep(delay)
        raise last_error


    async def _llm_extract_fields(self, user_message: str) -> dict:
        """Extract pre-supplied field values from a free-text message."""
        try:
            return await extract_known_fields(self.openai, self.openai_model, user_message)
        except Exception as e:
            logger.warning("[EXTRACTION] failed: %s", e)
            return {}

    async def _llm_extract_ms_fields(self, user_message: str) -> dict:
        """Extract pre-supplied MenuSet field values (Applicable Type, Client,
        Name, Module) from a free-text message. Values are returned as raw
        strings, not yet resolved against any picklist."""
        keys = ("ApplicableType", "Client", "Name", "Module")
        try:
            resp = await self.openai.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": _MS_EXTRACTION_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception as e:
            logger.warning("[MS EXTRACTION] failed: %s", e)
            return {k: "" for k in keys}
        return {k: str(data.get(k, "") or "").strip() for k in keys}

    async def _llm_extract_ms_tree_fields(self, user_message: str) -> dict:
        """Extract pre-supplied Parent/SubMenu/Child/Visible values from a
        free-text message, for the 'user gives everything at once' case."""
        keys = ("Parent", "SubMenu", "Child", "Visible")
        try:
            resp = await self.openai.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": _MS_TREE_EXTRACTION_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception as e:
            logger.warning("[MS TREE EXTRACTION] failed: %s", e)
            return {k: "" for k in keys}
        return {k: str(data.get(k, "") or "").strip() for k in keys}

    async def _ms_get_filtered_names(self, resolved_ms: dict) -> list:
        """Fetch MenuSet Name candidates.
        Per-item module filtering via get_menuset_context is intentionally skipped:
        calling it for every name in the list causes dozens of tool calls before any
        response is returned (the 'Server took too long' timeout). Module is already
        auto-filled from the menu's own module after name selection via
        _ms_autofill_from_name, so pre-filtering is not needed."""
        client_id = (resolved_ms.get("client") or {}).get("id")
        args = {"client_id": client_id} if client_id is not None else {}
        items = await self._call_tool("list_menuset_names", args)
        if isinstance(items, dict) and "error" in items:
            logger.warning("[MENUSET] list_menuset_names failed: %s", items["error"])
            return []
        if not isinstance(items, list):
            return []
        return items

    async def _ms_fetch_options(self, field_key: str, resolved_ms: dict) -> list:
        """Fetch the real, numbered options for a MenuSet field from its tool."""
        if field_key == "applicable_type":
            items = await self._call_tool("list_applicable_types", {})
        elif field_key == "client":
            items = await self._call_tool("list_menuset_clients", {})
        elif field_key == "name":
            items = await self._ms_get_filtered_names(resolved_ms)
        elif field_key == "module":
            items = await self._call_tool("list_modules", {})
        else:
            items = []
        if isinstance(items, dict) and "error" in items:
            logger.warning("[MENUSET] failed to load options for %s: %s", field_key, items["error"])
            return []
        if not isinstance(items, list):
            return []
        return [dict(it, number=i + 1) for i, it in enumerate(items)]

    async def _filter_names_by_module(self, items: list, target_module: str) -> list:
        """Keep only MenuSet Names whose own module matches target_module."""
        target = target_module.strip().lower()
        filtered = []
        for it in items:
            menuset_id = it.get("id")
            if menuset_id is None:
                continue
            context = await self._call_tool("get_menuset_context", {"menuset_id": menuset_id})
            if isinstance(context, dict) and str(context.get("module_name") or "").strip().lower() == target:
                filtered.append(it)
        return filtered

    async def _ms_prompt_for(self, field_key: str, resolved_ms: dict) -> tuple[str, list]:
        """Build the numbered picklist message (with REAL values) for a MenuSet field."""
        items = await self._ms_fetch_options(field_key, resolved_ms)
        header, footer = _MS_FIELD_HEADER_FOOTER[field_key]
        return _format_picklist(header, items, footer), items

    async def _ms_autofill_from_name(self, resolved_ms: dict) -> dict:
        """Once Name is resolved, auto-fill Client and Module from get_menuset_context."""
        name = resolved_ms.get("name") or {}
        menuset_id = name.get("id")
        if menuset_id is None:
            return resolved_ms
        if resolved_ms.get("client") and resolved_ms.get("module"):
            return resolved_ms
        context = await self._call_tool("get_menuset_context", {"menuset_id": menuset_id})
        if not isinstance(context, dict) or "error" in context:
            return resolved_ms
        resolved_ms = dict(resolved_ms)
        if not resolved_ms.get("client") and context.get("client_id") is not None:
            resolved_ms["client"] = {
                "id": context.get("client_id"),
                "name": context.get("client_name"),
                "code": None,
            }
        if not resolved_ms.get("module") and context.get("module_id") is not None:
            resolved_ms["module"] = {
                "id": context.get("module_id"),
                "name": context.get("module_name"),
                "code": context.get("module_code"),
            }
        return resolved_ms

    async def _enter_ms_visible_step(self, wf: dict, resolved_ms: dict) -> tuple[str, dict]:
        resolved_ms = dict(resolved_ms)
        resolved_ms["visible_code"] = 1
        resolved_ms["visible_label"] = "Yes"
        wf = {**wf, "_ms": resolved_ms}
        return await self._enter_final_confirm(wf)

    async def _enter_ms_parent_step(
        self, wf: dict, resolved_ms: dict, source_message: str | None = None
    ) -> tuple[str, dict]:
        """Once Name/Client/Module are resolved, fetch this module's real
        menu tree and walk Parent -> Sub Menu -> Child."""
        module_name = (resolved_ms.get("module") or {}).get("name", "")
        tree = await self._call_tool("get_menu_tree", {"module_name": module_name})
        if isinstance(tree, dict) and "error" in tree:
            return f"Error loading menu tree: {tree['error']}", wf

        tree_extracted = {"Parent": "", "SubMenu": "", "Child": "", "Visible": ""}
        source_texts = []
        first_message = wf.get("resolved", {}).get("_first_message")
        if first_message:
            source_texts.append(first_message)
        if source_message:
            source_texts.append(source_message)
        for text in source_texts:
            extracted = await self._llm_extract_ms_tree_fields(text)
            for k, v in extracted.items():
                if v and not tree_extracted.get(k):
                    tree_extracted[k] = v

        level_items = _numbered_tree_items(_menu_tree_level_items(tree))
        level_plan = (
            ("parent", STEP_MS_PARENT, "Select Parent Menu", "Parent"),
            ("sub_menu", STEP_MS_SUB, "Select Sub Menu", "SubMenu"),
            ("child", STEP_MS_CHILD, "Select Child Menu", "Child"),
        )

        for resolved_key, step_name, header, extract_key in level_plan:
            if not level_items:
                break
            guess = tree_extracted.get(extract_key)
            matched = None
            if guess:
                matched_num = await self._llm_match_name_to_number(guess, level_items)
                if matched_num is not None:
                    matched = next((it for it in level_items if it.get("number") == matched_num), None)
            if matched is None:
                wf = {**wf, "step": step_name, "_ms": resolved_ms, "picklist": level_items}
                return _format_tree_picklist(header, level_items), wf
            resolved_ms[resolved_key] = matched
            level_items = _numbered_tree_items(_menu_tree_level_items(matched.get("children") or []))

        visible = _resolve_ms_visibility(tree_extracted.get("Visible", ""))
        if visible:
            visible_code, visible_label = visible
            resolved_ms["visible_code"] = visible_code
            resolved_ms["visible_label"] = visible_label
            # Go to final combined summary instead of ms_confirm
            wf = {**wf, "_ms": resolved_ms}
            return await self._enter_final_confirm(wf)

        resolved_ms["visible_code"] = 1
        resolved_ms["visible_label"] = "Yes"
        wf = {**wf, "_ms": resolved_ms}
        return await self._enter_final_confirm(wf)

    async def _resolve_ms_option(self, tool: str, args: dict, guess: str) -> dict | None:
        """Fetch a MenuSet picklist and match the user's free-text guess against it."""
        if not guess:
            return None
        items = await self._call_tool(tool, args)
        if isinstance(items, dict) and "error" in items:
            logger.warning("[MENUSET] %s failed: %s", tool, items["error"])
            return None
        if not isinstance(items, list) or not items:
            return None
        numbered = [dict(it, number=i + 1) for i, it in enumerate(items)]
        matched_num = await self._llm_match_name_to_number(guess, numbered)
        if matched_num is None:
            return None
        for it in numbered:
            if it.get("number") == matched_num:
                return it
        return None

    async def _llm_match_name_to_number(self, user_answer: str, items: list) -> int | None:
        """When the user typed a name (not a bare number), use the LLM to find
        the matching item number."""
        if not items:
            return None
        options_text = "\n".join(
            f"{it.get('number')} - {it.get('name') or it.get('label') or ''}"
            for it in items
        )
        try:
            resp = await self.openai.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": _NAME_MATCH_PROMPT},
                    {"role": "user", "content": f"User typed: {user_answer!r}\n\nOptions:\n{options_text}"},
                ],
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            matched = data.get("number")
            if matched is not None:
                return int(matched)
        except Exception as e:
            logger.warning("[NAME_MATCH] failed: %s", e)
        return None


    async def _resolve_picklist_answer(self, user_answer: str, items: list) -> dict | None:
        """Given the user's answer and the current picklist, return the matched
        item dict or None if no match."""
        stripped = user_answer.strip()

        if _BARE_NUMBER_RE.match(stripped):
            num = int(stripped)
            for it in items:
                if it.get("number") == num:
                    return it
            return None

        matched_num = await self._llm_match_name_to_number(stripped, items)
        if matched_num is not None:
            for it in items:
                if it.get("number") == matched_num:
                    return it
        return None

    async def _try_prefill_picklist(
        self, pre_key: str, tool: str, wf: dict, tool_args: dict | None = None
    ) -> dict | None:
        """If the user's first message already supplied a value for `pre_key`,
        fetch the picklist for `tool` and try to match it."""
        pre = wf["resolved"].get("_pre", {})
        pre_value = pre.get(pre_key)
        if not pre_value:
            return None
        items = await self._call_tool(tool, tool_args or {})
        if not (isinstance(items, list) and items):
            return None
        numbered = [dict(it, number=i + 1) for i, it in enumerate(items)]
        return await self._resolve_picklist_answer(pre_value, numbered)

    async def _enter_module_step(self, wf: dict) -> tuple[str, dict]:
        """Fetch module list and present it."""
        items = await self._call_tool("list_modules", {})
        if isinstance(items, dict) and "error" in items:
            return f"Error loading modules: {items['error']}", wf
        if not isinstance(items, list):
            logger.warning("[MODULE STEP] list_modules returned non-list (%s): %r", type(items).__name__, items)
            return "Could not load the module list — the GoodBooks API returned an unexpected response. Please check your Login header / BaseUri and try again.", wf

        numbered = [dict(it, number=i + 1) for i, it in enumerate(items)]
        header, footer = PICKLIST_HEADERS[STEP_MODULE]
        reply = _format_picklist(header, numbered, footer)
        wf = {**wf, "step": STEP_MODULE, "picklist": numbered}
        return reply, wf

    async def _enter_section_step(self, wf: dict) -> tuple[str, dict]:
        """Fetch section list (with one retry) and present it."""
        for attempt in range(2):
            items = await self._call_tool("list_sections", {})
            if isinstance(items, list) and items:
                break
        else:
            err = items.get("error") if isinstance(items, dict) else "empty list"
            return f"Could not load the section list, try again. ({err})", wf

        numbered = [dict(it, number=i + 1) for i, it in enumerate(items)]
        header, footer = PICKLIST_HEADERS[STEP_SECTION]
        reply = _format_picklist(header, numbered, footer)
        wf = {**wf, "step": STEP_SECTION, "picklist": numbered}
        return reply, wf

    async def _enter_under_step(self, wf: dict) -> tuple[str, dict]:
        """Fetch parent menus for the resolved module and present them."""
        module = wf["resolved"].get("module", {})
        module_name = module.get("name", "") if isinstance(module, dict) else ""
        items = await self._call_tool("list_parent_menus", {"module_name": module_name})
        if isinstance(items, dict) and "error" in items:
            return f"Error loading parent menus: {items['error']}", wf
        if not isinstance(items, list):
            return "Could not load the parent menu list. Please try again.", wf

        numbered = [dict(it, number=i + 1) for i, it in enumerate(items)]
        header, footer = PICKLIST_HEADERS[STEP_UNDER]
        reply = _format_picklist(header, numbered, footer)
        wf = {**wf, "step": STEP_UNDER, "picklist": numbered}
        return reply, wf

    async def _enter_picklist_step(self, step: str, tool: str, wf: dict) -> tuple[str, dict]:
        """Generic entry for type/nature/status picklist steps."""
        items = await self._call_tool(tool, {})
        if isinstance(items, dict) and "error" in items:
            return f"Error loading options: {items['error']}", wf
        if not isinstance(items, list):
            return "Could not load the options list. Please try again.", wf

        numbered = [dict(it, number=i + 1) for i, it in enumerate(items)]
        header, footer = PICKLIST_HEADERS[step]
        reply = _format_picklist(header, numbered, footer)
        wf = {**wf, "step": step, "picklist": numbered}
        return reply, wf

    async def _advance_to_section(self, wf: dict) -> tuple[str, dict]:
        """Entry point for the Section step: honor a pre-filled SectionName if given."""
        resolved = wf["resolved"]
        matched = await self._try_prefill_picklist("SectionName", "list_sections", wf)
        if matched:
            resolved["section"] = {
                "id":   matched.get("id"),
                "name": matched.get("name"),
                "code": matched.get("code"),
            }
            wf = {**wf, "step": STEP_UNDER, "resolved": resolved, "picklist": []}
            return await self._advance_to_under(wf)
        wf = {**wf, "step": STEP_SECTION, "resolved": resolved, "picklist": []}
        return await self._enter_section_step(wf)

    async def _advance_to_under(self, wf: dict) -> tuple[str, dict]:
        """Entry point for the Under step: honor a pre-filled UnderName if given."""
        resolved = wf["resolved"]
        module = resolved.get("module", {})
        module_name = module.get("name", "") if isinstance(module, dict) else ""
        matched = await self._try_prefill_picklist(
            "UnderName", "list_parent_menus", wf, {"module_name": module_name}
        )
        if matched:
            resolved["under"] = {
                "id":   matched.get("id"),
                "name": matched.get("name"),
                "code": matched.get("code"),
            }
            wf = {**wf, "step": STEP_TYPE, "resolved": resolved, "picklist": []}
            return await self._advance_to_type(wf)
        wf = {**wf, "step": STEP_UNDER, "resolved": resolved, "picklist": []}
        return await self._enter_under_step(wf)

    async def _advance_to_type(self, wf: dict) -> tuple[str, dict]:
        """Entry point for the Type step: honor a pre-filled TypeName if given."""
        resolved = wf["resolved"]
        matched = await self._try_prefill_picklist("TypeName", "list_menu_types", wf)
        if matched:
            resolved["type"] = {
                "code":  matched.get("code"),
                "label": matched.get("label") or matched.get("name"),
            }
            wf = {**wf, "step": STEP_NATURE, "resolved": resolved, "picklist": []}
            return await self._advance_to_nature(wf)
        wf = {**wf, "step": STEP_TYPE, "resolved": resolved, "picklist": []}
        return await self._enter_picklist_step(STEP_TYPE, "list_menu_types", wf)

    async def _advance_to_nature(self, wf: dict) -> tuple[str, dict]:
        """Entry point for the Nature step: honor a pre-filled NatureName if given."""
        resolved = wf["resolved"]
        matched = await self._try_prefill_picklist("NatureName", "list_menu_natures", wf)
        if matched:
            resolved["nature"] = {
                "code":  matched.get("code"),
                "label": matched.get("label") or matched.get("name"),
            }
            pre = resolved.get("_pre", {})
            pre_sort_order = pre.get("SortOrder")
            if pre_sort_order and re.match(r"^\d+$", pre_sort_order):
                resolved["sort_order"] = int(pre_sort_order)
                wf = {**wf, "step": STEP_STATUS, "resolved": resolved, "picklist": []}
                return await self._advance_to_status(wf)
            wf = {**wf, "step": STEP_SORT_ORDER, "resolved": resolved, "picklist": []}
            return STEP_QUESTIONS[STEP_SORT_ORDER], wf
        wf = {**wf, "step": STEP_NATURE, "resolved": resolved, "picklist": []}
        return await self._enter_picklist_step(STEP_NATURE, "list_menu_natures", wf)

    async def _advance_to_status(self, wf: dict) -> tuple[str, dict]:
        resolved = wf["resolved"]
        matched = await self._try_prefill_picklist("StatusName", "list_menu_statuses", wf)
        if not matched:
            items = await self._call_tool("list_menu_statuses", {})
            if isinstance(items, list) and items:
                numbered = [dict(it, number=i + 1) for i, it in enumerate(items)]
                matched = await self._resolve_picklist_answer("Active", numbered)
        if matched:
            resolved["status"] = {
                "code":  matched.get("code"),
                "label": matched.get("label") or matched.get("name"),
            }
            pre = resolved.get("_pre", {})
            pre_from_version = pre.get("FromVersion")
            pre_to_version = pre.get("ToVersion")
            if pre_from_version:
                resolved["from_version"] = pre_from_version
                if pre_to_version:
                    resolved["to_version"] = pre_to_version
                    wf = {**wf, "step": "_mvm_collect", "resolved": resolved, "picklist": []}
                    return await self._collect_mvm_then_ms(wf)
                wf = {**wf, "step": STEP_TO_VERSION, "resolved": resolved, "picklist": []}
                return STEP_QUESTIONS[STEP_TO_VERSION], wf
            wf = {**wf, "step": STEP_FROM_VERSION, "resolved": resolved, "picklist": []}
            return STEP_QUESTIONS[STEP_FROM_VERSION], wf
        wf = {**wf, "step": STEP_STATUS, "resolved": resolved, "picklist": []}
        return await self._enter_picklist_step(STEP_STATUS, "list_menu_statuses", wf)

    async def _process_code_step(self, wf: dict) -> tuple[str, dict]:
        """Auto-generate menu code. Returns code announcement + enters caption step."""
        resolved = wf["resolved"]
        module = resolved.get("module", {})
        module_name = module.get("name", "") if isinstance(module, dict) else ""

        result = await self._call_tool("generate_menu_code", {"module_name": module_name})
        if isinstance(result, dict) and "error" in result:
            return f"Error: {result['error']}", wf
        if isinstance(result, dict) and result.get("needs_prefix_choice"):
            prefixes = result.get("available_prefixes", [])
            numbered_prefixes = [{"number": i+1, "name": p, "label": p} for i, p in enumerate(prefixes)]
            lines = [f"This module has {len(prefixes)} code prefixes: {', '.join(prefixes)}. Which one should this menu use?"]
            for p in numbered_prefixes:
                lines.append(f"{p['number']} - {p['name']}")
            lines.append("Please enter the prefix number.")
            wf = {**wf, "step": "_prefix_choice", "picklist": numbered_prefixes,
                  "_prefix_module": module_name}
            return "\n".join(lines), wf

        code = result.get("code") if isinstance(result, dict) else None
        if not code:
            return "Error: could not generate menu code.", wf

        resolved = {**resolved, "menu_code": code}
        wf = {**wf, "resolved": resolved}

        pre = resolved.get("_pre", {})
        caption = resolved.get("caption") or pre.get("Caption")
        code_line = f"Code: {code}"

        if caption:
            resolved = {**resolved, "caption": caption}
            wf = {**wf, "resolved": resolved}
            section_reply, wf = await self._advance_to_section(wf)
            return f"{code_line}\n{section_reply}", wf
        else:
            wf = {**wf, "step": STEP_CAPTION}
            return f"{code_line}\n{STEP_QUESTIONS[STEP_CAPTION]}", wf

    async def _enter_final_confirm(self, wf: dict) -> tuple[str, dict]:
        """Build the combined summary for Menu + ModuleVsMenu + MenuSet
        and present it for one final confirmation before saving anything."""
        resolved = wf.get("resolved", {})
        mvm_data = wf.get("_mvm", {})
        resolved_ms = wf.get("_ms", {})
        summary = _build_combined_summary(resolved, mvm_data, resolved_ms)
        wf = {**wf, "step": STEP_FINAL_CONFIRM}
        return summary, wf

    async def chat(
        self, history: list[dict], known_slots: dict, user_message: str
    ) -> tuple[str, list[dict], dict]:
        """
        Routes to Python-controlled Menu Creation workflow (steps 1-24 + save),
        or falls through to LLM-driven handler for ModuleVsMenu / MenuSet.
        """
        wf = _get_wf(known_slots)

        if wf.get("phase") == "post_save":
            return await self._llm_chat(history, known_slots, user_message)

        reply, new_wf = await self._handle_menu_creation(wf, history, known_slots, user_message)

        new_known_slots = _set_wf(known_slots, new_wf)
        new_history = history + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": reply},
        ]
        return reply, new_history, new_known_slots

    async def _handle_menu_creation(
        self,
        wf: dict,
        history: list[dict],
        known_slots: dict,
        user_message: str,
    ) -> tuple[str, dict]:
        """
        Python-controlled handler for all Menu Creation steps (1-24 + save).

        On FIRST message: extract any pre-supplied fields, pre-fill resolved,
        then start from the first unresolved step.

        On subsequent messages: process the user's answer for the current step,
        then advance to the next step.
        """
        resolved = dict(wf.get("resolved", {}))
        step = wf.get("step", STEP_MODULE)
        picklist = wf.get("picklist", [])

        if step == STEP_MODULE and not resolved:
            extracted = await self._llm_extract_fields(user_message)
            pre = {k: v for k, v in extracted.items() if v}
            if pre:
                resolved["_pre"] = pre
                logger.info("[PRE-FILL] extracted: %s", pre)
            resolved["_first_message"] = user_message

            # Also extract any MenuSet details given in the first message
            ms_extracted = await self._llm_extract_ms_fields(user_message)
            pre_ms = {k: v for k, v in ms_extracted.items() if v}
            if pre_ms:
                resolved["_pre_ms"] = pre_ms
                logger.info("[PRE-FILL MS] extracted: %s", pre_ms)

        if step == STEP_MODULE and not resolved.get("module"):
            if not picklist:
                pre_module_name = resolved.get("_pre", {}).get("ModuleName")
                if pre_module_name:
                    items = await self._call_tool("list_modules", {})
                    if isinstance(items, dict) and "error" in items:
                        return f"Error loading modules: {items['error']}", wf
                    if not isinstance(items, list):
                        return "Could not load the module list. Please try again.", wf
                    numbered = [dict(it, number=i + 1) for i, it in enumerate(items)]
                    matched = await self._resolve_picklist_answer(pre_module_name, numbered)
                    if not matched:
                        header, footer = PICKLIST_HEADERS[STEP_MODULE]
                        reply = _format_picklist(header, numbered, footer)
                        wf = {**wf, "step": STEP_MODULE, "resolved": resolved, "picklist": numbered}
                        return reply, wf
                else:
                    wf = {**wf, "resolved": resolved}
                    return await self._enter_module_step(wf)
            else:
                matched = await self._resolve_picklist_answer(user_message, picklist)
                if not matched:
                    wf = {**wf, "resolved": resolved}
                    return await self._enter_module_step(wf)
            resolved["module"] = {
                "id":   matched.get("id"),
                "name": matched.get("name"),
                "code": matched.get("code"),
                "number": matched.get("number"),
            }
            step = STEP_MENU_NAME

            pre = resolved.get("_pre", {})
            if pre.get("MenuName"):
                resolved["menu_name"] = pre["MenuName"]
                step = STEP_CODE
                wf = {**wf, "step": step, "resolved": resolved, "picklist": []}
                return await self._process_code_step(wf)

            wf = {**wf, "step": step, "resolved": resolved, "picklist": []}
            return STEP_QUESTIONS[STEP_MENU_NAME], wf

        elif step == STEP_MENU_NAME:
            resolved["menu_name"] = user_message.strip()
            step = STEP_CODE
            wf = {**wf, "step": step, "resolved": resolved, "picklist": []}
            return await self._process_code_step(wf)

        elif step == "_prefix_choice":
            module_name = wf.get("_prefix_module", "")
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if not matched:
                return "Please enter a valid prefix number.", wf
            preferred = matched.get("name") or matched.get("label")
            result = await self._call_tool("generate_menu_code", {
                "module_name": module_name,
                "preferred_prefix": preferred,
            })
            if isinstance(result, dict) and "error" in result:
                return f"Error: {result['error']}", wf
            code = result.get("code") if isinstance(result, dict) else None
            if not code:
                return "Error: could not generate menu code.", wf
            resolved = {**resolved, "menu_code": code}
            wf = {**wf, "resolved": resolved}
            code_line = f"Code: {code}"
            pre = resolved.get("_pre", {})
            caption = resolved.get("caption") or pre.get("Caption")
            if caption:
                resolved = {**resolved, "caption": caption}
                wf = {**wf, "resolved": resolved}
                section_reply, wf = await self._advance_to_section(wf)
                return f"{code_line}\n{section_reply}", wf
            else:
                wf = {**wf, "step": STEP_CAPTION, "picklist": []}
                return f"{code_line}\n{STEP_QUESTIONS[STEP_CAPTION]}", wf

        elif step == STEP_CAPTION:
            resolved["caption"] = user_message.strip()
            wf = {**wf, "resolved": resolved}
            return await self._advance_to_section(wf)

        elif step == STEP_SECTION:
            if not picklist:
                wf = {**wf, "resolved": resolved}
                return await self._enter_section_step(wf)
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if not matched:
                wf = {**wf, "resolved": resolved}
                return await self._enter_section_step(wf)
            resolved["section"] = {
                "id":   matched.get("id"),
                "name": matched.get("name"),
                "code": matched.get("code"),
            }
            wf = {**wf, "resolved": resolved}
            return await self._advance_to_under(wf)

        elif step == STEP_UNDER:
            if not picklist:
                wf = {**wf, "resolved": resolved}
                return await self._enter_under_step(wf)
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if not matched:
                wf = {**wf, "resolved": resolved}
                return await self._enter_under_step(wf)
            resolved["under"] = {
                "id":   matched.get("id"),
                "name": matched.get("name"),
                "code": matched.get("code"),
            }
            wf = {**wf, "resolved": resolved}
            return await self._advance_to_type(wf)

        elif step == STEP_TYPE:
            if not picklist:
                wf = {**wf, "resolved": resolved}
                return await self._enter_picklist_step(STEP_TYPE, "list_menu_types", wf)
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if not matched:
                wf = {**wf, "resolved": resolved}
                return await self._enter_picklist_step(STEP_TYPE, "list_menu_types", wf)
            resolved["type"] = {
                "code":  matched.get("code"),
                "label": matched.get("label") or matched.get("name"),
            }
            wf = {**wf, "resolved": resolved}
            return await self._advance_to_nature(wf)

        elif step == STEP_NATURE:
            if not picklist:
                wf = {**wf, "resolved": resolved}
                return await self._enter_picklist_step(STEP_NATURE, "list_menu_natures", wf)
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if not matched:
                wf = {**wf, "resolved": resolved}
                return await self._enter_picklist_step(STEP_NATURE, "list_menu_natures", wf)
            resolved["nature"] = {
                "code":  matched.get("code"),
                "label": matched.get("label") or matched.get("name"),
            }
            pre = resolved.get("_pre", {})
            pre_sort_order = pre.get("SortOrder")
            if pre_sort_order and re.match(r"^\d+$", pre_sort_order):
                resolved["sort_order"] = int(pre_sort_order)
                wf = {**wf, "resolved": resolved}
                return await self._advance_to_status(wf)

            wf = {**wf, "step": STEP_SORT_ORDER, "resolved": resolved, "picklist": []}
            return STEP_QUESTIONS[STEP_SORT_ORDER], wf

        elif step == STEP_SORT_ORDER:
            val = user_message.strip()
            if not re.match(r"^\d+$", val):
                return "Please enter a whole number for sort order.", wf
            resolved["sort_order"] = int(val)
            wf = {**wf, "resolved": resolved}
            return await self._advance_to_status(wf)

        elif step == STEP_STATUS:
            if not picklist:
                wf = {**wf, "resolved": resolved}
                return await self._enter_picklist_step(STEP_STATUS, "list_menu_statuses", wf)
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if not matched:
                wf = {**wf, "resolved": resolved}
                return await self._enter_picklist_step(STEP_STATUS, "list_menu_statuses", wf)
            resolved["status"] = {
                "code":  matched.get("code"),
                "label": matched.get("label") or matched.get("name"),
            }
            step = STEP_FROM_VERSION

            pre = resolved.get("_pre", {})
            pre_from_version = pre.get("FromVersion")
            pre_to_version = pre.get("ToVersion")

            if pre_from_version:
                resolved["from_version"] = pre_from_version
                if pre_to_version:
                    resolved["to_version"] = pre_to_version
                    wf = {**wf, "step": "_mvm_collect", "resolved": resolved, "picklist": []}
                    return await self._collect_mvm_then_ms(wf)
                step = STEP_TO_VERSION
                wf = {**wf, "step": step, "resolved": resolved, "picklist": []}
                return STEP_QUESTIONS[STEP_TO_VERSION], wf

            wf = {**wf, "step": step, "resolved": resolved, "picklist": []}
            return STEP_QUESTIONS[STEP_FROM_VERSION], wf

        elif step == STEP_FROM_VERSION:
            resolved["from_version"] = user_message.strip()
            step = STEP_TO_VERSION

            pre = resolved.get("_pre", {})
            pre_to_version = pre.get("ToVersion")
            if pre_to_version:
                resolved["to_version"] = pre_to_version
                wf = {**wf, "step": "_mvm_collect", "resolved": resolved, "picklist": []}
                return await self._collect_mvm_then_ms(wf)

            wf = {**wf, "step": step, "resolved": resolved, "picklist": []}
            return STEP_QUESTIONS[STEP_TO_VERSION], wf

        elif step == STEP_TO_VERSION:
            resolved["to_version"] = user_message.strip()
            wf = {**wf, "step": "_mvm_collect", "resolved": resolved, "picklist": []}
            return await self._collect_mvm_then_ms(wf)

        elif step == STEP_CONFIRM:
            if _is_change_request(user_message) or _is_negative(user_message):
                wf = {**wf, "step": "_change_field", "resolved": resolved}
                return "Which field would you like to change?", wf
            else:
                wf = {**wf, "step": "_mvm_collect", "resolved": resolved}
                return await self._collect_mvm_then_ms(wf)

        elif step == "_change_field":
            changed_step = self._detect_change_field(user_message)
            if changed_step:
                resolved_copy = dict(resolved)
                key = STEP_RESOLVED_KEY.get(changed_step)
                if key:
                    resolved_copy.pop(key, None)
                wf = {**wf, "step": changed_step, "resolved": resolved_copy, "picklist": []}
                return await self._enter_step_fresh(changed_step, wf)
            else:
                return "I didn't understand which field to change. Please name the field (e.g. 'module', 'caption', 'type').", wf

        elif step == "_mvm_collect":
            wf = {**wf, "step": STEP_MS_COLLECT}
            return await self._handle_menu_creation(wf, history, known_slots, user_message)

        elif step == STEP_MS_COLLECT:
            resolved_ms = dict(wf.get("_ms", {}))
            pending = wf.get("_ms_pending", "name")
            picklist = wf.get("picklist", [])

            direct = await self._resolve_picklist_answer(user_message, picklist)
            if direct is not None:
                resolved_ms[pending] = direct

            if resolved_ms.get("name") and (not resolved_ms.get("client") or not resolved_ms.get("module")):
                resolved_ms = await self._ms_autofill_from_name(resolved_ms)

            if not resolved_ms.get(pending):
                prompt, picklist = await self._ms_prompt_for(pending, resolved_ms)
                wf = {**wf, "_ms": resolved_ms, "picklist": picklist}
                return prompt, wf

            next_field = _ms_next_field(resolved_ms)
            if next_field:
                prompt, picklist = await self._ms_prompt_for(next_field, resolved_ms)
                wf = {
                    **wf, "step": STEP_MS_COLLECT, "_ms": resolved_ms,
                    "picklist": picklist, "_ms_pending": next_field,
                }
                return prompt, wf

            return await self._enter_ms_visible_step(wf, resolved_ms)

        elif step == STEP_MS_PARENT:
            resolved_ms = dict(wf.get("_ms", {}))
            picklist = wf.get("picklist", [])
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if matched is None:
                return _format_tree_picklist("Select Parent Menu", picklist), wf
            resolved_ms["parent"] = matched
            children = _numbered_tree_items(_menu_tree_level_items(matched.get("children") or []))
            if not children:
                resolved_ms["visible_code"] = 1
                resolved_ms["visible_label"] = "Yes"
                wf = {**wf, "_ms": resolved_ms}
                return await self._enter_final_confirm(wf)
            wf = {**wf, "step": STEP_MS_SUB, "_ms": resolved_ms, "picklist": children}
            return _format_tree_picklist("Select Sub Menu", children), wf

        elif step == STEP_MS_SUB:
            resolved_ms = dict(wf.get("_ms", {}))
            picklist = wf.get("picklist", [])
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if matched is None:
                return _format_tree_picklist("Select Sub Menu", picklist), wf
            resolved_ms["sub_menu"] = matched
            children = _numbered_tree_items(_menu_tree_level_items(matched.get("children") or []))
            if not children:
                resolved_ms["visible_code"] = 1
                resolved_ms["visible_label"] = "Yes"
                wf = {**wf, "_ms": resolved_ms}
                return await self._enter_final_confirm(wf)
            wf = {**wf, "step": STEP_MS_CHILD, "_ms": resolved_ms, "picklist": children}
            return _format_tree_picklist("Select Child Menu", children), wf

        elif step == STEP_MS_CHILD:
            resolved_ms = dict(wf.get("_ms", {}))
            picklist = wf.get("picklist", [])
            matched = await self._resolve_picklist_answer(user_message, picklist)
            if matched is None:
                return _format_tree_picklist("Select Child Menu", picklist), wf
            resolved_ms["child"] = matched
            resolved_ms["visible_code"] = 1
            resolved_ms["visible_label"] = "Yes"
            wf = {**wf, "_ms": resolved_ms}
            return await self._enter_final_confirm(wf)

        elif step == STEP_MS_VISIBLE:
            visible = _resolve_ms_visibility(user_message)
            if not visible:
                return "Visible?\n\n1. Yes\n2. No\n3. Optional", wf
            visible_code, visible_label = visible
            resolved_ms = dict(wf.get("_ms", {}))
            resolved_ms["visible_code"] = visible_code
            resolved_ms["visible_label"] = visible_label
            wf = {**wf, "_ms": resolved_ms}
            return await self._enter_final_confirm(wf)

        elif step == STEP_FINAL_CONFIRM:
            resolved_ms = wf.get("_ms", {})
            if _is_affirmative(user_message) or user_message.strip() == "1":
                return await self._do_save_all(wf)
            elif _is_negative(user_message) or user_message.strip() == "2":
                wf = {**wf, "step": STEP_POST_SAVE, "phase": "post_save"}
                return "Cancelled. Nothing was saved.", wf
            else:
                return await self._enter_final_confirm(wf)

        elif step == STEP_MS_CONFIRM:
            return await self._enter_final_confirm(wf)

        elif step == STEP_SAVE:
            return await self._do_save(wf)

        else:
            logger.error("[WORKFLOW] Unknown step %r — restarting", step)
            wf = {"step": STEP_MODULE, "resolved": {}, "picklist": [], "phase": "menu_creation"}
            return await self._enter_module_step(wf)

    async def _collect_mvm_then_ms(self, wf: dict) -> tuple[str, dict]:
        """After the user confirms the menu summary, compute ModuleVsMenu
        data silently, then start collecting MenuSet details.
        Nothing is saved here — all saving happens at STEP_FINAL_CONFIRM."""
        resolved = wf.get("resolved", {})
        module = resolved.get("module", {}) if isinstance(resolved.get("module"), dict) else {}
        module_name = module.get("name", "")
        menu_name = resolved.get("menu_name", "")
        menu_code = resolved.get("menu_code", "")

        under = resolved.get("under", {}) if isinstance(resolved.get("under"), dict) else {}
        under_id = under.get("id")
        under_label = under.get("name") or "NONE"

        menu_id = None
        seq_no = None

        mvm_data = {
            "module_name": module_name,
            "menu_name": menu_name,
            "menu_id": menu_id,
            "under_id": under_id,
            "under_label": under_label,
            "seq_no": seq_no,
        }

        resolved_ms = {
            "_menu_id": menu_id,
            "_menu_name": menu_name,
            "_menu_module_name": module_name,
        }

        resolved_ms["applicable_type"] = _MS_APPLICABLE_TYPE_DEFAULT
        menu_module = resolved.get("module", {})
        if isinstance(menu_module, dict) and menu_module.get("id"):
            resolved_ms["module"] = menu_module

        wf = {**wf, "_mvm": mvm_data, "_ms": resolved_ms}

        next_field = _ms_next_field(resolved_ms)

        if next_field:
            prompt, pl = await self._ms_prompt_for(next_field, resolved_ms)
            intro = f"{_MS_INTRO_HEADER}\n\n{prompt}"
            wf = {
                **wf, "step": STEP_MS_COLLECT,
                "picklist": pl, "_ms_pending": next_field,
            }
            return intro, wf

        if not resolved_ms.get("visible_code"):
            located_wf, located_ms, err = await self._locate_for_visible(wf, resolved_ms)
            if err:
                return err, wf
            wf = located_wf
            resolved_ms = located_ms
            resolved_ms["visible_code"] = 1
            resolved_ms["visible_label"] = "Yes"
            wf = {**wf, "_ms": resolved_ms}

        return await self._enter_final_confirm(wf)

    async def _locate_for_visible(self, wf: dict, resolved_ms: dict) -> tuple[dict, dict, str | None]:
        """Run locate_menu_in_menuset and update resolved_ms._menu_name.
        Returns (wf, resolved_ms, error_string_or_None).
        Skipped if menu_id is None (menu not saved yet); locate is done in
        _do_save_all after the menu is actually created."""
        menu_id = resolved_ms.get("_menu_id")
        if not menu_id:
            return wf, resolved_ms, None
        menuset_id = (resolved_ms.get("name") or {}).get("id")
        module_id = (resolved_ms.get("module") or {}).get("id")
        menu_module_name = resolved_ms.get("_menu_module_name")

        located = await self._call_tool("locate_menu_in_menuset", {
            "menuset_id": menuset_id,
            "module_id": module_id,
            "menu_id": menu_id,
            "menu_module_name": menu_module_name,
        })
        if isinstance(located, dict) and "error" in located:
            return wf, resolved_ms, f"Error locating menu in MenuSet: {located['error']}"
        if isinstance(located, dict) and located.get("menu_name"):
            resolved_ms = dict(resolved_ms)
            resolved_ms["_menu_name"] = located.get("menu_name")
        return wf, resolved_ms, None

    async def _do_save_all(self, wf: dict) -> tuple[str, dict]:
        """User confirmed yes on the combined summary.
        Save Menu → ModuleVsMenu → MenuSet in sequence."""
        resolved = wf.get("resolved", {})
        mvm_data = wf.get("_mvm", {})
        resolved_ms = wf.get("_ms", {})

        # --- 1. Save Menu ---
        fields = _build_create_fields(resolved)
        logger.info("[SAVE ALL] calling create_menu with fields: %s", fields)
        result = await self._call_tool("create_menu", {"fields": fields})
        if isinstance(result, dict) and "error" in result:
            wf = {**wf, "step": STEP_FINAL_CONFIRM}
            return (
                f"Error saving menu: {result['error']}\n\nWould you like to retry? (yes/no)"
            ), wf

        menu_name = resolved.get("menu_name", "")
        module_name = mvm_data.get("module_name", "")
        menu_code = resolved.get("menu_code", "")
        success_parts = [f"Menu '{menu_name}' created successfully."]

        # --- 2. Get the real menu_id now that menu is saved ---
        id_result = await self._call_tool("find_new_menu_id", {
            "module_name": module_name,
            "menu_code": menu_code,
        })
        menu_id = id_result.get("menu_id") if isinstance(id_result, dict) and "error" not in id_result else mvm_data.get("menu_id")

        mvm_data = {**mvm_data, "menu_id": menu_id}
        resolved_ms = {**resolved_ms, "_menu_id": menu_id}

        # --- 2b. Now locate the menu in the MenuSet (we have a real menu_id) ---
        if menu_id:
            menuset_id_pre = (resolved_ms.get("name") or {}).get("id")
            module_id_pre = (resolved_ms.get("module") or {}).get("id")
            located = await self._call_tool("locate_menu_in_menuset", {
                "menuset_id": menuset_id_pre,
                "module_id": module_id_pre,
                "menu_id": menu_id,
                "menu_module_name": resolved_ms.get("_menu_module_name"),
            })
            if isinstance(located, dict) and located.get("menu_name"):
                resolved_ms = {**resolved_ms, "_menu_name": located.get("menu_name")}

        # --- 3. Save ModuleVsMenu ---
        mvm_result = await self._call_tool("add_menu_to_module_tree", {
            "module_name": mvm_data.get("module_name"),
            "menu_id": menu_id,
            "under_id": mvm_data.get("under_id"),
        })
        if isinstance(mvm_result, dict) and "error" in mvm_result:
            wf = {**wf, "step": STEP_FINAL_CONFIRM, "_mvm": mvm_data, "_ms": resolved_ms}
            return (
                f"Menu saved but error saving ModuleVsMenu: {mvm_result['error']}\n\n"
                "Would you like to retry? (yes/no)"
            ), wf
        success_parts.append(f"ModuleVsMenu mapping saved.")

        # --- 4. Save MenuSet ---
        menuset_id = (resolved_ms.get("name") or {}).get("id")
        module_id = (resolved_ms.get("module") or {}).get("id")
        visible_code = resolved_ms.get("visible_code")

        ms_result = await self._call_tool("save_menuset_visibility", {
            "menuset_id": menuset_id,
            "module_id": module_id,
            "menu_id": menu_id,
            "visible_code": visible_code,
        })
        if isinstance(ms_result, dict) and "error" in ms_result:
            wf = {**wf, "step": STEP_FINAL_CONFIRM, "_mvm": mvm_data, "_ms": resolved_ms}
            return (
                f"Menu and ModuleVsMenu saved but error saving MenuSet: {ms_result['error']}\n\n"
                "Would you like to retry? (yes/no)"
            ), wf

        menuset_name = (resolved_ms.get("name") or {}).get("name", "NONE")
        visible_label = resolved_ms.get("visible_label", "NONE")
        success_parts.append(
            f"MenuSet '{menuset_name}': '{menu_name}' is now Visible = {visible_label}."
        )

        wf = {**wf, "step": STEP_POST_SAVE, "phase": "post_save"}
        return "\n".join(success_parts), wf

    async def _do_save(self, wf: dict) -> tuple[str, dict]:
        """Legacy single save — kept for safety but normal flow uses _do_save_all."""
        resolved = wf["resolved"]
        fields = _build_create_fields(resolved)
        logger.info("[SAVE] calling create_menu with fields: %s", fields)
        result = await self._call_tool("create_menu", {"fields": fields})

        if isinstance(result, dict) and "error" in result:
            err = result["error"]
            wf = {**wf, "step": STEP_CONFIRM}
            return f"Error saving menu: {err}\n\nWould you like to retry? (yes/no)", wf

        menu_name = resolved.get("menu_name", "")
        module = resolved.get("module", {}) if isinstance(resolved.get("module"), dict) else {}
        module_name = module.get("name", "")
        menu_code = resolved.get("menu_code", "")
        success_msg = f"Successfully created menu '{menu_name}' for module {module_name} (Code: {menu_code})."

        under = resolved.get("under", {}) if isinstance(resolved.get("under"), dict) else {}
        under_id = under.get("id")
        under_label = under.get("name") or "NONE"

        id_result = await self._call_tool("find_new_menu_id", {
            "module_name": module_name,
            "menu_code": menu_code,
        })
        if isinstance(id_result, dict) and "error" in id_result:
            wf = {**wf, "step": STEP_POST_SAVE, "phase": "post_save"}
            return (
                f"{success_msg}\n\nCould not prepare the ModuleVsMenu mapping: "
                f"{id_result['error']}"
            ), wf
        menu_id = id_result.get("menu_id") if isinstance(id_result, dict) else None

        mappings = await self._call_tool("list_module_menu_mappings", {"module_name": module_name})
        seq_no = (len(mappings) + 1) if isinstance(mappings, list) else 1

        mvm_summary = (
            "ModuleVsMenu Summary\n\n"
            f"Module        : {module_name}\n"
            f"Menu          : {menu_name}\n"
            f"Parent Menu   : {under_label}\n"
            f"Sequence No   : {seq_no}"
        )

        wf = {
            **wf,
            "step": "_mvm_confirm",
            "resolved": resolved,
            "picklist": [],
            "_mvm": {
                "module_name": module_name,
                "menu_name": menu_name,
                "menu_id": menu_id,
                "under_id": under_id,
                "under_label": under_label,
            },
        }
        reply = (
            f"{success_msg}\n\n"
            f"{mvm_summary}\n\n"
            "Do you want to save this ModuleVsMenu mapping? (Yes / No)"
        )
        return reply, wf

    async def _enter_step_fresh(self, step: str, wf: dict) -> tuple[str, dict]:
        """Enter a step from scratch (used after a change request)."""
        if step == STEP_MODULE:
            return await self._enter_module_step(wf)
        elif step == STEP_MENU_NAME:
            wf = {**wf, "step": STEP_MENU_NAME}
            return STEP_QUESTIONS[STEP_MENU_NAME], wf
        elif step == STEP_CAPTION:
            wf = {**wf, "step": STEP_CAPTION}
            return STEP_QUESTIONS[STEP_CAPTION], wf
        elif step == STEP_SECTION:
            return await self._enter_section_step(wf)
        elif step == STEP_UNDER:
            return await self._enter_under_step(wf)
        elif step == STEP_TYPE:
            return await self._enter_picklist_step(STEP_TYPE, "list_menu_types", wf)
        elif step == STEP_NATURE:
            return await self._enter_picklist_step(STEP_NATURE, "list_menu_natures", wf)
        elif step == STEP_SORT_ORDER:
            wf = {**wf, "step": STEP_SORT_ORDER}
            return STEP_QUESTIONS[STEP_SORT_ORDER], wf
        elif step == STEP_STATUS:
            return await self._enter_picklist_step(STEP_STATUS, "list_menu_statuses", wf)
        elif step == STEP_FROM_VERSION:
            wf = {**wf, "step": STEP_FROM_VERSION}
            return STEP_QUESTIONS[STEP_FROM_VERSION], wf
        elif step == STEP_TO_VERSION:
            wf = {**wf, "step": STEP_TO_VERSION}
            return STEP_QUESTIONS[STEP_TO_VERSION], wf
        else:
            return "Which field would you like to change?", wf

    def _detect_change_field(self, user_message: str) -> str | None:
        """Map a user's change-request text to a step name."""
        low = user_message.strip().lower()
        if "module" in low:
            return STEP_MODULE
        if "menu name" in low or "name" in low:
            return STEP_MENU_NAME
        if "caption" in low:
            return STEP_CAPTION
        if "section" in low:
            return STEP_SECTION
        if "under" in low or "parent" in low:
            return STEP_UNDER
        if "type" in low:
            return STEP_TYPE
        if "nature" in low:
            return STEP_NATURE
        if "sort" in low:
            return STEP_SORT_ORDER
        if "status" in low:
            return STEP_STATUS
        if "from version" in low or "from_version" in low:
            return STEP_FROM_VERSION
        if "to version" in low or "to_version" in low:
            return STEP_TO_VERSION
        return None


    async def _llm_chat(
        self, history: list[dict], known_slots: dict, user_message: str
    ) -> tuple[str, list[dict], dict]:
        """
        Deterministic post-save handler.

        By the time `phase == "post_save"`, the Menu/ModuleVsMenu/MenuSet
        workflow has already finished (success message or explicit
        cancellation was already returned by _do_save_all / the final-
        confirm step and appended to history). There is nothing left in
        this conversation that requires calling an MCP tool -- the only
        remaining job is to acknowledge and offer what's next. This
        replaces the former autonomous tool-calling loop (which could
        decide on its own which MCP tool to invoke) with plain
        Python-controlled branching, matching how every other step in
        this workflow is already handled.
        """
        wf = _get_wf(known_slots)

        def _finish(reply: str, new_wf: dict | None = None) -> tuple[str, list[dict], dict]:
            new_history = history + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply},
            ]
            new_known_slots = _set_wf(known_slots, new_wf) if new_wf is not None else known_slots
            return reply, new_history, new_known_slots

        if _is_affirmative(user_message):
            return _finish(
                "Sure — let's create another menu. Please enter module name.",
                new_wf={"step": STEP_MODULE},
            )

        if _is_negative(user_message):
            return _finish("Okay, done for now. Let me know if you need anything else.")

        return _finish(
            "Would you like to create another menu? (yes/no)"
        )



def _mcp_tools_to_openai(mcp_tools) -> list[dict]:
    out = []
    for t in mcp_tools:
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        })
    return out