import json

from menu_agent_config.settings import CONFIG_DIR

_ENUMS_PATH = CONFIG_DIR / "menu_enums.json"

with open(_ENUMS_PATH) as f:
    _ENUMS = json.load(f)

MENU_TYPE = _ENUMS["MenuType"]
MENU_NATURE = _ENUMS["MenuNature"]
MENU_STATUS = _ENUMS["MenuStatus"]
MENU_STATUS_DEFAULT = _ENUMS["MenuStatus_default"]
_VERIFIED = _ENUMS.get("_verified", False)


class EnumsNotVerifiedError(Exception):
    pass


def numbered_options(enum_dict: dict):
    items = sorted(enum_dict.items(), key=lambda kv: int(kv[0]))
    return [{"number": i, "code": code, "label": label} for i, (code, label) in enumerate(items, start=1)]


def require_verified():
    """
    These Type/Nature/Status values were never confirmed against GB4's
    actual menu.php <select> options -- only inferred. Saving a menu
    with a wrong number here silently mis-classifies it. Call this
    before create_menu ever runs, and don't proceed until
    config/menu_enums.json has been checked by hand and '_verified'
    flipped to true.
    """
    if not _VERIFIED:
        raise EnumsNotVerifiedError(
            "config/menu_enums.json has not been verified against GB4's "
            "real menu.php <select> options yet. See '_how_to_verify' in "
            "that file. Refusing to save menus with unverified Type/"
            "Nature/Status values until this is confirmed."
        )
