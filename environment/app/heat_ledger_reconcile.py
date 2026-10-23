#!/usr/bin/env python3
"""Legacy room-order allocation for a district heating export."""

import json
import sys
from pathlib import Path


def main(argv):
    if len(argv) != 3:
        return 1
    source, target = Path(argv[1]), Path(argv[2])
    payload = json.loads(source.read_text(encoding="utf-8"))
    loops = payload["loops"]
    assignments = []
    cursor = 0
    for loop in loops:
        for room in payload["rooms"][cursor:cursor + loop["terminal_count"]]:
            total = sum(room["terminal_kwh"])
            assignments.append(
                {
                    "room_id": room["room_id"],
                    "loop_id": loop["loop_id"],
                    "terminal_kwh": total,
                    "pipe_loss_kwh": 0.0,
                    "billed_kwh": total,
                }
            )
        cursor += loop["terminal_count"]
    result = {
        "site_id": payload["site_id"],
        "status": "reconciled",
        "assignments": assignments,
        "loop_totals": [],
        "tenant_bills": [],
    }
    target.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
