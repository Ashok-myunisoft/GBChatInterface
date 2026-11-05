"""Configuration-driven capability validation."""
from __future__ import annotations

from datetime import datetime


def _as_date(value):
    text = str(value).strip()
    for pattern in ("%d-%m-%Y", "%d-%m-%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError("Unrecognized date")


def validate(fields: dict, rules: list[dict]) -> list[str]:
    errors = []
    for row in rules:
        field = next((v for k, v in row.items() if k.lower() in {"fieldcode", "fieldname", "field"}), None)
        kind = next((str(v).upper() for k, v in row.items() if k.lower() in {"validationtype", "ruletype", "validationcode"}), "")
        value = fields.get(field) if field is not None else None
        args = next((v for k, v in row.items() if k.lower() in {"configurationjson", "rulevalue", "validationvalue", "value", "parameters", "validationparams"}), None)
        error_message = next((v for k, v in row.items() if k.lower() in {"errormessage", "message"}), None)
        if kind in {"REQUIRED", "IS_REQUIRED"}:
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(error_message or f"{field} is required.")
        elif kind in {"DATE_AFTER_OR_EQUAL", "AFTER_OR_EQUAL"}:
            other = args
            if isinstance(args, dict):
                other = args.get("field") or args.get("compareField")
            if not other and isinstance(args, str):
                other = args
            if value and fields.get(other):
                try:
                    left = _as_date(value)
                    right = _as_date(fields[other])
                    if left < right:
                        errors.append(error_message or f"{field} must be on or after {other}.")
                except ValueError:
                    # Existing leave date parsing remains responsible for date syntax.
                    pass
        else:
            raise ValueError(f"Unsupported validation type: {kind!r}")
    return errors

