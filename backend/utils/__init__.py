LEAVE_TYPE_MASTER = {
    "casual leave": "-1500000000",
    "cl": "-1500000000",
    "absent": "-1499999999",
    "a": "-1499999999",
    "sick leave": "-1499999998",
    "sl": "-1499999998",
    "loss of pay": "-1499999997",
    "lop": "-1499999997",
    "earned leave": "-1499999996",
    "el": "-1499999996",
    "maternity leave": "-1499999995",
    "ml": "-1499999995",
    "comp off leave": "-1499999994",
    "coff": "-1499999994",
}


def map_leave_type_id(value: str) -> str | None:
    """
    Maps user-entered leave type to LeaveTypeId
    """
    if not value:
        return None

    key = value.lower().strip()
    return LEAVE_TYPE_MASTER.get(key)


REASON_MAP = {
    "personal": ("-1499999996", "Personal"),
    "health issue": ("-1499999995", "Health Issue"),
    "health": ("-1499999995", "Health Issue"),
    "emergency": ("-1499999994", "Emergency"),
}


def map_leave_reason(reason: str):
    if not reason:
        return None, None

    r = reason.lower().strip()

    if r in ("personal", "personal issue"):
        return "-1499999996", "Personal"
    if r in ("health", "health issue", "sick"):
        return "-1499999995", "Health Issue"
    if r in ("emergency",):
        return "-1499999994", "Emergency"

    return None, None


def time_to_minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)
