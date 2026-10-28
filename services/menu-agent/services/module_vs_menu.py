from __future__ import annotations

from services.client import client
from services import picklists

NO_PARENT_ID = -1
NO_PARENT_LEVEL = 8


class ModuleVsMenuError(Exception):
    pass


def get_module_menu_mappings(module_id: int) -> list[dict]:
    """Raw existing ModuleVsMenu rows for one module, GB4's own shape."""
    data = client.get(f"ModuleVsMenu.svc/GetModuleVsMenuWithModule/?ModuleId={module_id}")
    return data if isinstance(data, list) else []


def find_menu_id_by_code(module_id: int, menu_code: str) -> int | None:
    """
    Look up a menu's real MenuId by its Code.

    GB4's Menu.svc/ save response never returns the newly created
    MenuId (unconfirmed/unresolved -- see this project's README
    "Still open" list), so after create_menu succeeds, look the new
    menu up by the code we generated for it ourselves. Uses
    picklists.get_menu_by_code(), NOT get_parent_menus() -- a freshly
    created menu isn't wired into the module's ModuleVsMenu tree yet
    (that's what add_menu_to_module_tree does next), so
    get_parent_menus()/MenuTree structurally cannot see it yet.
    module_id is accepted for API compatibility with callers but is
    not used for filtering -- individual Menu rows don't carry a real
    ModuleId (it's -1 on every row), so the code is the only reliable
    filter here.

    picklists.get_menu_by_code() no longer guesses a single field name
    for "the id" (that was wrong twice already -- see picklists.py's
    docstring there); it instead returns a list of plausible
    id_candidates per matching row. Each candidate is cross-checked
    here against Menu.svc/?MenuId=<candidate>, which IS a confirmed
    shape (it's the same call _build_new_row below already uses
    successfully for the "Under" menu, and it's known to key its
    result "MenuId") -- the first candidate that both (a) resolves to
    a real menu via that endpoint and (b) round-trips to the SAME
    candidate id is trusted as the real MenuId.
    """
    for row in picklists.get_menu_by_code(menu_code):
        for candidate_id in row.get("id_candidates", []):
            try:
                detail = client.get(f"Menu.svc/?MenuId={candidate_id}")
            except Exception:
                continue
            if isinstance(detail, dict) and str(detail.get("MenuId")) == str(candidate_id):
                return candidate_id
    return None


def _existing_row_to_post_shape(row: dict) -> dict:
    return {
        "ModuleVsMenuId": str(row.get("ModuleVsMenuId")),
        "ModuleId": str(row.get("ModuleId")),
        "ModuleVsMenuSlNo": row.get("ModuleVsMenuSlNo"),
        "undefined": "",
        "MenuId": str(row.get("MenuId")),
        "ParentIdd": str(row.get("ParentId")),
        "ParentId": str(row.get("ParentId")),
        "ModuleVsMenuParentIds": row.get("ModuleVsMenuParentIds", ""),
        "ModuleVsMenuLevels": str(row.get("ModuleVsMenuLevels")),
    }


def _build_new_row(module_id: int, menu_id: int, under_id: int | None) -> dict:
    if under_id is None or int(under_id) == NO_PARENT_ID:
        resolved_under_id = NO_PARENT_ID
        parent_ids = ""
        level = NO_PARENT_LEVEL
    else:
        under_menu = client.get(f"Menu.svc/?MenuId={under_id}")
        if not isinstance(under_menu, dict) or "MenuId" not in under_menu:
            raise ModuleVsMenuError(
                f"Could not resolve Under menu id {under_id} via Menu.svc -- "
                f"got: {under_menu!r}"
            )
        resolved_under_id = under_menu["MenuId"]
        parent_ids = f"{under_menu.get('MenuParentIds', '')},{resolved_under_id}"
        level = int(under_menu.get("MenuLevel", 0)) + 1

    return {
        "ModuleVsMenuId": "0",
        "ModuleId": str(module_id),
        "ModuleVsMenuSlNo": 0,
        "undefined": "",
        "MenuId": str(menu_id),
        "ParentIdd": str(resolved_under_id),
        "ParentId": str(resolved_under_id),
        "ModuleVsMenuParentIds": parent_ids,
        "ModuleVsMenuLevels": str(level),
    }


def add_menu_to_module(module_id: int, menu_id: int, under_id: int | None = None) -> dict:
    """
    Adds one menu into a module's ModuleVsMenu tree. Resends the WHOLE
    grid, per GB4's own confirmed save behaviour -- there is no
    single-row endpoint for this table.
    """
    existing = get_module_menu_mappings(module_id)

    for row in existing:
        if str(row.get("MenuId")) == str(menu_id):
            return {
                "already_mapped": True,
                "module_id": module_id,
                "menu_id": menu_id,
                "existing_row": row,
                "message": "This menu is already mapped into this module's ModuleVsMenu tree.",
            }

    rows = [_existing_row_to_post_shape(r) for r in existing]
    rows.append(_build_new_row(module_id, menu_id, under_id))

    for i, row in enumerate(rows, start=1):
        row["ModuleVsMenuSlNo"] = i

    return client.post("ModuleVsMenu.svc/", rows)