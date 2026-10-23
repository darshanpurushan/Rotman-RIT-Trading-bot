"""Summarize router sessions without treating submitted limit prices as fills.

Usage: python audit_events.py path/to/algo3_events.jsonl
Keep the input and generated output private unless you intend to share them.
"""

import argparse
import json
from collections import Counter
from pathlib import Path


def summarize(rows):
    sessions = []
    current = None
    for row in rows:
        event = row.get("event")
        if event == "start":
            current = dict(
                version=row.get("version", "unversioned"),
                started=row.get("time"),
                start_tick=None,
                automatic_acceptances=[],
                decision_tenders=set(),
                recovery_reasons=Counter(),
                rearmed=0,
                children=0,
                confirmed_child_volume=0,
                zero_fill_children=0,
                tiny_children=0,
                last_flat_check=None,
            )
            sessions.append(current)
        if current is None:
            continue
        if event == "case_started":
            current["start_tick"] = row.get("tick")
        elif event == "decision":
            current["decision_tenders"].add(row["tender"]["tender_id"])
        elif event == "tender_accepted":
            current["automatic_acceptances"].append(row.get("tender_id"))
        elif event == "recovery":
            current["recovery_reasons"][row.get("reason", "unknown")] += 1
        elif event == "recovery_rearmed":
            current["rearmed"] += 1
        elif event == "child":
            current["children"] += 1
            current["confirmed_child_volume"] += int(row.get("filled", 0))
            current["zero_fill_children"] += row.get("filled", 0) == 0
            current["tiny_children"] += 0 < row.get("requested", 0) <= 100
        elif event == "case_inactive":
            current["last_flat_check"] = row.get("last_active_flat_check")
    for session in sessions:
        session["decision_tenders"] = sorted(session["decision_tenders"])
        session["recovery_reasons"] = dict(session["recovery_reasons"])
    return sessions


def read_events(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {number}") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(read_events(args.events)), indent=2))
