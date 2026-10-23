"""Acceptance tests for district-heating ledger reconciliation."""

import json
import itertools
import os
import subprocess
import sys

import pytest


ENTRY = os.environ.get("HEAT_LEDGER_ENTRY", "/app/heat_ledger_reconcile.py")
LOOP_SCALES = {"L1": 0.94, "L2": 0.985, "L3": 1.035, "L4": 1.085}


def loss(loop, period_count=3, ambient=2.0, factor=1.7, hours=8):
    return [
        loop["route_m"]
        * factor
        * (loop["supply_c"][index] - ambient)
        * hours
        / 1000.0
        for index in range(period_count)
    ]


def make_payload(loop_count=3, rooms_per_loop=4, period_count=3):
    periods = [f"P{index + 1}" for index in range(period_count)]
    ambient = 2.0
    factor = 1.7
    pressure_coeff = 2.5
    hours = 8
    rooms = []
    for index in range(loop_count * rooms_per_loop):
        rooms.append(
            {
                "room_id": f"R{index + 1:02d}",
                "tenant_id": f"T{index % 4 + 1}",
                "area_m2": float(1 << index) / 4.0,
                "terminal_kwh": [
                    1.2 + 0.37 * index + 0.11 * period
                    for period in range(period_count)
                ],
                "branch_length_m": 2.0 + 0.37 * index,
                "flow_lps": 0.15 + 0.013 * index,
            }
        )
    risers = []
    risers_by_loop = {}
    for loop_index in range(loop_count):
        members = [
            rooms[index]
            for index in range(len(rooms))
            if index % loop_count == loop_index
        ]
        loop_risers = []
        for start in range(0, len(members), 2):
            subset = members[start:start + 2]
            riser_id = f"RS{len(risers) + 1:02d}"
            risers.append(
                {
                    "riser_id": riser_id,
                    "terminal_count": len(subset),
                    "floor_area_m2": sum(room["area_m2"] for room in subset),
                    "pressure_drop_kpa": pressure_coeff
                    * sum(
                        room["branch_length_m"] * room["flow_lps"] ** 2
                        for room in subset
                    ),
                }
            )
            loop_risers.append(riser_id)
        risers_by_loop[f"L{loop_index + 1}"] = loop_risers
    loops = []
    for loop_index in range(loop_count):
        members = [
            rooms[index]
            for index in range(len(rooms))
            if index % loop_count == loop_index
        ]
        loop = {
            "loop_id": f"L{loop_index + 1}",
            "riser_count": len(risers_by_loop[f"L{loop_index + 1}"]),
            "floor_area_m2": sum(room["area_m2"] for room in members),
            "route_m": 18.0 + 7.0 * loop_index,
            "supply_c": [51.0 + loop_index + period for period in range(period_count)],
            "meter_kwh": [],
        }
        pipe = loss(loop, period_count, ambient, factor, hours)
        loop["meter_kwh"] = [
            LOOP_SCALES[loop["loop_id"]]
            * sum(room["terminal_kwh"][period] for room in members)
            + pipe[period]
            for period in range(period_count)
        ]
        loops.append(loop)
    return {
        "site_id": "NORTH-YARD",
        "periods": periods,
        "period_hours": hours,
        "ambient_c": ambient,
        "pipe_loss_w_per_m_k": factor,
        "riser_pressure_coeff": pressure_coeff,
        "rate_per_kwh": 0.68,
        "loops": loops,
        "risers": risers,
        "rooms": rooms,
    }


def encode(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"


def invoke(tmp_path, document, raw=None, suffix="result"):
    source = tmp_path / "export.json"
    output = tmp_path / f"{suffix}.json"
    source.write_bytes((raw if raw is not None else encode(document)).encode("utf-8"))
    completed = subprocess.run(
        [sys.executable, ENTRY, str(source), str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed, source, output


def invoke_ledger(tmp_path, document, suffix="ledgered"):
    source = tmp_path / "export.json"
    output = tmp_path / f"{suffix}.json"
    ledger = tmp_path / f"{suffix}.jsonl"
    source.write_text(encode(document), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, ENTRY, str(source), str(output), str(ledger)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed, source, output, ledger


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def ground_mapping(document):
    return {
        room["room_id"]: loop["loop_id"]
        for index, room in enumerate(document["rooms"])
        for loop in [document["loops"][index % len(document["loops"])]]
    }


def ground_riser_mapping(document):
    mapping = {}
    for riser in document["risers"]:
        candidates = []
        for subset in itertools.combinations(
            document["rooms"], riser["terminal_count"]
        ):
            pressure = document["riser_pressure_coeff"] * sum(
                room["branch_length_m"] * room["flow_lps"] ** 2
                for room in subset
            )
            if (
                abs(sum(room["area_m2"] for room in subset) - riser["floor_area_m2"])
                <= 1e-7
                and abs(pressure - riser["pressure_drop_kpa"]) <= 1e-7
            ):
                candidates.append(subset)
        assert len(candidates) == 1
        for room in candidates[0]:
            mapping[room["room_id"]] = riser["riser_id"]
    return mapping


def test_success_is_silent(tmp_path):
    """题面：「成功时不写标准输出和标准错误。」"""
    result, _, output = invoke(tmp_path, make_payload())
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert output.is_file()


def test_hidden_loop_mapping_is_recovered(tmp_path):
    """题面：「房间到环路的唯一划分与每个环路的倍率要共同满足终端数量、面积和全部计费段」"""
    document = make_payload()
    result, _, output = invoke(tmp_path, document)
    actual = {
        item["room_id"]: item["loop_id"] for item in read(output)["assignments"]
    }
    assert result.returncode == 0
    assert actual == ground_mapping(document)


def test_hidden_riser_mapping_is_recovered(tmp_path):
    """题面：「每个立管接收 `terminal_count` 个房间，每个环路接收 `riser_count` 个立管。」"""
    document = make_payload()
    result, _, output = invoke(tmp_path, document)
    actual = {
        item["room_id"]: item["riser_id"] for item in read(output)["assignments"]
    }
    assert result.returncode == 0
    assert actual == ground_riser_mapping(document)


def test_loop_meter_balance_reconciles(tmp_path):
    """题面：「环路总表读数等于所辖房间终端热量之和乘该路计量倍率，再加管路损失。」"""
    document = make_payload()
    result, _, output = invoke(tmp_path, document)
    totals = {item["loop_id"]: item for item in read(output)["loop_totals"]}
    meters = {item["loop_id"]: item for item in document["loops"]}
    assert result.returncode == 0
    assert all(
        totals[loop_id]["billed_kwh"]
        == pytest.approx(sum(meters[loop_id]["meter_kwh"]))
        for loop_id in meters
    )
    assert {
        loop_id: totals[loop_id]["scale"] for loop_id in meters
    } == pytest.approx({loop_id: LOOP_SCALES[loop_id] for loop_id in meters})


def test_pipe_loss_uses_supply_minus_ambient(tmp_path):
    """题面：「loss_kwh = route_m * pipe_loss_w_per_m_k」"""
    document = make_payload()
    result, _, output = invoke(tmp_path, document)
    totals = {item["loop_id"]: item for item in read(output)["loop_totals"]}
    expected = {
        loop["loop_id"]: sum(
            loss(loop, len(document["periods"]), document["ambient_c"],
                 document["pipe_loss_w_per_m_k"], document["period_hours"])
        )
        for loop in document["loops"]
    }
    assert result.returncode == 0
    assert {key: value["pipe_loss_kwh"] for key, value in totals.items()} == pytest.approx(expected)


def test_loss_is_allocated_by_room_terminal_share(tmp_path):
    """题面：「环路管路损失按该环路各房间全部计费段的终端热量占比分摊。」"""
    result, _, output = invoke(tmp_path, make_payload())
    assignments = read(output)["assignments"]
    totals = {item["loop_id"]: item for item in read(output)["loop_totals"]}
    assert result.returncode == 0
    assert all(
        item["pipe_loss_kwh"]
        == pytest.approx(
            totals[item["loop_id"]]["pipe_loss_kwh"]
            * item["terminal_kwh"]
            / total_terminal
        )
        for item, total_terminal in [
            (
                item,
                sum(
                    other["terminal_kwh"]
                    for other in assignments
                    if other["loop_id"] == item["loop_id"]
                ),
            )
            for item in assignments
        ]
    )


def test_tenant_bills_aggregate_rooms(tmp_path):
    """题面：「租户账单把同一 `tenant_id` 下房间的 `billed_kwh` 相加。」"""
    document = make_payload()
    result, _, output = invoke(tmp_path, document)
    document_result = read(output)
    expected = {}
    for assignment, room in zip(
        document_result["assignments"], sorted(document["rooms"], key=lambda item: item["room_id"])
    ):
        expected[room["tenant_id"]] = expected.get(room["tenant_id"], 0.0) + assignment["billed_kwh"]
    bills = {item["tenant_id"]: item for item in document_result["tenant_bills"]}
    assert result.returncode == 0
    assert {key: value["billed_kwh"] for key, value in bills.items()} == pytest.approx(expected)
    assert all(
        value["cost"] == pytest.approx(value["billed_kwh"] * document["rate_per_kwh"])
        for value in bills.values()
    )


def test_result_has_exact_top_level_keys(tmp_path):
    """题面：「复核单顶层成员是」"""
    result, _, output = invoke(tmp_path, make_payload())
    assert result.returncode == 0
    assert set(read(output)) == {
        "site_id", "status", "assignments", "loop_totals", "tenant_bills"
    }


def test_result_is_single_compact_line(tmp_path):
    """题面：「复核单采用紧凑 JSON，并以单个 LF 结束。」"""
    result, _, output = invoke(tmp_path, make_payload())
    raw = output.read_bytes()
    assert result.returncode == 0
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert b": " not in raw and b", " not in raw


def test_input_top_level_extra_key_is_refused(tmp_path):
    """题面：「导出程序附加未声明成员时，复核请求被拒。」"""
    document = make_payload()
    document["operator"] = "night shift"
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_input_top_level_missing_key_is_refused(tmp_path):
    """题面：「导出程序遗漏任一顶层成员时，复核请求被拒。」"""
    document = make_payload()
    document.pop("rate_per_kwh")
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_short_site_id_is_refused(tmp_path):
    """题面：「`site_id` 是 2–40 个字长的站区简称。」"""
    document = make_payload()
    document["site_id"] = "X"
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_loop_members_are_exact(tmp_path):
    """题面：「环路对象的成员是」"""
    document = make_payload()
    document["loops"][0]["label"] = "west"
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_riser_members_are_exact(tmp_path):
    """题面：「立管记录只接受下列字段：」"""
    document = make_payload()
    document["risers"][0]["label"] = "north"
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_room_members_are_exact(tmp_path):
    """题面：「房间对象的成员是」"""
    document = make_payload()
    document["rooms"][0]["note"] = "corner"
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_boolean_period_hours_is_refused(tmp_path):
    """题面：「布尔值不能代替」"""
    document = make_payload()
    document["period_hours"] = True
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_duplicate_json_key_is_refused(tmp_path):
    """题面：「输入 JSON 无法解码」"""
    raw = '{"site_id":"A","site_id":"B"}\n'
    result, _, output = invoke(tmp_path, None, raw=raw)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_six_periods_are_accepted(tmp_path):
    """题面：「`periods` 数组包含 3 到 6 个互不重复的」"""
    result, _, output = invoke(tmp_path, make_payload(period_count=6))
    assert result.returncode == 0
    assert len(read(output)["loop_totals"]) == 3


def test_seven_periods_are_refused(tmp_path):
    """题面：「`periods` 数组包含 3 到 6 个互不重复的」"""
    result, _, output = invoke(tmp_path, make_payload(period_count=7))
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_sixteen_rooms_are_accepted(tmp_path):
    """题面：「8 到 16 个房间。」"""
    document = make_payload(loop_count=4, rooms_per_loop=4)
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 0
    assert len(read(output)["assignments"]) == 16
    assert len(read(output)["loop_totals"]) == 4


def test_seventeen_rooms_are_refused(tmp_path):
    """题面：「8 到 16 个房间。」"""
    document = make_payload(loop_count=4, rooms_per_loop=4)
    extra = dict(document["rooms"][0])
    extra["room_id"] = "R17"
    document["rooms"].append(extra)
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_duplicate_room_id_is_refused(tmp_path):
    """题面：「房间编号互不重复。」"""
    document = make_payload()
    document["rooms"][1]["room_id"] = document["rooms"][0]["room_id"]
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_supply_equal_to_ambient_is_refused(tmp_path):
    """题面：「`supply_c` 各项大于 `ambient_c`」"""
    document = make_payload()
    document["loops"][0]["supply_c"][0] = document["ambient_c"]
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert not output.exists()


def test_ambiguous_mapping_is_bad_model(tmp_path):
    """题面：「满足条件的划分不唯一」"""
    document = make_payload()
    period_count = len(document["periods"])
    pipe = [loss(loop, period_count) for loop in document["loops"]]
    total = [
        sum(room["terminal_kwh"][period] for room in document["rooms"])
        for period in range(period_count)
    ]
    for loop_index, loop in enumerate(document["loops"]):
        loop["floor_area_m2"] = float(loop["riser_count"])
        loop["meter_kwh"] = [
            total[period] / len(document["loops"]) + pipe[loop_index][period]
            for period in range(period_count)
        ]
    for room in document["rooms"]:
        room["area_m2"] = 1.0
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_MODEL\n"
    assert not output.exists()


def test_terminal_count_total_mismatch_is_bad_model(tmp_path):
    """题面：「每个立管接收 `terminal_count` 个房间，每个环路接收 `riser_count` 个立管。」"""
    document = make_payload()
    document["risers"][0]["terminal_count"] += 1
    result, _, output = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_MODEL\n"
    assert not output.exists()


def test_repeat_run_is_byte_identical(tmp_path):
    """题面：「同一输入再次调用时，复核单字节一致。」"""
    result, source, output = invoke(tmp_path, make_payload())
    first = output.read_bytes()
    repeated = subprocess.run(
        [sys.executable, ENTRY, str(source), str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert repeated.returncode == 0
    assert output.read_bytes() == first


def test_bad_input_preserves_existing_result(tmp_path):
    """题面：「复核请求失败时，复核单与台账保留旧字节」"""
    document = make_payload()
    output = tmp_path / "result.json"
    output.write_bytes(b"old-result\n")
    document["ambient_c"] = "cold"
    result, _, _ = invoke(tmp_path, document)
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_INPUT\n"
    assert output.read_bytes() == b"old-result\n"
    assert "traceback" not in result.stderr.lower()


def test_ledger_has_exact_compact_rows(tmp_path):
    """题面：「台账行成员是」"""
    result, _, output, ledger = invoke_ledger(tmp_path, make_payload())
    row = json.loads(ledger.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert set(row) == {"site_id", "input_sha256", "result_sha256", "billed_kwh"}
    assert ledger.read_bytes().endswith(b"\n")
    assert ledger.read_bytes().count(b"\n") == 1
    assert row["result_sha256"] == __import__("hashlib").sha256(output.read_bytes()).hexdigest()


def test_ledger_total_matches_result(tmp_path):
    """题面：「等于复核单中所有房间」"""
    result, _, output, ledger = invoke_ledger(tmp_path, make_payload())
    row = json.loads(ledger.read_text(encoding="utf-8"))
    total = sum(item["billed_kwh"] for item in read(output)["assignments"])
    assert result.returncode == 0
    assert row["billed_kwh"] == pytest.approx(total)


def test_repeated_success_is_ledger_idempotent(tmp_path):
    """题面：「同一输入再次成功复核时，台账字节一致。」"""
    first, source, output, ledger = invoke_ledger(tmp_path, make_payload())
    before = ledger.read_bytes()
    repeated = subprocess.run(
        [sys.executable, ENTRY, str(source), str(output), str(ledger)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert first.returncode == 0
    assert repeated.returncode == 0
    assert ledger.read_bytes() == before


def test_distinct_result_is_appended(tmp_path):
    """题面：「不同输入产生不同复核单摘要时，台账按成功顺序」"""
    first, source, output, ledger = invoke_ledger(tmp_path, make_payload())
    changed = make_payload()
    changed["rate_per_kwh"] = 0.69
    source.write_text(encode(changed), encoding="utf-8")
    second = subprocess.run(
        [sys.executable, ENTRY, str(source), str(output), str(ledger)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert first.returncode == 0
    assert second.returncode == 0
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2


def test_bad_ledger_preserves_both_old_bytes(tmp_path):
    """题面：「台账无法解码」"""
    first, source, output, ledger = invoke_ledger(tmp_path, make_payload())
    result_before = output.read_bytes()
    ledger.write_bytes(b"\xff")
    repeated = subprocess.run(
        [sys.executable, ENTRY, str(source), str(output), str(ledger)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert first.returncode == 0
    assert repeated.returncode == 1
    assert repeated.stderr == "运行拒收: BAD_LEDGER\n"
    assert output.read_bytes() == result_before
    assert ledger.read_bytes() == b"\xff"


def test_same_result_and_ledger_path_is_refused(tmp_path):
    """题面：「台账路径与复核单路径相同时」"""
    source = tmp_path / "export.json"
    target = tmp_path / "same.json"
    target.write_bytes(b"old\n")
    source.write_text(encode(make_payload()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, ENTRY, str(source), str(target), str(target)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert result.stderr == "运行拒收: BAD_LEDGER\n"
    assert target.read_bytes() == b"old\n"


def test_absent_result_parent_is_io_failure(tmp_path):
    """题面：「复核单无法完整发布时」"""
    source = tmp_path / "export.json"
    output = tmp_path / "nested" / "result.json"
    source.write_text(encode(make_payload()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, ENTRY, str(source), str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert result.stderr == "运行拒收: IO_FAILURE\n"
    assert not output.exists()
    assert "traceback" not in result.stderr.lower()
