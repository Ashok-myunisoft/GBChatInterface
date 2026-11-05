"""Read capability records without assuming unverified table column names."""
from __future__ import annotations

import re
from typing import Any

from .db import connection

TABLES = {
    "capability": "maicapability",
    "field": "maicapabilityfield",
    "validation": "maicapabilityvalidation",
    "payload": "maicapabilitypayload",
    "version": "maicapabilityversion",
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class CapabilityRepository:
    """Schema-aware reader; identifiers come only from information_schema."""

    def _columns(self, cur, table: str) -> dict[str, str]:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND lower(table_name) = lower(%s)",
            (table,),
        )
        cols = [row[0] for row in cur.fetchall()]
        if not cols:
            raise RuntimeError(f"Required capability table {table} was not found in current_schema().")
        return {_norm(col): col for col in cols}

    def _column_type(self, cur, table: str, column: str) -> str:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND lower(table_name) = lower(%s) "
            "AND lower(column_name) = lower(%s)",
            (table, column),
        )
        row = cur.fetchone()
        return row[0].lower() if row else ""

    @staticmethod
    def _find(columns: dict[str, str], *names: str, required: bool = True) -> str | None:
        for name in names:
            if _norm(name) in columns:
                return columns[_norm(name)]
        if required:
            raise RuntimeError("Capability schema is missing expected column: " + "/".join(names))
        return None

    def _select(self, cur, table: str, filters: list[tuple[str, Any]], active: bool = True):
        cols = self._columns(cur, table)
        clauses, params = [], []
        for candidates, value in filters:
            col = self._find(cols, *candidates.split("|"))
            clauses.append(f'"{col}" = %s')
            params.append(value)
        if active:
            active_col = self._find(cols, "isactive", "active", required=False)
            if active_col:
                dtype = self._column_type(cur, table, active_col)
                if dtype in {"smallint", "integer", "bigint", "numeric"}:
                    clauses.append(f'"{active_col}" = 1')
                elif dtype in {"character varying", "character", "text"}:
                    clauses.append(f'lower("{active_col}") IN (\'1\', \'true\', \'active\', \'y\')')
                else:
                    clauses.append(f'"{active_col}" = TRUE')
        cur.execute(f'SELECT * FROM "{table}" WHERE ' + " AND ".join(clauses), params)
        names = [d.name for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()], cols

    def load_by_code(self, code: str) -> dict[str, Any] | None:
        with connection() as conn, conn.cursor() as cur:
            caps, ccols = self._select(cur, TABLES["capability"], [("aicapabilitycode|capabilitycode|code", code)])
            if not caps:
                return None
            cap = caps[0]
            cap_id_col = self._find(ccols, "aicapabilityid", "capabilityid", "id")
            cap_id = cap[cap_id_col]
            versions, vcols = self._select(cur, TABLES["version"], [("aicapabilityid|capabilityid", cap_id)])
            if not versions:
                raise RuntimeError(f"No active configuration version exists for {code}.")
            vstatus = self._find(vcols, "status", "versionstatus", required=False)
            if vstatus:
                number_col = self._find(vcols, "versionnumber", "versionno", required=False)
                versions.sort(
                    key=lambda row: (
                        str(row.get(vstatus, "")).upper() in {"ACTIVE", "PUBLISHED"},
                        row.get(number_col, 0) if number_col else 0,
                    ),
                    reverse=True,
                )
            version = versions[0]
            version_id = version[self._find(vcols, "aicapabilityversionid", "versionid", "id")]
            result = {"capability": cap, "version": version}
            for kind in ("field", "validation", "payload"):
                cols = self._columns(cur, TABLES[kind])
                cap_rel = self._find(cols, "aicapabilityid", "capabilityid", required=False)
                ver_rel = self._find(cols, "aicapabilityversionid", "capabilityversionid", "versionid", required=False)
                filters = []
                if cap_rel:
                    filters.append(("aicapabilityid|capabilityid", cap_id))
                if ver_rel:
                    filters.append(("aicapabilityversionid|capabilityversionid|versionid", version_id))
                if not filters:
                    raise RuntimeError(f"{TABLES[kind]} has no capability or version relationship column.")
                rows, _ = self._select(cur, TABLES[kind], filters, active=True)
                if kind == "validation":
                    field_rows, _ = self._select(
                        cur, TABLES["field"], [("aicapabilityid|capabilityid", cap_id)], active=True
                    )
                    field_id_col = self._find(self._columns(cur, TABLES["field"]), "aicapabilityfieldid", "fieldid", "id")
                    field_name_col = self._find(self._columns(cur, TABLES["field"]), "fieldname", "fieldcode")
                    field_names = {row[field_id_col]: row[field_name_col] for row in field_rows}
                    validation_cols = self._columns(cur, TABLES["validation"])
                    validation_field_col = self._find(validation_cols, "aicapabilityfieldid", "capabilityfieldid", "fieldid", required=False)
                    if validation_field_col:
                        for row in rows:
                            row["fieldname"] = field_names.get(row.get(validation_field_col))
                result[kind + "s"] = rows
            return result

