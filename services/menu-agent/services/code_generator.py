from __future__ import annotations

import re
import sys

from services.picklists import get_module_menu_tree, get_menus_by_module, get_menu_by_code
from services.module_vs_menu import get_module_menu_mappings

_CODE_KEY_NAMES = {"menucode", "code"}
_CODE_PATTERN = re.compile(r"^[A-Za-z]+\d+$")

# Modules where multiple code prefixes exist historically, but a human
# (team lead) has confirmed which single prefix new menus should
# continue using. Keyed by module NAME, lowercased. Add more entries
# here as each ambiguous module gets a decision -- see the
# audit_module_code_prefixes tool (mcp_server.py) for finding which
# modules still need one.
#
# Confirmed 2026-07-25: Framework -> FW (multiple legacy prefixes
# exist: AD, FW, PL, REP -- new menus continue FW only).
MODULE_PREFIX_OVERRIDES = {
    "framework": "FW",
}

# Same idea, but keyed by the module's actual GB4 numeric id instead
# of its name. Checked as a fallback if name-matching ever fails
# (wrong case, whitespace, a rename in GB4, an id passed straight
# through, etc.) -- belt and suspenders, since the name-keyed lookup
# is the more fragile of the two paths.
# Confirmed 2026-07-25: Framework module id -2147483637 -> FW.
MODULE_ID_PREFIX_OVERRIDES = {
    -2147483637: "FW",
}


def _flatten_codes(node, out: list[str]):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key.lower() in _CODE_KEY_NAMES and _CODE_PATTERN.match(value):
                out.append(value)
        for value in node.values():
            if isinstance(value, (dict, list)):
                _flatten_codes(value, out)
    elif isinstance(node, list):
        for item in node:
            _flatten_codes(item, out)


def _get_parsed_codes(module_id: int, module_name: str | None = None) -> list[tuple[str, str]]:
    """Fetch + parse a module's existing menu codes into (prefix, number) pairs.

    Reads from THREE independent sources and merges them, rather than
    trusting MenuTree alone:
      1. Menu.svc/MenuTree/?Type=0 (via get_module_menu_tree) -- the
         original confirmed source.
      2. ModuleVsMenu.svc/GetModuleVsMenuWithModule (via
         get_module_menu_mappings) -- confirmed (see module_vs_menu.py)
         to return a flat list of every menu wired into this module's
         tree, including MenuCode, independent of MenuTree's own
         filtering.
      3. Menu.svc/SelectList filtered by ModuleId (via
         get_menus_by_module) -- returns every Menu row under this
         module straight from the Menu table, independent of both
         MenuTree's Type filtering AND ModuleVsMenu tree-wiring.
    MenuTree's hardcoded '?Type=0' is UNCONFIRMED to cover every
    MenuType, and ModuleVsMenu only sees menus already wired into the
    tree -- either gap alone could let a real, already-used code go
    unseen, so generate_next_code() would hand out that same code
    again (this was the confirmed cause of the FW30003 collision: a
    menu with that code existed in Menu.svc but wasn't visible to
    either of the first two sources). Merging all three closes that
    blind spot.
    """
    tree = get_module_menu_tree(module_id)
    print(
        f"[CODE GEN] module_id={module_id!r} module_name={module_name!r} raw_tree={tree!r}",
        file=sys.stderr, flush=True,
    )

    codes: list[str] = []
    _flatten_codes(tree, codes)

    try:
        mappings = get_module_menu_mappings(module_id)
    except Exception as e:
        print(
            f"[CODE GEN] get_module_menu_mappings failed, continuing without "
            f"its codes: {e}",
            file=sys.stderr, flush=True,
        )
        mappings = []

    mapping_codes = [
        row.get("MenuCode")
        for row in mappings
        if isinstance(row, dict)
        and isinstance(row.get("MenuCode"), str)
        and _CODE_PATTERN.match(row["MenuCode"])
    ]

    try:
        module_menus = get_menus_by_module(module_id)
    except Exception as e:
        print(
            f"[CODE GEN] get_menus_by_module failed, continuing without "
            f"its codes: {e}",
            file=sys.stderr, flush=True,
        )
        module_menus = []

    selectlist_codes = [
        m.get("code")
        for m in module_menus
        if isinstance(m, dict)
        and isinstance(m.get("code"), str)
        and _CODE_PATTERN.match(m["code"])
    ]

    codes = list(dict.fromkeys(codes + mapping_codes + selectlist_codes))  # merge, de-duplicated, order preserved
    print(
        f"[CODE GEN] module_id={module_id!r} module_name={module_name!r} "
        f"merged_codes={codes!r}",
        file=sys.stderr, flush=True,
    )

    parsed = []
    for code in codes:
        m = re.match(r"^([A-Za-z]+)(\d+)$", code)
        if m:
            parsed.append((m.group(1), m.group(2)))
    return parsed


def generate_next_code(
    module_id: int,
    module_name: str | None = None,
    preferred_prefix: str | None = None,
) -> str:
    """
    preferred_prefix: an explicit, one-off prefix choice for THIS call
    only (not saved anywhere). Used when a module has multiple
    prefixes and isn't in MODULE_PREFIX_OVERRIDES / MODULE_ID_PREFIX_OVERRIDES
    -- the caller asks the user which prefix to use, then re-calls with
    this set to skip the permanent-override requirement.
    """
    parsed = _get_parsed_codes(module_id, module_name)

    if not parsed:
        raise ValueError(
            "Could not determine an existing code prefix for this module -- "
            "either it genuinely has no existing menus, or the module_id "
            "passed in wasn't the module's real GB4 id (double-check it's "
            "the 'id' field from list_modules, not the display 'number'). "
            "Check the [CODE GEN] debug lines in the server log for the "
            "raw response. If the module truly has no menus yet, this "
            "needs a human to assign the first code manually."
        )

    prefixes = {p for p, _ in parsed}

    if len(prefixes) > 1:
        chosen = (
            preferred_prefix
            or MODULE_ID_PREFIX_OVERRIDES.get(module_id)
            or MODULE_PREFIX_OVERRIDES.get((module_name or "").strip().lower())
        )

        if chosen and chosen in prefixes:
            # Confirmed choice (permanent override or one-off from the
            # user this call): only continue that prefix's sequence,
            # ignore the other legacy prefixes entirely.
            parsed = [(p, n) for p, n in parsed if p == chosen]
            prefixes = {chosen}
        elif chosen:
            # A prefix was specified/configured but it isn't even among
            # this module's actual codes -- don't guess, that's a typo
            # or stale config, not a real answer.
            raise ValueError(
                f"Prefix '{chosen}' was specified for this module, but it "
                f"doesn't appear in its existing codes ({sorted(prefixes)}). "
                f"Check MODULE_PREFIX_OVERRIDES / MODULE_ID_PREFIX_OVERRIDES "
                f"in code_generator.py, or the prefix the user chose."
            )
        else:
            # No permanent override and no per-call choice: this is the
            # "ask the user" case. NEEDS_PREFIX_CHOICE is a stable marker
            # the caller (mcp_server.py) can detect and turn into a
            # question, rather than parsing prose.
            raise ValueError(
                f"NEEDS_PREFIX_CHOICE: This module's existing menus use more "
                f"than one code prefix ({sorted(prefixes)}). Ask the user "
                f"which one to continue, then call generate_menu_code again "
                f"with that prefix as preferred_prefix."
            )

    prefix = prefixes.pop()
    # Tie BOTH the next number and its zero-padding width to the SAME entry
    # -- the one with the actual highest number. Previously the width came
    # from parsed[0] (whichever code happened to appear first in the tree
    # traversal) while the number came from max() over the whole list --
    # two different entries. If this module's history has mixed-width
    # codes (e.g. old 4-digit codes alongside newer 5-digit ones), that
    # mismatch silently produced a code with the wrong digit count and
    # padding, unrelated to the true last code. Always derive both from
    # the single max entry so the result is deterministic and always
    # "last real code + 1".
    max_prefix, max_num_str = max(parsed, key=lambda pn: int(pn[1]))
    next_num = int(max_num_str) + 1
    width = len(max_num_str)
    candidate = f"{prefix}{str(next_num).zfill(width)}"

    # MenuCode is unique across the WHOLE Menu table in GB4, not just
    # within this module -- but everything above (MenuTree, ModuleVsMenu,
    # SelectList-by-ModuleId) only ever looks at codes visible to THIS
    # module. A menu can hold the "next" code already while being
    # invisible to all three of those (e.g. not yet wired into this
    # module's tree), and GB4 will still reject the save as a duplicate
    # (ErrorNumber=UNI0006) -- confirmed cause of the FW30011 collision.
    # get_menu_by_code() does a genuine system-wide, unfiltered scan
    # (already relied on elsewhere for the same reason -- see
    # module_vs_menu.find_menu_id_by_code), so re-use it here to confirm
    # the candidate is actually free before handing it out, incrementing
    # past any real collisions found.
    while get_menu_by_code(candidate):
        next_num += 1
        candidate = f"{prefix}{str(next_num).zfill(width)}"

    print(
        f"[CODE GEN] chosen max entry: prefix={max_prefix!r} num={max_num_str!r} "
        f"(width={width}) -> next={candidate}",
        file=sys.stderr, flush=True,
    )
    return candidate


def get_all_prefixes(module_id: int, module_name: str | None = None) -> list[str]:
    parsed = _get_parsed_codes(module_id, module_name)
    return sorted({p for p, _ in parsed})