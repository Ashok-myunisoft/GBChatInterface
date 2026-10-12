from mcp.server.fastmcp import FastMCP

from services import picklists
from services.client import client
from services.code_generator import generate_next_code, get_all_prefixes
from services.save_menu import save_menu, MenuValidationError, NONE_CODE
from services.enums import MENU_TYPE, MENU_NATURE, MENU_STATUS, numbered_options
from services.module_vs_menu import (
    add_menu_to_module,
    find_menu_id_by_code,
    get_module_menu_mappings,
    ModuleVsMenuError,
)

from services import menuset
from services.save_menuset import set_menu_visibility, preview_menu_visibility_change, MenuSetError as _SaveMenuSetError
from services.menuset import MenuSetError

mcp = FastMCP("goodbooks-menu-agent")


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def list_modules() -> list:
    return _safe(picklists.get_modules)


@mcp.tool()
def list_parent_menus(module_name: str) -> list:
    module_id, err = _resolve_module_id(module_name)
    if err:
        return err
    return _safe(picklists.get_parent_menus, module_id)


@mcp.tool()
def list_sections() -> list:
    return _safe(picklists.get_sections)


@mcp.tool()
def list_web_services() -> list:
    return _safe(picklists.get_web_services)


@mcp.tool()
def list_object_models() -> list:
    return _safe(picklists.get_object_models)


@mcp.tool()
def list_biz_transaction_classes() -> list:
    return _safe(picklists.get_biz_transaction_classes)


@mcp.tool()
def list_entities() -> list:
    return _safe(picklists.get_entities)


@mcp.tool()
def list_gcm_types() -> list:
    return _safe(picklists.get_gcm_types)


@mcp.tool()
def list_pages() -> list:
    return _safe(picklists.get_pages)


@mcp.tool()
def list_report_formats() -> list:
    return _safe(picklists.get_report_formats)


@mcp.tool()
def list_webforms() -> list:
    return _safe(picklists.get_webforms)


@mcp.tool()
def list_menu_types() -> list:
    return numbered_options(MENU_TYPE)


@mcp.tool()
def list_menu_natures() -> list:
    return numbered_options(MENU_NATURE)


@mcp.tool()
def list_menu_statuses() -> list:
    return numbered_options(MENU_STATUS)


def _resolve_module_id(module_name: str):
    modules = picklists.get_modules()
    if not isinstance(modules, list):
        return None, {"error": f"Could not load module list: {modules}"}

    name_lower = module_name.strip().lower()
    exact = [m for m in modules if str(m.get("name", "")).strip().lower() == name_lower]
    matches = exact or [
        m for m in modules if name_lower in str(m.get("name", "")).strip().lower()
    ]

    if not matches:
        available = ", ".join(str(m.get("name")) for m in modules)
        return None, {
            "error": f"No module found matching '{module_name}'. "
                     f"Available modules: {available}"
        }
    if len(matches) > 1:
        available = ", ".join(str(m.get("name")) for m in matches)
        return None, {
            "error": f"Multiple modules match '{module_name}': {available}. "
                     f"Please ask the user which one they mean and call this again "
                     f"with the exact name."
        }
    return matches[0].get("id"), None


@mcp.tool()
def generate_menu_code(module_name: str, preferred_prefix: str | None = None) -> dict:
    module_id, err = _resolve_module_id(module_name)
    if err:
        return err
    try:
        return {"code": generate_next_code(module_id, module_name=module_name, preferred_prefix=preferred_prefix)}
    except ValueError as e:
        msg = str(e)
        if msg.startswith("NEEDS_PREFIX_CHOICE:"):
            import re as _re
            m = _re.search(r"\[(.+?)\]", msg)
            prefixes = [p.strip(" '\"") for p in m.group(1).split(",")] if m else []
            return {
                "needs_prefix_choice": True,
                "available_prefixes": prefixes,
                "message": f"Module '{module_name}' has multiple code prefixes: "
                           f"{', '.join(prefixes)}. Which one should this menu use?",
            }
        return {"error": msg}


@mcp.tool()
def audit_module_code_prefixes() -> dict:
    modules = picklists.get_modules()
    if not isinstance(modules, list):
        return {"error": f"Could not load module list: {modules}"}

    from services.code_generator import MODULE_PREFIX_OVERRIDES, MODULE_ID_PREFIX_OVERRIDES

    needs_decision = []
    resolved = []
    no_codes = []

    for m in modules:
        name = m.get("name")
        mod_id = m.get("id")
        if mod_id is None or name is None:
            continue
        prefixes = _safe(get_all_prefixes, mod_id, name)
        if isinstance(prefixes, dict) and "error" in prefixes:
            no_codes.append({"module": name, "error": prefixes["error"]})
            continue
        if not prefixes:
            no_codes.append({"module": name, "prefixes": []})
        elif len(prefixes) > 1:
            override = (
                MODULE_ID_PREFIX_OVERRIDES.get(mod_id)
                or MODULE_PREFIX_OVERRIDES.get(name.strip().lower())
            )
            if override and override in prefixes:
                resolved.append({"module": name, "prefixes": prefixes, "using": override})
            else:
                needs_decision.append({"module": name, "prefixes": prefixes})

    return {
        "needs_human_decision": needs_decision,
        "already_resolved": resolved,
        "modules_with_no_existing_codes": no_codes,
    }


@mcp.tool()
def get_current_user() -> dict:
    return {
        "user_id": client.user_id,
        "user_code": client.user_code,
        "user_name": client.user_name,
        "source_type": client.source_type,
    }


@mcp.tool()
def create_menu(fields: dict) -> dict:
    module_name = fields.get("module_name")
    real_module_id = fields.get("module_id")
    if module_name:
        real_module_id, err = _resolve_module_id(module_name)
        if err:
            return err
        fields = {**fields, "module_id": real_module_id}

    if fields.get("menu_name") and real_module_id is not None:
        existing = picklists.get_parent_menus(real_module_id)
        if isinstance(existing, list):
            target = str(fields["menu_name"]).strip().lower()
            dupes = [
                e for e in existing
                if str(e.get("name", "")).strip().lower() == target
            ]
            if dupes:
                taken = ", ".join(f"{e.get('code')} (id {e.get('id')})" for e in dupes)
                return {
                    "error": f"menu_name {fields['menu_name']!r} already exists in this "
                             f"module as: {taken}. GoodBooks rejects saves with a duplicate "
                             f"name (ErrorNumber=UNI0006) even when menu_code is unique. "
                             f"Choose a different menu_name or confirm you meant to reuse "
                             f"the existing menu."
                }

    if "section_id" in fields and fields["section_id"] != NONE_CODE:
        sections = picklists.get_sections()
        if not isinstance(sections, list):
            return {"error": f"Could not load section list: {sections}"}
        valid = {s.get("id"): s.get("name") for s in sections}
        if fields["section_id"] not in valid:
            options = ", ".join(f"{i} ({n})" for i, n in valid.items())
            return {
                "error": f"section_id {fields['section_id']!r} is not a real section id "
                         f"from list_sections. Valid section ids: {options}"
            }

    if "under_id" in fields and fields["under_id"] != NONE_CODE:
        parents = picklists.get_parent_menus(real_module_id)
        if not isinstance(parents, list):
            return {"error": f"Could not load parent menu list: {parents}"}
        valid = {p.get("id"): p.get("name") for p in parents}
        if fields["under_id"] not in valid:
            options = ", ".join(f"{i} ({n})" for i, n in valid.items())
            return {
                "error": f"under_id {fields['under_id']!r} is not a real parent menu id "
                         f"from list_parent_menus. Valid parent menu ids: {options}"
            }

    try:
        return save_menu(fields, created_by_name=client.user_name, source_type=client.source_type or 0)
    except MenuValidationError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def find_new_menu_id(module_name: str, menu_code: str) -> dict:
    """Looks up a just-created menu's real MenuId by the Code that was
    generated for it, since Menu.svc/'s save response never returns
    the new id directly."""
    module_id, err = _resolve_module_id(module_name)
    if err:
        return err
    menu_id = _safe(find_menu_id_by_code, module_id, menu_code)
    if isinstance(menu_id, dict) and "error" in menu_id:
        return menu_id
    if menu_id is None:
        return {"error": f"No menu found with code '{menu_code}' under module '{module_name}'."}
    return {"menu_id": menu_id}


@mcp.tool()
def list_module_menu_mappings(module_name: str) -> list:
    module_id, err = _resolve_module_id(module_name)
    if err:
        return err
    return _safe(get_module_menu_mappings, module_id)


@mcp.tool()
def add_menu_to_module_tree(module_name: str, menu_id: int, under_id: int | None = None) -> dict:
    module_id, err = _resolve_module_id(module_name)
    if err:
        return err
    try:
        return add_menu_to_module(module_id, menu_id, under_id)
    except ModuleVsMenuError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
@mcp.tool()
def list_applicable_types() -> list:
    return menuset.list_applicable_types()


@mcp.tool()
def list_visible_options() -> list:
    return menuset.list_visible_options()


@mcp.tool()
def list_menuset_clients() -> list:
    return _safe(menuset.get_clients)


@mcp.tool()
def list_menuset_names(client_id: int | None = None, module_name: str | None = None) -> list:
    """Name dropdown for MenuSet. Each entry is {"number", "id" (=MenuSetId),
    "name", "code", "module_name", "module_id"} -- module_name/module_id
    are shown alongside every option so the user (or the caller) can see
    which module a Name actually belongs to before picking it.

    If module_name is given, this pre-filters to only the names whose
    own module matches -- this is what stops a user from ever being
    offered a Name that belongs to some other module in the first
    place. The filter only ever narrows the list, never fabricates
    module info: if GB4's response for this endpoint turns out not to
    carry module data at all (module_name/module_id come back None on
    every row), filtering is skipped entirely and the full unfiltered
    list is returned instead -- silently hiding every option because
    of an unconfirmed field would be worse than showing an unfiltered
    list.
    """
    names = _safe(menuset.get_menuset_names, client_id)
    if not isinstance(names, list) or not module_name:
        return names

    has_module_data = any(n.get("module_name") for n in names)
    if not has_module_data:
        return names

    wanted = module_name.strip().lower()
    return [
        n for n in names
        if str(n.get("module_name") or "").strip().lower() == wanted
    ]


@mcp.tool()
def get_menu_tree(module_name: str) -> dict:
    module_id, err = _resolve_module_id(module_name)
    if err:
        return err
    return _safe(picklists.get_module_menu_tree, module_id)


@mcp.tool()
def get_menuset_context(menuset_id: int) -> dict:
    """Call this right after the user picks a Name. Returns Client and
    Module (both auto-fill), and current Applicable Type on record."""
    try:
        detail = menuset.get_menuset_detail(menuset_id)
    except MenuSetError as e:
        return {"error": str(e)}
    return {
        "menuset_id": detail.get("MenuSetId"),
        "menuset_name": detail.get("MenuSetName"),
        "applicable_type_code": detail.get("MenuSetApplicableType"),
        "client_id": detail.get("ClientId"),
        "client_name": detail.get("ClientName"),
        "module_id": detail.get("ModuleId"),
        "module_name": detail.get("ModuleName"),
        "module_code": detail.get("ModuleCode"),
    }


@mcp.tool()
def locate_menu_in_menuset(menuset_id: int, module_id: int, menu_id: int, menu_module_name: str | None = None) -> dict:
    """Finds the specific menu (by MenuId) inside this MenuSet's
    module catalog, and reports whether it's currently visible.

    menu_module_name: the module the menu was created/added under in
    STEP 26 (e.g. "Frame Work"). If given, this is cross-checked
    against the MenuSet's own module before doing the lookup, since a
    MenuSet is its own record and can belong to a different module
    than the one you were just working in -- get_menuset_context's
    module_id/module_name come straight off the MenuSet record, not
    from your prior steps. Catches the mismatch immediately instead
    of surfacing as a confusing "not found in catalog" error after
    retries.
    """
    try:
        detail = menuset.get_menuset_detail(menuset_id)
    except MenuSetError as e:
        return {"error": str(e)}

    actual_module_name = detail.get("ModuleName")
    if menu_module_name and str(actual_module_name or "").strip().lower() != menu_module_name.strip().lower():
        return {
            "error": f"Module mismatch: the menu was created under module "
                     f"'{menu_module_name}', but MenuSet {menuset_id} belongs to "
                     f"module '{actual_module_name}' (ModuleId={detail.get('ModuleId')}). "
                     f"Pick a MenuSet that belongs to '{menu_module_name}' instead -- "
                     f"call list_menuset_names again and choose a different Name."
        }

    try:
        return preview_menu_visibility_change(menuset_id, module_id, menu_id, visible_code=0)
    except (MenuSetError, _SaveMenuSetError) as e:
        return {"error": str(e)}


@mcp.tool()
def preview_menuset_visibility_change(menuset_id: int, module_id: int, menu_id: int, visible_code: int) -> dict:
    """Call before saving, to build the summary shown to the user for
    confirmation. visible_code: 1=Yes, 2=No, 3=Optional."""
    try:
        return preview_menu_visibility_change(menuset_id, module_id, menu_id, visible_code)
    except (MenuSetError, _SaveMenuSetError) as e:
        return {"error": str(e)}


@mcp.tool()
def save_menuset_visibility(menuset_id: int, module_id: int, menu_id: int, visible_code: int) -> dict:
    """Saves the visibility change. visible_code: 1=Yes, 2=No,
    3=Optional. Only call this after the user has confirmed the
    summary from preview_menuset_visibility_change with yes."""
    try:
        return set_menu_visibility(menuset_id, module_id, menu_id, visible_code)
    except (MenuSetError, _SaveMenuSetError) as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}



if __name__ == "__main__":
    mcp.run()