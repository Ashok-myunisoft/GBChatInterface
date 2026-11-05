"""APPLY_LEAVE orchestration over the generic config, validation and builder."""
from __future__ import annotations

from .configuration_loader import load_capability
from .payload_builder import build_payload
from .resolver_register import computation_registry, resolver_registry
from .validation_engine import validate


def prepare_apply_leave(slots: dict, login: dict, department_details: dict) -> tuple[dict, dict]:
    config = load_capability("APPLY_LEAVE")
    values = dict(slots)
    values.setdefault("EmployeeName", login.get("UserName", ""))
    nested = {
        **values,
        "LeaveType": {"Id": values.get("LeaveTypeId"), "Name": values.get("LeaveType", "")},
        "Reason": {"Id": values.get("ReasonId"), "Name": values.get("Reason", "")},
    }
    errors = validate(values, config.get("validations", []))
    if errors:
        raise ValueError(" ".join(errors))
    payload = build_payload(
        config.get("payloads", []), login=login, fields=nested,
        resolvers=resolver_registry(login), computations=computation_registry(values),
        department_details=department_details,
    )
    return payload, config

