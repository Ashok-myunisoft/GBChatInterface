from __future__ import annotations

from services.client import client


class ModuleVsMenuValidationError(Exception):
    pass


def get_existing_grid(module_id: int) -> list[dict]:
    data = client.get(f"ModuleVsMenu.svc/GetModuleVsMenuWithModule/?ModuleId={module_id}")
    return data if isinstance(data, list) else []


def get_menu_detail(menu_id: int) -> dict:
    return client.get(f"Menu.svc/?MenuId={menu_id}")


def _to_save_shape(row: dict) -> dict:
    return {
        "ModuleVsMenuId": str(row["ModuleVsMenuId"]),
        "ModuleId": str(row["ModuleId"]),
        "ModuleVsMenuSlNo": row["ModuleVsMenuSlNo"],
        "undefined": "",
        "MenuId": str(row["MenuId"]),
        "ParentIdd": str(row["ParentId"]),
        "ParentId": str(row["ParentId"]),
        "ModuleVsMenuParentIds": row.get("ModuleVsMenuParentIds", ""),
        "ModuleVsMenuLevels": str(row.get("ModuleVsMenuLevels", "")),
    }


def build_new_row(
    existing_rows: list[dict],
    module_id: int,
    menu_id: int,
    under_id: int,
    under_menu_parent_ids: str,
    under_menu_level: int,
) -> dict:
    if under_id is None or int(under_id) == -1:
        raise ModuleVsMenuValidationError(
            "An 'Under' (parent) menu is required."
        )

    next_sl_no = 1
    if existing_rows:
        next_sl_no = max(int(r["ModuleVsMenuSlNo"]) for r in existing_rows) + 1

    parent_ids = f"{under_menu_parent_ids or ''},{under_id}"
    levels = int(under_menu_level) + 1

    return {
        "ModuleVsMenuId": "0",
        "ModuleId": str(module_id),
        "ModuleVsMenuSlNo": next_sl_no,
        "undefined": "",
        "MenuId": str(menu_id),
        "ParentIdd": str(under_id),
        "ParentId": str(under_id),
        "ModuleVsMenuParentIds": parent_ids,
        "ModuleVsMenuLevels": levels,
    }


def create_module_vs_menu(module_id: int, menu_id: int, under_id: int) -> dict:
    existing_rows = get_existing_grid(module_id)

    if int(menu_id) == int(under_id):
        raise ModuleVsMenuValidationError("A menu cannot be placed under itself.")

    under_detail = get_menu_detail(under_id)
    if not isinstance(under_detail, dict) or "MenuLevel" not in under_detail:
        raise ModuleVsMenuValidationError(
            f"Could not fetch details for the 'Under' menu (MenuId={under_id})."
        )

    new_row = build_new_row(
        existing_rows=existing_rows,
        module_id=module_id,
        menu_id=menu_id,
        under_id=under_id,
        under_menu_parent_ids=under_detail.get("MenuParentIds", ""),
        under_menu_level=under_detail.get("MenuLevel", 0),
    )

    payload = [_to_save_shape(r) for r in existing_rows] + [new_row]
    result = client.post("ModuleVsMenu.svc/", payload)
    return {"result": result, "new_row": new_row, "sl_no": new_row["ModuleVsMenuSlNo"]}