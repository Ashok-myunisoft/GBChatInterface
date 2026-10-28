from __future__ import annotations

import logging

from services.client import client
from services.menuset import (
    get_menuset_detail,
    find_menu_in_catalog,
    MenuSetError,
    VISIBLE_YES,
)

logger = logging.getLogger("uvicorn.error")

NEW_ROW_SOURCE_TYPE_FALLBACK = 0
# ^ Only used when a MenuSet has zero existing detail rows to copy from.
#   NOT independently HAR-confirmed. 0 matches the "unspecified/default"
#   convention already used for ApplicableType_default in
#   config/menuset_enums.json. If a save still fails UNI0017 with an
#   empty MenuSet, capture one real "add first row to an empty MenuSet"
#   HAR entry and replace this with the confirmed value.


def _existing_source_type(array: list[dict]) -> int:
    """
    Picks a MenuSetDetailSourceType value that GoodBooks has *already
    accepted* on this MenuSet, instead of guessing a literal.

    Root cause of the previous save failure (GoodBooks
    ErrorNumber=UNI0017, "ALTER TABLE statement conflicted with the
    FOREIGN KEY constraint"): the old code sent a hardcoded
    MenuSetDetailSourceType=5 for every new row. That value was never
    confirmed against a real HAR capture (every other field in this
    file is either read live from GB4 or has a comment citing the
    capture that confirmed it -- this one had neither). GoodBooks
    validates MenuSetDetailSourceType as a foreign key into a lookup
    table, and 5 isn't a valid row there, so every save of a new/newly
    -visible menu failed with a FK conflict.

    Fix: every row already in MenuSetDetailArray came straight from a
    GET on this same MenuSet -- GoodBooks has already accepted those
    values, so they're guaranteed valid. Reuse the most common one
    among them for the new row. Only fall back to a literal if the
    MenuSet has no existing rows at all to copy from.
    """
    existing = [
        row.get("MenuSetDetailSourceType")
        for row in array
        if row.get("MenuSetDetailSourceType") is not None
    ]
    if not existing:
        return NEW_ROW_SOURCE_TYPE_FALLBACK
    return max(set(existing), key=existing.count)


def _fk_value_or_sentinel(value, sentinel: int = -1) -> int:
    """catalog_item fields like ObjectModelId/ReportFormatId/ReportViewId
    come back from Menu.svc/MenuObject as an explicit JSON null when not
    applicable to that menu -- NOT a missing key. dict.get(key, -1) only
    applies its default for a MISSING key, so a present-but-null value
    was silently passing through as None instead of -1, and GoodBooks
    rejected that None on the FK column. This normalises both cases."""
    return sentinel if value is None else value


def _new_detail_row(catalog_item: dict, next_slno: int, source_type: int) -> dict:
    return {
        "MenuSetDetailSlNo": next_slno,
        "MenuId": catalog_item.get("MenuId"),
        "MenuSetDetailDisplayName": catalog_item.get("MenuDisplayName") or catalog_item.get("MenuName"),
        "MenuSetDetailMenuKey": catalog_item.get("MenuKey") or "",
        "MenuSetDetailSortOrder": catalog_item.get("MenuSetsDetailSortOrder") or catalog_item.get("MenuSortOrder") or 0,
        "ObjectModelId": _fk_value_or_sentinel(catalog_item.get("ObjectModelId")),
        "ReportFormatId": _fk_value_or_sentinel(catalog_item.get("ReportFormatId")),
        "ReportViewId": _fk_value_or_sentinel(catalog_item.get("ReportViewId")),
        "MenuSetDetailSourceType": source_type,
    }


def preview_menu_visibility_change(menuset_id: int, module_id: int, menu_id: int, visible_code: int) -> dict:
    """
    Builds (but does not save) the summary of what a visibility change
    would do, for the confirm-before-save step.
    """
    detail = get_menuset_detail(menuset_id)
    array = detail.get("MenuSetDetailArray") or []
    currently_visible = any(str(row.get("MenuId")) == str(menu_id) for row in array)

    catalog_item = find_menu_in_catalog(module_id, menuset_id, menu_id)
    menu_name = catalog_item.get("MenuDisplayName") or catalog_item.get("MenuName")

    will_be_visible = visible_code == VISIBLE_YES

    return {
        "menuset_id": menuset_id,
        "menuset_name": detail.get("MenuSetName"),
        "module_name": detail.get("ModuleName"),
        "client_name": detail.get("ClientName"),
        "menu_id": menu_id,
        "menu_name": menu_name,
        "currently_visible": currently_visible,
        "will_be_visible": will_be_visible,
        "no_change": currently_visible == will_be_visible,
    }


def set_menu_visibility(menuset_id: int, module_id: int, menu_id: int, visible_code: int) -> dict:
    """
    Toggles ONE menu's visibility within an existing MenuSet and saves
    the whole record back, per GB4's confirmed save behaviour (there
    is no single-row update endpoint -- MenuSet.svc/ always resends
    the entire MenuSetDetailArray).

    visible_code: 1 = Yes, 2 = No, 3 = Optional. Confirmed from a real
    HAR diff that Yes means "row present in MenuSetDetailArray" and
    No/Optional both mean "row absent" -- there is no server-side
    distinction between No and Optional; both simply omit the row.
    """
    detail = get_menuset_detail(menuset_id)
    array = list(detail.get("MenuSetDetailArray") or [])

    array = [row for row in array if str(row.get("MenuId")) != str(menu_id)]

    if visible_code == VISIBLE_YES:
        catalog_item = find_menu_in_catalog(module_id, menuset_id, menu_id)
        next_slno = max((row.get("MenuSetDetailSlNo") or 0) for row in array) + 1 if array else 1
        source_type = _existing_source_type(array)
        new_row = _new_detail_row(catalog_item, next_slno, source_type)
        logger.info("[MENUSET SAVE] new detail row: %s", new_row)
        array.append(new_row)

    payload = {
        "MenuSetId": detail.get("MenuSetId"),
        "MenuSetName": detail.get("MenuSetName"),
        "ClientId": detail.get("ClientId"),
        "ClientCode": detail.get("ClientCode"),
        "ClientName": detail.get("ClientName"),
        "ModuleId": detail.get("ModuleId"),
        "ModuleCode": detail.get("ModuleCode"),
        "ModuleName": detail.get("ModuleName"),
        "MenuSetSourceType": detail.get("MenuSetSourceType"),
        "MenuSetDetailArray": array,
        "MenuSetStatus": detail.get("MenuSetStatus"),
        "MenuSetVersion": detail.get("MenuSetVersion"),
    }
    logger.info("[MENUSET SAVE] posting payload with %d detail rows for MenuSetId=%s",
                len(array), payload.get("MenuSetId"))

    return client.post("MenuSet.svc/", payload)