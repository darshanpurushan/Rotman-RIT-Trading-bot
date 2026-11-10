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
    confirmed_fills = {}
    for row in rows:
        event = row.get("event")
        if event == "start":
            confirmed_fills = {}
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
        elif event in ("child", "pending_order_resolved"):
            filled = int(row.get("filled", 0))
            order_id = row.get("order_id")
            if order_id is None:
                # Legacy child records have no identity for deduplication.
                if event == "child":
                    current["confirmed_child_volume"] += filled
            else:
                # Recovery can be the only fill evidence, or precede a
                # normal child record for the same confirmed total.
                key = str(order_id)
                previous = confirmed_fills.get(key, 0)
                current["confirmed_child_volume"] += max(0, filled - previous)
                confirmed_fills[key] = max(previous, filled)
            if event == "child":
                current["children"] += 1
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
