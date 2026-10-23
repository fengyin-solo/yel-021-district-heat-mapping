#!/usr/bin/env python3
"""Recover an unlabelled district-heating topology and publish a billing ledger."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import sys
import tempfile
from pathlib import Path


TOP = {
    "site_id",
    "periods",
    "period_hours",
    "ambient_c",
    "pipe_loss_w_per_m_k",
    "riser_pressure_coeff",
    "rate_per_kwh",
    "loops",
    "risers",
    "rooms",
}
LOOP = {
    "loop_id",
    "riser_count",
    "floor_area_m2",
    "route_m",
    "supply_c",
    "meter_kwh",
}
RISER = {"riser_id", "terminal_count", "floor_area_m2", "pressure_drop_kpa"}
ROOM = {
    "room_id",
    "tenant_id",
    "area_m2",
    "terminal_kwh",
    "branch_length_m",
    "flow_lps",
}
LEDGER = {"site_id", "input_sha256", "result_sha256", "billed_kwh"}


class Refusal(Exception):
    def __init__(self, kind: str):
        self.kind = kind


def fail(kind: str) -> None:
    sys.stderr.write(f"运行拒收: {kind}\n")
    raise SystemExit(1)


def finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("BAD_INPUT")
        result[key] = value
    return result


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != TOP:
        raise Refusal("BAD_INPUT")
    site_id = payload["site_id"]
    if (
        not isinstance(site_id, str)
        or not 2 <= len(site_id) <= 40
        or site_id != site_id.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in site_id)
    ):
        raise Refusal("BAD_INPUT")
    periods = payload["periods"]
    if (
        not isinstance(periods, list)
        or not 3 <= len(periods) <= 6
        or any(not isinstance(item, str) or not item for item in periods)
        or len(set(periods)) != len(periods)
    ):
        raise Refusal("BAD_INPUT")
    period_count = len(periods)
    period_hours = payload["period_hours"]
    ambient = payload["ambient_c"]
    loss_factor = payload["pipe_loss_w_per_m_k"]
    pressure_coeff = payload["riser_pressure_coeff"]
    rate = payload["rate_per_kwh"]
    if (
        isinstance(period_hours, bool)
        or not isinstance(period_hours, int)
        or not 1 <= period_hours <= 24
    ):
        raise Refusal("BAD_INPUT")
    if not finite(ambient) or not -40 <= ambient <= 60:
        raise Refusal("BAD_INPUT")
    if not finite(loss_factor) or not 0 < loss_factor <= 1_000_000:
        raise Refusal("BAD_INPUT")
    if not finite(pressure_coeff) or not 0 < pressure_coeff <= 1_000_000:
        raise Refusal("BAD_INPUT")
    if not finite(rate) or not 0 < rate <= 1_000_000:
        raise Refusal("BAD_INPUT")

    raw_loops = payload["loops"]
    if not isinstance(raw_loops, list) or not 2 <= len(raw_loops) <= 4:
        raise Refusal("BAD_INPUT")
    loops = []
    seen_loops = set()
    for raw in raw_loops:
        if not isinstance(raw, dict) or set(raw) != LOOP:
            raise Refusal("BAD_INPUT")
        identifier = raw["loop_id"]
        riser_count = raw["riser_count"]
        area = raw["floor_area_m2"]
        route = raw["route_m"]
        supply = raw["supply_c"]
        meter = raw["meter_kwh"]
        if not isinstance(identifier, str) or not identifier or identifier in seen_loops:
            raise Refusal("BAD_INPUT")
        seen_loops.add(identifier)
        if (
            isinstance(riser_count, bool)
            or not isinstance(riser_count, int)
            or not 1 <= riser_count <= 4
        ):
            raise Refusal("BAD_INPUT")
        if not finite(area) or area <= 0 or not finite(route) or route <= 0:
            raise Refusal("BAD_INPUT")
        if not isinstance(supply, list) or len(supply) != period_count:
            raise Refusal("BAD_INPUT")
        if not isinstance(meter, list) or len(meter) != period_count:
            raise Refusal("BAD_INPUT")
        if any(not finite(value) or value <= ambient for value in supply):
            raise Refusal("BAD_INPUT")
        if any(not finite(value) or value < 0 for value in meter):
            raise Refusal("BAD_INPUT")
        losses = [
            route
            * loss_factor
            * (float(supply[index]) - float(ambient))
            * period_hours
            / 1000.0
            for index in range(period_count)
        ]
        target = [
            float(meter[index]) - losses[index] for index in range(period_count)
        ]
        if any(value < -1e-7 for value in target):
            raise Refusal("BAD_MODEL")
        loops.append(
            {
                "loop_id": identifier,
                "riser_count": riser_count,
                "floor_area_m2": float(area),
                "losses": losses,
                "meter_target": [float(value) for value in meter],
                "room_target": target,
            }
        )

    raw_risers = payload["risers"]
    if not isinstance(raw_risers, list) or not 2 <= len(raw_risers) <= 8:
        raise Refusal("BAD_INPUT")
    risers = []
    seen_risers = set()
    for raw in raw_risers:
        if not isinstance(raw, dict) or set(raw) != RISER:
            raise Refusal("BAD_INPUT")
        riser_id = raw["riser_id"]
        terminal_count = raw["terminal_count"]
        area = raw["floor_area_m2"]
        pressure = raw["pressure_drop_kpa"]
        if (
            not isinstance(riser_id, str)
            or not riser_id
            or riser_id in seen_risers
        ):
            raise Refusal("BAD_INPUT")
        seen_risers.add(riser_id)
        if (
            isinstance(terminal_count, bool)
            or not isinstance(terminal_count, int)
            or not 1 <= terminal_count <= 4
        ):
            raise Refusal("BAD_INPUT")
        if (
            not finite(area)
            or area <= 0
            or not finite(pressure)
            or pressure <= 0
        ):
            raise Refusal("BAD_INPUT")
        risers.append(
            {
                "riser_id": riser_id,
                "terminal_count": terminal_count,
                "floor_area_m2": float(area),
                "pressure_drop_kpa": float(pressure),
            }
        )

    raw_rooms = payload["rooms"]
    if not isinstance(raw_rooms, list) or not 8 <= len(raw_rooms) <= 16:
        raise Refusal("BAD_INPUT")
    rooms = []
    seen_rooms = set()
    for raw in raw_rooms:
        if not isinstance(raw, dict) or set(raw) != ROOM:
            raise Refusal("BAD_INPUT")
        room_id = raw["room_id"]
        tenant_id = raw["tenant_id"]
        area = raw["area_m2"]
        terminal = raw["terminal_kwh"]
        branch_length = raw["branch_length_m"]
        flow = raw["flow_lps"]
        if not isinstance(room_id, str) or not room_id or room_id in seen_rooms:
            raise Refusal("BAD_INPUT")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise Refusal("BAD_INPUT")
        seen_rooms.add(room_id)
        if (
            not finite(area)
            or area <= 0
            or not finite(branch_length)
            or branch_length <= 0
            or not finite(flow)
            or flow <= 0
        ):
            raise Refusal("BAD_INPUT")
        if not isinstance(terminal, list) or len(terminal) != period_count:
            raise Refusal("BAD_INPUT")
        if any(not finite(value) or value < 0 for value in terminal):
            raise Refusal("BAD_INPUT")
        rooms.append(
            {
                "room_id": room_id,
                "tenant_id": tenant_id,
                "area_m2": float(area),
                "terminal_kwh": [float(value) for value in terminal],
                "branch_length_m": float(branch_length),
                "flow_lps": float(flow),
            }
        )
    return {
        "site_id": site_id,
        "periods": list(periods),
        "ambient_c": float(ambient),
        "riser_pressure_coeff": float(pressure_coeff),
        "rate_per_kwh": float(rate),
        "loops": loops,
        "risers": risers,
        "rooms": rooms,
    }


def close_vector(left, right):
    return all(abs(a - b) <= 1e-7 for a, b in zip(left, right))


def recover_mapping(loops, risers, rooms, period_count, pressure_coeff):
    riser_room_candidates = []
    for riser in risers:
        choices = []
        for subset in itertools.combinations(
            range(len(rooms)), riser["terminal_count"]
        ):
            if (
                abs(
                    sum(rooms[index]["area_m2"] for index in subset)
                    - riser["floor_area_m2"]
                )
                > 1e-7
            ):
                continue
            pressure = pressure_coeff * sum(
                rooms[index]["branch_length_m"]
                * rooms[index]["flow_lps"] ** 2
                for index in subset
            )
            if abs(pressure - riser["pressure_drop_kpa"]) <= 1e-7:
                choices.append(subset)
        if not choices:
            raise Refusal("BAD_MODEL")
        riser_room_candidates.append(choices)

    room_partitions = []

    def assign_risers(position, remaining, chosen):
        if len(room_partitions) > 1:
            return
        if position == len(risers):
            if not remaining:
                room_partitions.append(dict(chosen))
            return
        riser_index = position
        for subset in riser_room_candidates[riser_index]:
            if any(index not in remaining for index in subset):
                continue
            updated = dict(chosen)
            for index in subset:
                updated[index] = risers[riser_index]["riser_id"]
            assign_risers(
                position + 1,
                remaining.difference(subset),
                updated,
            )

    assign_risers(0, set(range(len(rooms))), {})
    if len(room_partitions) != 1:
        raise Refusal("BAD_MODEL")
    room_to_riser = room_partitions[0]

    all_risers = set(range(len(risers)))
    loop_partitions = []

    def assign_loops(loop_index, remaining, chosen):
        if len(loop_partitions) > 1:
            return
        if loop_index == len(loops):
            if not remaining:
                loop_partitions.append(dict(chosen))
            return
        loop = loops[loop_index]
        count = loop["riser_count"]
        if count > len(remaining):
            return
        for subset in itertools.combinations(sorted(remaining), count):
            if (
                abs(
                    sum(risers[index]["floor_area_m2"] for index in subset)
                    - loop["floor_area_m2"]
                )
                > 1e-7
            ):
                continue
            room_indexes = [
                room_index
                for room_index, riser_id in room_to_riser.items()
                if riser_id in {risers[index]["riser_id"] for index in subset}
            ]
            totals = [
                sum(
                    rooms[room_index]["terminal_kwh"][period]
                    for room_index in room_indexes
                )
                for period in range(period_count)
            ]
            if any(value <= 0 for value in totals):
                continue
            scales = [
                loop["room_target"][period] / totals[period]
                for period in range(period_count)
            ]
            scale = scales[0]
            if not 0.8 <= scale <= 1.2:
                continue
            if any(abs(value - scale) > 1e-7 for value in scales[1:]):
                continue
            updated = dict(chosen)
            for riser_index in subset:
                updated[risers[riser_index]["riser_id"]] = loop["loop_id"]
            assign_loops(
                loop_index + 1,
                remaining.difference(subset),
                updated,
            )

    loop_order = sorted(
        range(len(loops)),
        key=lambda index: (
            loops[index]["riser_count"],
            loops[index]["floor_area_m2"],
            loops[index]["loop_id"],
        ),
    )
    ordered_loops = [loops[index] for index in loop_order]
    original_loops = loops
    loops = ordered_loops
    assign_loops(0, all_risers, {})
    loops = original_loops
    if len(loop_partitions) != 1:
        raise Refusal("BAD_MODEL")
    riser_to_loop = loop_partitions[0]
    return {
        room_index: (
            riser_to_loop[room_to_riser[room_index]],
            room_to_riser[room_index],
        )
        for room_index in range(len(rooms))
    }


def reconcile(payload):
    loops = payload["loops"]
    rooms = payload["rooms"]
    period_count = len(payload["periods"])
    mapping = recover_mapping(
        loops,
        payload["risers"],
        rooms,
        period_count,
        payload["riser_pressure_coeff"],
    )
    by_id = {loop["loop_id"]: loop for loop in loops}

    assigned = {loop["loop_id"]: [] for loop in loops}
    for room_index, room in enumerate(rooms):
        assigned[mapping[room_index][0]].append(room_index)

    assignments = []
    loss_by_room = {}
    scale_by_loop = {}
    for loop_id, indexes in assigned.items():
        loop = by_id[loop_id]
        room_total_sum = sum(
            sum(rooms[index]["terminal_kwh"]) for index in indexes
        )
        totals = [
            sum(rooms[index]["terminal_kwh"][period] for index in indexes)
            for period in range(period_count)
        ]
        scale_candidates = [
            loop["room_target"][period] / totals[period]
            for period in range(period_count)
        ]
        loop_scale = sum(scale_candidates) / len(scale_candidates)
        scale_by_loop[loop_id] = loop_scale
        loop_loss = sum(loop["losses"])
        for index in indexes:
            terminal_total = sum(rooms[index]["terminal_kwh"])
            if room_total_sum == 0:
                allocated = loop_loss / len(indexes)
            else:
                allocated = loop_loss * terminal_total / room_total_sum
            loss_by_room[index] = allocated
            assignments.append(
                {
                    "room_id": rooms[index]["room_id"],
                    "riser_id": mapping[index][1],
                    "loop_id": loop_id,
                    "terminal_kwh": terminal_total,
                    "pipe_loss_kwh": allocated,
                    "billed_kwh": terminal_total * loop_scale + allocated,
                }
            )
    assignments.sort(key=lambda item: item["room_id"])

    loop_totals = []
    for loop in loops:
        indexes = assigned[loop["loop_id"]]
        terminal_total = sum(
            sum(rooms[index]["terminal_kwh"]) for index in indexes
        )
        pipe_total = sum(loop["losses"])
        loop_totals.append(
            {
                "loop_id": loop["loop_id"],
                "riser_count": loop["riser_count"],
                "scale": scale_by_loop[loop["loop_id"]],
                "terminal_kwh": terminal_total,
                "pipe_loss_kwh": pipe_total,
                "billed_kwh": terminal_total * scale_by_loop[loop["loop_id"]] + pipe_total,
            }
        )
    loop_totals.sort(key=lambda item: item["loop_id"])

    tenant_accumulator = {}
    for index, room in enumerate(rooms):
        entry = tenant_accumulator.setdefault(
            room["tenant_id"], {"billed_kwh": 0.0, "room_count": 0}
        )
        room_loop = mapping[index][0]
        entry["billed_kwh"] += (
            sum(room["terminal_kwh"]) * scale_by_loop[room_loop]
            + loss_by_room[index]
        )
        entry["room_count"] += 1
    tenant_bills = [
        {
            "tenant_id": tenant_id,
            "billed_kwh": value["billed_kwh"],
            "cost": value["billed_kwh"] * payload["rate_per_kwh"],
            "room_count": value["room_count"],
        }
        for tenant_id, value in tenant_accumulator.items()
    ]
    tenant_bills.sort(key=lambda item: item["tenant_id"])
    return {
        "site_id": payload["site_id"],
        "status": "reconciled",
        "assignments": assignments,
        "loop_totals": loop_totals,
        "tenant_bills": tenant_bills,
    }


def parse_ledger(path, expected_entry):
    if not path.exists():
        encoded = (
            json.dumps(expected_entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        return encoded, {}
    payload = path.read_bytes()
    if payload and not payload.endswith(b"\n"):
        raise Refusal("BAD_LEDGER")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal("BAD_LEDGER")
    known = {}
    for line in text.splitlines():
        try:
            row = json.loads(line, object_pairs_hook=pairs_hook)
        except (ValueError, Refusal):
            raise Refusal("BAD_LEDGER")
        if not isinstance(row, dict) or set(row) != LEDGER:
            raise Refusal("BAD_LEDGER")
        if not isinstance(row["site_id"], str):
            raise Refusal("BAD_LEDGER")
        if not isinstance(row["input_sha256"], str) or len(row["input_sha256"]) != 64:
            raise Refusal("BAD_LEDGER")
        if not isinstance(row["result_sha256"], str) or len(row["result_sha256"]) != 64:
            raise Refusal("BAD_LEDGER")
        if not finite(row["billed_kwh"]):
            raise Refusal("BAD_LEDGER")
        previous = known.get(row["result_sha256"])
        if previous is not None and previous != row:
            raise Refusal("BAD_LEDGER")
        known[row["result_sha256"]] = row
    previous = known.get(expected_entry["result_sha256"])
    if previous is not None:
        if previous != expected_entry:
            raise Refusal("BAD_LEDGER")
        return payload, known
    encoded = (
        json.dumps(expected_entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    return payload + encoded, known


def publish(targets, contents):
    for target in targets:
        if not target.parent.is_dir() or target.is_dir():
            raise Refusal("IO_FAILURE")
    old = {target: target.read_bytes() if target.exists() else None for target in targets}
    stages = []
    try:
        for target, content in zip(targets, contents):
            descriptor, name = tempfile.mkstemp(prefix=".heat-", dir=target.parent)
            stage = Path(name)
            stages.append(stage)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for stage, target in zip(stages, targets):
            os.replace(stage, target)
        stages.clear()
    except OSError:
        for target, previous in old.items():
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
        raise Refusal("IO_FAILURE")
    finally:
        for stage in stages:
            stage.unlink(missing_ok=True)


def main(argv):
    if len(argv) not in {3, 4}:
        fail("BAD_INPUT")
    source = Path(argv[1])
    result_path = Path(argv[2])
    ledger_path = Path(argv[3]) if len(argv) == 4 else None
    if ledger_path is not None and ledger_path == result_path:
        fail("BAD_LEDGER")
    try:
        source_bytes = source.read_bytes()
        payload = json.loads(source_bytes.decode("utf-8"), object_pairs_hook=pairs_hook)
        clean = validate(payload)
        result = reconcile(clean)
        result_bytes = (
            json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if ledger_path is None:
            publish([result_path], [result_bytes])
        else:
            total = sum(item["billed_kwh"] for item in result["assignments"])
            entry = {
                "site_id": result["site_id"],
                "input_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                "billed_kwh": total,
            }
            ledger_bytes, _ = parse_ledger(ledger_path, entry)
            publish([result_path, ledger_path], [result_bytes, ledger_bytes])
        return 0
    except Refusal as exc:
        fail(exc.kind)
    except (OSError, UnicodeError, ValueError):
        fail("BAD_INPUT")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
