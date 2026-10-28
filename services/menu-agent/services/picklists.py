from __future__ import annotations

import sys
import time

from services.client import client


def _numbered(items, id_key="Id", name_key="Name", code_key="Code"):
    # Guard: if the API returned a raw string (e.g. an HTML error page or a
    # plain-text error message) instead of a list, treat it as an empty result
    # rather than iterating its characters one-by-one and producing thousands
    # of single-character "items" like 16600.<  16601./  16608.h … (Image 1 bug).
    if not isinstance(items, list):
        return []
    if not items:
        return []
    out = []
    for i, item in enumerate(items, start=1):
        if isinstance(item, dict):
            out.append({
                "number": i,
                "id": item.get(id_key),
                "name": item.get(name_key),
                "code": item.get(code_key),
            })
        else:
            # GoodBooks sometimes returns a plain list of strings for
            # this endpoint instead of {"Id":.., "Name":..} objects.
            # Treat the string itself as both the id and the name so
            # picklists still render instead of crashing.
            out.append({
                "number": i,
                "id": item,
                "name": item,
                "code": None,
            })
    return out


def _empty_criteria():
    return {"SectionCriteriaList": [{"SectionId": 0, "AttributesCriteriaList": [], "OperationType": 0}]}


def _name_criteria():
    """
    Criteria required by Module.svc/SelectList (confirmed via HAR capture):
    the endpoint rejects an empty AttributesCriteriaList with UNI0002 and
    requires a Name filter (FieldValue="" = no restriction, returns all rows).
    Used ONLY for get_modules() -- all other endpoints that accept an empty
    list continue to use _empty_criteria() unchanged.
    """
    return {
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


def _module_criteria(module_id: int):
    return {
        "SectionCriteriaList": [{
            "SectionId": 0,
            "AttributesCriteriaList": [{
                "FieldName": "ModuleId",
                "OperationType": 1,
                "FieldValue": module_id,
                "InArray": None,
                "JoinType": 0,
            }],
            "OperationType": 0,
        }]
    }


def get_modules():
    data = client.post("Module.svc/SelectList", _name_criteria())
    return _numbered(data)


def get_module_menu_tree(module_id: int):
    """Full existing menu tree for one module -- used for both the
    'Under' (parent) picklist and, critically, for deriving that
    module's real code prefix + highest sequence number in
    code_generator.py. Confirmed endpoint + payload shape from HAR."""
    return client.post("Menu.svc/MenuTree/?Type=0", _module_criteria(module_id))


def get_menus_by_module(module_id: int):
    """
    Every Menu.svc row filtered by ModuleId. Used by code_generator.py
    as a third, independent source of existing codes (see its own
    docstring for why). Uses the same _module_criteria() shape already
    confirmed for Menu.svc/MenuTree's ModuleId filter (get_module_menu_tree
    above) -- unconfirmed here specifically, since individual Menu rows
    are not guaranteed to carry a real ModuleId (see
    module_vs_menu.py::find_menu_id_by_code, which notes it's -1 on
    every row it has seen). If ModuleId turns out to be unreliable on
    this endpoint too, this will just come back empty -- callers
    already treat that as "no extra codes found here", not an error.
    """
    data = client.post("Menu.svc/SelectList", _module_criteria(module_id))
    return _numbered(data, id_key="MenuId", name_key="MenuName", code_key="MenuCode")


def _flatten_menu_tree(node, out: list[dict]):
    """
    MenuTree responses are nested (children arrays) -- reading only
    the top level (as _numbered() would) returns container/root nodes
    that have no meaningful name of their own, since the actual menus
    live inside 'children'. Walk the whole tree, same approach already
    used for code extraction in code_generator.py's _flatten_codes.

    IMPORTANT field names, confirmed from a real captured MenuTree
    response: each node uses "DisplayName" (or "data", which holds the
    same value) for the readable menu name, and "Code" for its code --
    there is NO "MenuName"/"MenuCode" key on these nodes at all (those
    only exist on the flat Menu.svc/SelectList shape used elsewhere).
    Using the wrong key here silently returns nothing for every node,
    which was the previous bug.
    """
    if isinstance(node, dict):
        name = node.get("DisplayName") or node.get("data")
        code = node.get("Code")
        mid = node.get("Id")
        if name:
            out.append({"id": mid, "name": name, "code": code})
        for child in node.get("children", []) or []:
            _flatten_menu_tree(child, out)
    elif isinstance(node, list):
        for item in node:
            _flatten_menu_tree(item, out)


def get_parent_menus(module_id: int):
    tree = get_module_menu_tree(module_id)
    if isinstance(tree, dict) and "error" in tree:
        return tree

    flat: list[dict] = []
    _flatten_menu_tree(tree, flat)
    return [
        {"number": i, "id": m["id"], "name": m["name"], "code": m.get("code")}
        for i, m in enumerate(flat, start=1)
    ]


def _code_criteria(menu_code: str):
    return {
        "SectionCriteriaList": [{
            "SectionId": 0,
            "AttributesCriteriaList": [{
                "FieldName": "MenuCode",
                "OperationType": 2,
                "FieldValue": menu_code,
                "InArray": None,
                "JoinType": 2,
            }],
            "OperationType": 0,
        }]
    }


def _numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _row_matches_code(item: dict, menu_code: str) -> bool:
    """
    True if ANY value in this row equals menu_code, regardless of what
    the key holding it is called. We know the exact code string we're
    looking for, so matching by VALUE sidesteps guessing the field
    name entirely -- unlike an earlier version of this function, which
    guessed "MenuCode"/"Code" and was wrong both times (see git
    history / prior chat messages).
    """
    target = menu_code.strip().upper()
    return any(
        isinstance(v, str) and v.strip().upper() == target
        for v in item.values()
    )


def _row_id_candidates(item: dict) -> list[int]:
    """
    Every numeric-looking value in this row that could plausibly be
    its own MenuId, ordered by how likely the key name is to mean
    exactly that: keys mentioning both "id" and "menu" first, then any
    other "id"-ish key, then any other whole number as a last resort.
    Nothing here is trusted as THE answer -- the caller
    (module_vs_menu.py::find_menu_id_by_code) must cross-check each
    candidate against the CONFIRMED Menu.svc/?MenuId=<id> shape
    (already used successfully elsewhere in this project for "Under"
    menu resolution) before treating any of them as real.
    """
    menu_id_keys, other_id_keys, other_numeric = [], [], []
    for k, v in item.items():
        n = _numeric(v)
        if n is None:
            continue
        kl = k.lower()
        if "id" in kl and "menu" in kl:
            menu_id_keys.append(n)
        elif "id" in kl:
            other_id_keys.append(n)
        else:
            other_numeric.append(n)

    seen = set()
    ordered = []
    for n in menu_id_keys + other_id_keys + other_numeric:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def get_menu_by_code(menu_code: str) -> list[dict]:
    """
    Finds the Menu row(s) whose code matches menu_code, WITHOUT relying
    on any single guessed field name for "the code" or "the id" --
    guessing that got this wrong twice already (first "MenuId"/
    "MenuName"/"MenuCode", then also trying "Id"/"DisplayName"/
    "Code" -- neither ever matched a menu that genuinely existed in
    GB4 right after being saved). Returns
    [{"code": menu_code, "id_candidates": [...]}, ...] -- the caller
    must resolve id_candidates against a confirmed endpoint.

    Retries with short backoff: a menu that's genuinely already in the
    DB (confirmed by checking it directly) has still repeatedly come
    back invisible to Menu.svc/SelectList on the FIRST lookup right
    after saving -- consistent with GB4 reading this list from a
    cache/index that lags the actual table by a second or two, not
    with a field-name problem alone. Retrying costs a little time but
    costs nothing if the first attempt was already fine.
    """
    def _scan(payload, label, delay):
        data = client.post("Menu.svc/SelectList", payload)
        rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        matched = [r for r in rows if _row_matches_code(r, menu_code)]
        print(
            f"[MENU LOOKUP] {label} scan for MenuCode={menu_code!r} "
            f"(after {delay}s wait) -> {len(rows)} total row(s), "
            f"{len(matched)} matching; sample_match={(matched[0] if matched else None)!r}",
            file=sys.stderr, flush=True,
        )
        return matched

    for delay in (0, 1, 2, 4):
        if delay:
            time.sleep(delay)

        matched = _scan(_code_criteria(menu_code), "filtered", delay)
        if not matched:
            matched = _scan(_empty_criteria(), "unfiltered", delay)

        if matched:
            return [
                {"code": menu_code, "id_candidates": _row_id_candidates(r)}
                for r in matched
            ]

    return []


def _section_criteria():
    """
    Confirmed via HAR capture of GB4's own Section dropdown (2026-07):
    GCM.svc/SelectList must be filtered by GcmType.Id == -1399999866,
    GB4's fixed internal id for the "Menu Section" GCM category. Without
    this filter the endpoint returns every category in the shared GCM
    master table (states, tax codes, etc.), not just sections.
    """
    return {
        "SectionCriteriaList": [{
            "SectionId": 0,
            "AttributesCriteriaList": [{
                "FieldName": "GcmType.Id",
                "OperationType": 1,
                "FieldValue": -1399999866,
                "InArray": None,
                "JoinType": 2,
            }],
            "OperationType": 0,
        }]
    }


def get_sections():
    data = client.post("GCM.svc/SelectList/?FirstNumber=1&MaxResult=50", _section_criteria())
    return _numbered(data)


def get_web_services():
    data = client.post("WebService.svc/SelectList", _empty_criteria())
    return _numbered(data)


def get_object_models():
    """Object Model field -> File.svc/SelectList (confirmed by GB4 dev,
    corrects an earlier assumption that this was Entity.svc)."""
    data = client.post("File.svc/SelectList/?FirstNumber=1&MaxResult=50", _empty_criteria())
    return _numbered(data)


def get_biz_transaction_classes():
    data = client.post("BIZTransactionClass.svc/SelectList/?FirstNumber=-1&MaxResult=-1", _empty_criteria())
    return _numbered(data)


def get_entities():
    data = client.post("Entity.svc/SelectList/?FirstNumber=1&MaxResult=50", _empty_criteria())
    return _numbered(data)


def get_gcm_types():
    data = client.post("GcmType.svc/SelectList/?FirstNumber=1&MaxResult=50", _empty_criteria())
    return _numbered(data)


def get_images():
    data = client.post("Image.svc/SelectList", _empty_criteria())
    return _numbered(data)


def get_report_formats():
    data = client.post("Report.svc/SelectList", _empty_criteria())
    return _numbered(data)


def get_webforms():
    data = client.post("WebForm.svc/SelectList", _empty_criteria())
    return _numbered(data)


def get_pages():
    data = client.post("Page.svc/SelectList", _empty_criteria())
    return _numbered(data)