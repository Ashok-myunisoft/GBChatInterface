"""Allowlisted resolvers and computed values for APPLY_LEAVE mappings."""
from __future__ import annotations

from datetime import datetime
import re
import uuid

from biztransactionid import LEAVE_TRANSACTION_CLASS_ID
from biztransactionid import TIME_SLIP_TRANSACTION_CLASS_ID
from biztransactionid.service import get_biz_transaction_type_id


def resolver_registry(login: dict) -> dict:
    return {
        "BIZ_TRANSACTION_TYPE": lambda: get_biz_transaction_type_id(LEAVE_TRANSACTION_CLASS_ID, login),
        "GENERATE_UUID": lambda: str(uuid.uuid4()),
    }


def permission_resolver_registry(login: dict) -> dict:
    return {
        "BIZ_TRANSACTION_TYPE": lambda: get_biz_transaction_type_id(TIME_SLIP_TRANSACTION_CLASS_ID, login),
        "GENERATE_UUID": lambda: str(uuid.uuid4()),
    }


def computation_registry(fields: dict) -> dict:
    def date(field):
        value = fields.get(field)
        if not value:
            raise ValueError(f"{field} is required to compute its ERP date.")
        parsed = datetime.strptime(str(value), "%d-%m-%Y")
        return parsed

    def permission_date():
        value = str(fields.get("TimeSlipDate", "")).strip()
        if not value:
            raise ValueError("TimeSlipDate is required to compute its ERP date.")
        match = re.search(r"/Date\((-?\d+)", value)
        if match:
            return datetime.fromtimestamp(int(match.group(1)) / 1000)
        for pattern in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, pattern)
            except ValueError:
                pass
        raise ValueError(f"Invalid TimeSlipDate: {value}")

    def minutes(field):
        value = str(fields.get(field, "")).strip()
        try:
            hour, minute = map(int, value.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            return hour * 60 + minute
        except (ValueError, TypeError):
            raise ValueError(f"Invalid {field}; expected HH:MM.") from None

    def duration_minutes():
        start, end = minutes("FromTime"), minutes("ToTime")
        return (end - start) % (24 * 60)

    def permission_duration_format():
        start, end = minutes("FromTime"), minutes("ToTime")
        duration = end - start
        if duration <= 0:
            raise ValueError("ToTime must be later than FromTime to compute Permission duration.")
        return f"{duration // 60}:{duration % 60:02}"

    return {
        "FROM_DATE_ERP": lambda: date("FromDate").strftime("%Y-%m-%d"),
        "TO_DATE_ERP": lambda: date("ToDate").strftime("%Y-%m-%d"),
        "LEAVE_DAYS_FLOAT": lambda: float(fields["NumberOfDays"]),
        "DAY_TYPE_INTEGER": lambda: int(fields.get("TLeaveDayTypeCode", 0)),
        "FROM_DATE_DAY_NAME": lambda: date("FromDate").strftime("%A"),
        "PERMISSION_DATE_ERP": lambda: permission_date().strftime("%Y-%m-%d"),
        "PERMISSION_DURATION_MINUTES": duration_minutes,
        "PERMISSION_FROM_TIME_MINUTES": lambda: minutes("FromTime"),
        "PERMISSION_TO_TIME_MINUTES": lambda: minutes("ToTime"),
        "PERMISSION_DURATION_HHMM": lambda: f"{duration_minutes() // 60:02}:{duration_minutes() % 60:02}",
        "TIME_SLIP_START_MINUTES": lambda: minutes("FromTime"),
        "TIME_SLIP_END_MINUTES": lambda: minutes("ToTime"),
        "TIME_SLIP_DURATION_FORMAT": permission_duration_format,
    }

