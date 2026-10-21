#!/usr/bin/env python3
"""Resolve unnumbered stop gaps only from unambiguous source configuration rows.

Original patent exports are immutable inputs. --apply writes a reviewed CSV
overlay, source-value evidence, and the matching SQLite surface value. It does
not repair merged tables, guess a distance, or promote a quality recommendation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _surface_number(row: dict) -> int | None:
    label = str(row.get("surface_key") or row.get("surface_label") or "").strip(" *")
    return int(label) if label.isdecimal() else None


def _contained_path(root: Path, relative: str) -> Path:
    """Reject absolute paths, traversal, and symlinks outside the declared root."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Prescription paths must be relative to {root}: {relative}")
    result = (root / path).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"Prescription path escapes {root}: {relative}")
    return result


def _db_gap_state(con: sqlite3.Connection, candidate: dict, repair: dict, source: dict) -> dict:
    """Check identity and numeric conflicts under either a read or write lock."""
    matches = con.execute(
        "SELECT thickness_mm,raw_json FROM lens_surfaces "
        "WHERE lens_id=? AND configuration=? AND surface_order=?",
        (candidate["lens_id"], candidate["configuration"], repair["surface_order"]),
    ).fetchall()
    if len(matches) != 1:
        raise ValueError(f"Expected one DB gap for {candidate['simulation_id']}")
    actual = matches[0]
    raw = json.loads(actual[1])
    if (
        not isinstance(raw, dict)
        or str(raw.get("surface_kind", "")).strip().lower() != "stop"
        or str(raw.get("surface_key") or raw.get("surface_label") or "").strip(" *")
        != str(source.get("surface_key") or source.get("surface_label") or "").strip(" *")
    ):
        raise ValueError(f"Source/DB surface identity mismatch for {candidate['simulation_id']}")
    previous = raw.get("reviewed_gap_source", {})
    if actual[0] is not None:
        if (
            actual[0] == repair["value_mm"]
            and isinstance(previous, dict)
            and previous.get("parameter") == repair["parameter"]
            and previous.get("configuration") == candidate["configuration"]
            and previous.get("value_mm") == repair["value_mm"]
        ):
            return {"already_applied": True, "raw": raw, "evidence": previous}
        raise ValueError(f"Refusing to overwrite a numeric DB gap for {candidate['simulation_id']}")
    if str(raw.get("thickness", "")) != str(repair["old_thickness"]):
        raise ValueError(f"Source/DB gap mismatch for {candidate['simulation_id']}")
    return {"already_applied": False, "raw": raw}


def plan_stop_gap_repairs(
    surfaces: list[dict], variables: list[dict], configuration: str
) -> list[dict]:
    """Return only uniquely sourced repairs; no positional guess or default."""
    repairs = []
    for index, row in enumerate(surfaces):
        if row.get("surface_kind", "").lower() != "stop":
            continue
        if "variable" not in str(row.get("thickness", "")).lower():
            continue
        if index == 0 or index == len(surfaces) - 1:
            continue
        previous, following = (
            _surface_number(surfaces[index - 1]),
            _surface_number(surfaces[index + 1]),
        )
        if previous is None or following != previous + 2:
            continue
        number = previous + 1
        declared = _surface_number(row)
        if declared is not None and declared != number:
            continue
        parameter = f"d{number}"
        matches = [
            (line, value)
            for line, value in enumerate(variables, 2)
            if _key(str(value.get("parameter", ""))) == parameter
            and _key(str(value.get("configuration", ""))) == _key(configuration)
        ]
        if len(matches) != 1:
            continue
        line, variable = matches[0]
        try:
            value = float(variable["value"])
        except (KeyError, ValueError, TypeError):
            continue
        if not 0 <= value < float("inf"):
            continue
        repairs.append(
            {
                "surface_order": int(row["surface_order"]),
                "parameter": parameter,
                "configuration": configuration,
                "value_mm": value,
                "value_text": variable["value"],
                "source_variable_line": line,
                "source_variable_row": dict(variable),
                "source_neighbors": [previous, following],
                "old_thickness": row["thickness"],
            }
        )
    return repairs


def repair_database(db_path: Path, export_root: Path, *, apply: bool = False) -> dict:
    db_path, export_root = db_path.resolve(), export_root.resolve()
    data_root = db_path.parent
    if data_root.parent.name == "companies":
        data_root = data_root.parent.parent
    reviewed_root = data_root / "reviewed_prescriptions"
    if apply and db_path.is_relative_to(export_root):
        raise ValueError("Refusing to modify a database inside the immutable export root.")
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        candidates = [
            dict(row)
            for row in con.execute(
                "SELECT s.simulation_id,s.lens_id,s.configuration,s.source_surfaces_csv,"
                "l.source_variable_distances_csv FROM simulation_results s "
                "JOIN lenses l USING(lens_id)"
            )
        ]
    planned = []
    for candidate in candidates:
        original = _contained_path(export_root, candidate["source_surfaces_csv"])
        variable_file = _contained_path(export_root, candidate["source_variable_distances_csv"])
        target = _contained_path(reviewed_root, candidate["source_surfaces_csv"])
        if target.is_relative_to(export_root) or target == db_path:
            raise ValueError("Reviewed output must not overwrite immutable patent source files.")
        if not original.is_file() or not variable_file.is_file():
            continue
        source_rows = read_csv(original)
        repairs = plan_stop_gap_repairs(
            source_rows, read_csv(variable_file), candidate["configuration"]
        )
        if not repairs:
            continue
        # Source CSV and the DB must agree on identity/old token. An existing
        # matching repair is idempotent; unrelated user edits must not be lost.
        applicable = []
        already_applied = []
        source_by_order = {int(row["surface_order"]): row for row in source_rows}
        if len(source_by_order) != len(source_rows):
            raise ValueError(f"Duplicate source surface orders for {candidate['simulation_id']}")
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as con:
            for repair in repairs:
                state = _db_gap_state(
                    con, candidate, repair, source_by_order[repair["surface_order"]]
                )
                if state["already_applied"]:
                    already_applied.append({**repair, "existing_evidence": state["evidence"]})
                    continue
                applicable.append(repair)
        if not applicable:
            continue
        record = {
            **candidate,
            "repairs": applicable,
            "original_surfaces_csv": str(original),
            "original_surfaces_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            "variables_csv": str(variable_file),
            "variables_sha256": hashlib.sha256(variable_file.read_bytes()).hexdigest(),
            "reviewed_surfaces_csv": str(target),
        }
        planned.append(record)
        if apply:
            by_order = {r["surface_order"]: r for r in [*already_applied, *applicable]}
            with sqlite3.connect(db_path) as con:
                con.execute("BEGIN IMMEDIATE")
                for source_row in source_rows:
                    repair = by_order.get(int(source_row["surface_order"]))
                    if repair is None:
                        continue
                    state = _db_gap_state(con, candidate, repair, source_row)
                    evidence = state.get("evidence") or {
                        **repair,
                        "variables_csv": str(variable_file),
                        "variables_sha256": record["variables_sha256"],
                        "method": "explicit_configuration_value_with_bracketing_surface_numbers",
                    }
                    source_row["thickness"] = repair["value_text"]
                    source_row["variable_parameter"] = repair["parameter"]
                    source_row["variable_value_source"] = candidate["configuration"]
                    source_row["reviewed_gap_source"] = json.dumps(evidence, sort_keys=True)
                    if state["already_applied"]:
                        continue
                    raw = state["raw"]
                    raw.update(
                        thickness=repair["value_text"],
                        variable_parameter=repair["parameter"],
                        variable_value_source=candidate["configuration"],
                        reviewed_gap_source=evidence,
                    )
                    changed = con.execute(
                        "UPDATE lens_surfaces SET thickness_mm=?,raw_json=? "
                        "WHERE lens_id=? AND configuration=? AND surface_order=? "
                        "AND thickness_mm IS NULL",
                        (
                            repair["value_mm"],
                            json.dumps(raw, sort_keys=True),
                            candidate["lens_id"],
                            candidate["configuration"],
                            repair["surface_order"],
                        ),
                    )
                    if changed.rowcount != 1:
                        raise ValueError(
                            f"DB gap changed during repair for {candidate['simulation_id']}"
                        )
                target = Path(record["reviewed_surfaces_csv"])
                target.parent.mkdir(parents=True, exist_ok=True)
                columns = list(dict.fromkeys(key for row in source_rows for key in row))
                temporary = target.with_suffix(".csv.tmp")
                with temporary.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(source_rows)
                temporary.replace(target)
    return {
        "database": str(db_path),
        "applied": apply,
        "repaired_configurations": len(planned),
        "repaired_gaps": sum(len(row["repairs"]) for row in planned),
        "evidence": planned,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.output.resolve() == args.db.resolve() or args.output.resolve().is_relative_to(
        args.export_root.resolve()
    ):
        parser.error("Audit output must not overwrite the database or immutable export files.")
    result = repair_database(args.db, args.export_root, apply=args.apply)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "evidence"}))


if __name__ == "__main__":
    main()
