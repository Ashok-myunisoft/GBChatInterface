"""Allowlisted resolvers and computed values for APPLY_LEAVE mappings."""
from __future__ import annotations

from datetime import datetime
import uuid

from biztransactionid import LEAVE_TRANSACTION_CLASS_ID
from biztransactionid.service import get_biz_transaction_type_id


def resolver_registry(login: dict) -> dict:
    return {
        "BIZ_TRANSACTION_TYPE": lambda: get_biz_transaction_type_id(LEAVE_TRANSACTION_CLASS_ID, login),
        "GENERATE_UUID": lambda: str(uuid.uuid4()),
    }


def computation_registry(fields: dict) -> dict:
    def date(field):
        value = fields.get(field)
        if not value:
            raise ValueError(f"{field} is required to compute its ERP date.")
        parsed = datetime.strptime(str(value), "%d-%m-%Y")
        return parsed

    return {
        "FROM_DATE_ERP": lambda: date("FromDate").strftime("%Y-%m-%d"),
        "TO_DATE_ERP": lambda: date("ToDate").strftime("%Y-%m-%d"),
        "LEAVE_DAYS_FLOAT": lambda: float(fields["NumberOfDays"]),
        "DAY_TYPE_INTEGER": lambda: int(fields.get("TLeaveDayTypeCode", 0)),
        "FROM_DATE_DAY_NAME": lambda: date("FromDate").strftime("%A"),
    }

