from __future__ import annotations

import time

from services.client import client, GoodBooksSessionError
from services.enums import require_verified

NONE_CODE = -1
NONE_NAME = "NONE"
IMAGE_PLACEHOLDER_NAME = "Image place holder"


class MenuValidationError(Exception):
    pass


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _to_int(value, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise MenuValidationError(f"'{field_name}' must be a whole number, got {value!r}.")


def _validate_under_id(under_id: int, under_name: str) -> None:
    if under_id == NONE_CODE:
        return
    try:
        result = client.get(f"Menu.svc/?MenuId={under_id}")
    except GoodBooksSessionError:
        raise MenuValidationError(
            f"Could not verify the Under (Parent) selection '{under_name}' "
            f"(MenuId {under_id}) against GoodBooks. It does not resolve to "
            "an existing menu, so it cannot be used as a parent. Please "
            "pick a different Under value."
        )
    if not result:
        raise MenuValidationError(
            f"The Under (Parent) selection '{under_name}' (MenuId {under_id}) "
            "does not correspond to an existing menu in GoodBooks. Please "
            "pick a different Under value."
        )


def build_menu_payload(fields: dict, created_by_name: str, source_type: int) -> dict:
    menu_name = fields["menu_name"]
    under_name = fields.get("under_name", NONE_NAME)
    under_id = _to_int(fields.get("under_id", NONE_CODE), "under_id")

    if under_name and under_name != NONE_NAME and menu_name == under_name:
        raise MenuValidationError("Menu Name and Parent (Under) Name must not be the same.")

    _validate_under_id(under_id, under_name)

    now = _epoch_ms()
    caption = fields.get("caption", "")

    return {
        "MenuId": 0,
        "MenuCode": fields["menu_code"],
        "MenuName": menu_name,
        "ModuleId": _to_int(fields["module_id"], "module_id"),
        "ModuleName": fields["module_name"],
        "ModuleCode": fields["module_code"],
        "GcmTypeId": _to_int(fields.get("gcm_type_id", NONE_CODE), "gcm_type_id"),
        "GcmTypeCode": fields.get("gcm_type_code", NONE_NAME),
        "MenuSection": "NONE",
        "MenuIsParentMenu": 0,
        "ParentId": under_id,
        "ParentCode": fields.get("under_code", NONE_NAME),
        "ParentName": under_name,
        "MenuParentIds": fields.get("parent_ids", ""),
        "MenuLevel": 3,
        "ReportId": _to_int(fields.get("report_id", NONE_CODE), "report_id"),
        "ReportCode": fields.get("report_code", NONE_NAME),
        "ReportName": fields.get("report_name", NONE_NAME),
        "WebFormId": _to_int(fields.get("webform_id", NONE_CODE), "webform_id"),
        "WebFormName": fields.get("webform_name", NONE_NAME),
        "MenuMenuType": fields["type_code"],
        "MenuMenuNature": fields["nature_code"],
        "ImageId": _to_int(fields.get("image_id", NONE_CODE), "image_id"),
        "ImageName": fields.get("image_name", IMAGE_PLACEHOLDER_NAME),
        "PageId": _to_int(fields.get("page_id", NONE_CODE), "page_id"),
        "PageCode": fields.get("page_code", NONE_NAME),
        "PageName": fields.get("page_name", NONE_NAME),
        "MenuDisplayName": menu_name,
        "MenuDescription": caption,
        "MenuToolTip": caption,
        "MenuCaption": caption,
        "MenuSortOrder": _to_int(fields.get("sort_order", 1), "sort_order"),
        "MenuVersion": 1,
        "MenuStatus": fields.get("status_code", "1"),
        "SaveWebServiceId": _to_int(fields.get("save_ws_id", NONE_CODE), "save_ws_id"),
        "SaveWebServiceName": fields.get("save_ws_name", NONE_NAME),
        "GetWebServiceId": _to_int(fields.get("get_ws_id", NONE_CODE), "get_ws_id"),
        "GetWebServiceName": fields.get("get_ws_name", NONE_NAME),
        "DeleteWebServiceId": _to_int(fields.get("delete_ws_id", NONE_CODE), "delete_ws_id"),
        "DeleteWebServiceName": fields.get("delete_ws_name", NONE_NAME),
        "EntityId": _to_int(fields.get("entity_id", NONE_CODE), "entity_id"),
        "EntityName": fields.get("entity_name", NONE_NAME),
        "EntityCode": fields.get("entity_code", NONE_NAME),
        "MenuBizTransactionClassId": _to_int(fields.get("biz_class_id", NONE_CODE), "biz_class_id"),
        "MenuBizTransactionClassCode": fields.get("biz_class_code", NONE_NAME),
        "MenuBizTransactionClassName": fields.get("biz_class_name", NONE_NAME),
        "ReportFormatId": _to_int(fields.get("report_format_id", NONE_CODE), "report_format_id"),
        "ReportFormatName": fields.get("report_format_name", NONE_NAME),
        "ReportViewId": _to_int(fields.get("report_view_id", NONE_CODE), "report_view_id"),
        "ReportViewName": fields.get("report_view_name", NONE_NAME),
        "URIParameterValue": fields.get("uri_parameter_value", ""),
        "ListMenuId": _to_int(fields.get("list_menu_id", NONE_CODE), "list_menu_id"),
        "GcmSectionId": _to_int(fields.get("section_id", NONE_CODE), "section_id"),
        "MenuCreatedOn": f"/Date({now})/",
        "MenuModifiedOn": f"/Date({now})/",
        "MenuCreatedByName": created_by_name,
        "MenuModifiedByName": created_by_name,
        "MenuSourceType": source_type,
        "HelpUrl": fields.get("help_url", ""),
        "FromVersion": fields.get("from_version", ""),
        "ToVersion": fields.get("to_version", ""),
        "ObjectModelId": _to_int(fields.get("object_model_id", NONE_CODE), "object_model_id"),
        "IconName": fields.get("icon_name", ""),
    }


def save_menu(fields: dict, created_by_name: str, source_type: int) -> dict:
    require_verified()
    payload = build_menu_payload(fields, created_by_name, source_type)
    return client.post("Menu.svc/", payload)