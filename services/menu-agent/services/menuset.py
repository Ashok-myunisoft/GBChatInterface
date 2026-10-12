from __future__ import annotations

import json
import time

from menu_agent_config.settings import CONFIG_DIR
from services.client import client
from services.enums import numbered_options

_ENUMS_PATH = CONFIG_DIR / "menuset_enums.json"

with open(_ENUMS_PATH) as f:
    _MENUSET_ENUMS = json.load(f)

APPLICABLE_TYPE = _MENUSET_ENUMS["ApplicableType"]
APPLICABLE_TYPE_DEFAULT = _MENUSET_ENUMS["ApplicableType_default"]
VISIBLE = _MENUSET_ENUMS["Visible"]

VISIBLE_YES = 1
VISIBLE_NO = 2
VISIBLE_OPTIONAL = 3


class MenuSetError(Exception):
    pass


def _numbered(items, id_key="Id", name_key="Name", code_key="Code", extra=None):
    """extra: optional {output_key: source_key} map for pulling additional
    fields off the raw row (e.g. Module info riding along on a MenuSet
    row) without disturbing the base id/name/code shape every other
    caller already depends on."""
    if not items:
        return []
    out = []
    for i, item in enumerate(items, start=1):
        row = {
            "number": i,
            "id": item.get(id_key),
            "name": item.get(name_key),
            "code": item.get(code_key),
        }
        if extra:
            for out_key, src_key in extra.items():
                row[out_key] = item.get(src_key)
        out.append(row)
    return out


def list_applicable_types():
    return numbered_options(APPLICABLE_TYPE)


def list_visible_options():
    return numbered_options(VISIBLE)


def get_clients():
    """
    Client dropdown for the MenuSet page's 'Client *' field.
    NOT independently confirmed via HAR capture -- no recording of
    this exact call was made. Inferred by matching the identical
    SectionCriteriaList/AttributesCriteriaList shape confirmed on
    every other GB4 *.svc/SelectList endpoint in this project
    (Module.svc, MenuSet.svc, GCM.svc, etc). If results look wrong,
    capture one real "open the Client dropdown" HAR entry and fix
    this function against it.
    """
    body = {
        "SectionCriteriaList": [{
            "SectionId": 0,
            "AttributesCriteriaList": [{
                "FieldName": "Name",
                "OperationType": 2,
                "FieldValue": "",
                "InArray": None,
                "JoinType": 0,
            }],
            "OperationType": 0,
        }]
    }
    data = client.post("Client.svc/SelectList", body)
    return _numbered(data)


def get_menuset_names(client_id: int | None = None):
    """
    Name dropdown. The returned "id" IS the MenuSetId.

    Each Name already encodes both its Client and Module (e.g.
    "DEMO-ADMIN", "AProduction") -- Name alone is enough to resolve
    both later via get_menuset_detail, so client_id is optional here.

    IMPORTANT -- this list is NOT filtered by Module at all (only
    optionally by Client). A MenuSet is its own record, permanently
    tied to one Module, independent of whatever module the user's
    menu lives in -- so this list can (and does) mix in names that
    belong to a completely different module. Previously nothing
    surfaced that until the much later locate_menu_in_menuset call
    failed with a confusing "Module mismatch" error.

    Fix: each raw row is also probed for a Module field (best-effort,
    key name not independently HAR-confirmed the way Id/Name/Client.Id
    are -- if GB4 doesn't actually return one here, "module_name"/
    "module_id" just come back None and every caller already treats
    that as "unknown, don't filter or claim anything"). When present,
    mcp_server.py's list_menuset_names() tool uses it to pre-filter to
    the requested module so the user is never even offered a
    mismatched Name in the first place.
    """
    attrs = []
    if client_id is not None:
        attrs.append({
            "FieldName": "Client.Id",
            "OperationType": 5,
            "FieldValue": str(client_id),
            "InArray": None,
            "JoinType": 2,
        })
    attrs.append({
        "FieldName": "Name",
        "OperationType": 2,
        "FieldValue": "",
        "InArray": None,
        "JoinType": 0,
    })
    body = {
        "SectionCriteriaList": [{
            "SectionId": 0,
            "AttributesCriteriaList": attrs,
            "OperationType": 0,
        }]
    }
    data = client.post("MenuSet.svc/SelectList", body)
    return _numbered(data, extra={"module_name": "ModuleName", "module_id": "ModuleId"})


def get_menuset_detail(menuset_id: int) -> dict:
    """
    Full existing MenuSet record. Confirmed shape from HAR
    (GET MenuSet.svc/?MenuSetId=<id>). This single call is what
    auto-fills Module once Name is picked -- it already contains
    ModuleId/ModuleCode/ModuleName, ClientId/Code/Name,
    MenuSetApplicableType, and the CURRENT MenuSetDetailArray.

    Confirmed by diffing a real save: a row is present in
    MenuSetDetailArray if and only if its Visible is "Yes" -- there is
    no server-side IsVisible flag actually honoured. "No" and
    "Optional" both simply mean the row is absent from this array
    (confirmed identical for both by a live test toggle to Optional).
    """
    detail = client.get(f"MenuSet.svc/?MenuSetId={menuset_id}")
    if not isinstance(detail, dict) or "MenuSetId" not in detail:
        raise MenuSetError(f"Could not load MenuSet {menuset_id} -- got: {detail!r}")
    return detail


def get_menu_catalog(module_id: int, menuset_id: int) -> list[dict]:
    """
    Every menu item that exists for this module, flat, regardless of
    current visibility. Confirmed shape from HAR (GET
    Menu.svc/MenuObject/?ModuleId=<id>&MenuSetId=<id>). This is the
    source of a menu's display name/key/sort order/report format/
    object model/report view when building a new MenuSetDetailArray
    row for it.
    """
    data = client.get(f"Menu.svc/MenuObject/?ModuleId={module_id}&MenuSetId={menuset_id}")
    return data if isinstance(data, list) else []


def find_menu_in_catalog(module_id: int, menuset_id: int, menu_id: int) -> dict:
    """Locates one specific MenuId and returns the fields needed to
    build a MenuSetDetailArray row for it.

    This used to only scan Menu.svc/MenuObject/?ModuleId=..&MenuSetId=..
    (the "menu catalog" for a module/menuset pair) for the MenuId. In
    practice that catalog endpoint can lag well behind a fresh
    add_menu_to_module_tree() call -- a menu that already shows up in
    the raw ModuleVsMenu grid (GB4's own Module Vs Menu screen, which
    reads ModuleVsMenu.svc/GetModuleVsMenuWithModule) can still be
    missing from Menu.svc/MenuObject for longer than is practical to
    retry for.

    Fixed approach: confirm real module-tree membership against
    ModuleVsMenu.svc/GetModuleVsMenuWithModule -- the same endpoint
    GB4's own Module Vs Menu screen reads, and the same one
    add_menu_to_module() itself already reads right after a tree add
    to avoid duplicate rows, so it's known to be immediately
    consistent. Then pull the menu's own fields straight from
    Menu.svc/?MenuId=<id> -- another confirmed, immediately-consistent
    endpoint already used elsewhere in this project (module_vs_menu.py)
    to resolve an Under/parent menu right after it's picked. Only
    falls back to the old catalog scan (with a short retry) if this
    direct path doesn't find it, in case some module/menuset
    combination genuinely still needs it.
    """
    from services.module_vs_menu import get_module_menu_mappings

    mapped = any(
        str(row.get("MenuId")) == str(menu_id)
        for row in get_module_menu_mappings(module_id)
    )
    if mapped:
        detail = client.get(f"Menu.svc/?MenuId={menu_id}")
        if isinstance(detail, dict) and str(detail.get("MenuId")) == str(menu_id):
            return detail

    attempts = 4
    delay_seconds = 1.5
    for attempt in range(1, attempts + 1):
        for item in get_menu_catalog(module_id, menuset_id):
            if isinstance(item, dict) and str(item.get("MenuId")) == str(menu_id):
                return item
        if attempt < attempts:
            time.sleep(delay_seconds)

    raise MenuSetError(
        f"MenuId {menu_id} was not found in module {module_id}'s menu "
        "catalog -- it may not have been added to this module's tree "
        "yet (check add_menu_to_module_tree ran first)."
    )