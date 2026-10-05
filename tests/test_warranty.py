"""单体责任期归属与索赔证据链。

覆盖四个索赔时绕不开的点：责任期两种切法在检修/搬迁后给出不同归属、证据链
按时间拼接且缺段显式标出、结构性断点只能判无法认定、材料包生成后不可改写
只能追加复核版本且能被哈希链验出来。
"""

from __future__ import annotations

import glob
import json
import os

from batterystore.errors import WarrantyError
from batterystore.warranty.events import CellEvent
from batterystore.warranty.ledger import CellEventLedger, cell_slug
from batterystore.warranty.liability import (
    STATUS_COVERED,
    STATUS_EXPIRED,
    STATUS_INCONCLUSIVE,
)
from batterystore.warranty.package import build_claim_package
from batterystore.warranty.timeline import build_cell_timeline

DAY = 86400.0
TERM_5Y = 5 * 365 * DAY


def _record(platform, cell_id, action, advance_days=0.0, **payload):
    if advance_days:
        platform.clock.advance(advance_days * DAY)
    return platform.record_cell_event(cell_id, action, payload=payload)


def test_event_hash_chain_detects_rewrite(tmp_path):
    ledger = CellEventLedger(str(tmp_path / "state"))
    ledger.record("c1", "delivered", 1000.0)
    ledger.record("c1", "commissioned", 1001.0, {"rack_id": "rk-01"})
    assert ledger.verify_cell("c1")["ok"]
    path = sorted(glob.glob(os.path.join(str(tmp_path / "state"), "cellevents-c1-*.jsonl")))[0]
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    record = json.loads(lines[0])
    record["source"] = "forged"
    lines[0] = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    result = CellEventLedger(str(tmp_path / "state")).verify_cell("c1")
    assert not result["ok"]
    assert any("摘要不匹配" in item for item in result["errors"])


def test_runtime_event_requires_positive_grid_seconds():
    try:
        CellEvent.create(1, "c1", "runtime", 1.0, {"grid_seconds": 0})
    except WarrantyError:
        pass
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("零并网时长的运行证据必须被拒")


def test_cell_slug_rejects_empty_id():
    try:
        cell_slug("  ")
    except WarrantyError:
        pass
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("空白单体标识必须被拒")


def test_both_bases_agree_without_maintenance(platform):
    cid = "rk-01-p1c1"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=5, rack_id="rk-01")
    _record(platform, cid, "runtime", advance_days=10, rack_id="rk-01", grid_seconds=10 * DAY)
    platform.clock.advance(10 * DAY)
    claim_ts = platform.clock.now()
    attribution = platform.cell_attribution(cid, claim_ts=claim_ts)
    assert attribution["selected_basis"] == "grid"
    assert attribution["bases_diverge"] is False
    assert attribution["views"]["delivery"]["status"] == STATUS_COVERED
    assert attribution["views"]["grid"]["status"] == STATUS_COVERED
    assert attribution["basis_reason"]


def test_bases_diverge_after_maintenance_pause(platform):
    cid = "rk-02-p3c3"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=10, rack_id="rk-02")
    for _ in range(18):
        _record(platform, cid, "runtime", advance_days=80, rack_id="rk-02", grid_seconds=80 * DAY)
    _record(platform, cid, "decommissioned", reason="maintenance")
    platform.clock.advance(200 * DAY)
    _record(platform, cid, "recommissioned", rack_id="rk-02")
    for _ in range(4):
        _record(platform, cid, "runtime", advance_days=80, rack_id="rk-02", grid_seconds=80 * DAY)
    attribution = platform.cell_attribution(cid, claim_ts=platform.clock.now(), warranty_term_s=TERM_5Y)
    delivery = attribution["views"]["delivery"]
    grid = attribution["views"]["grid"]
    assert delivery["status"] == STATUS_EXPIRED
    assert grid["status"] == STATUS_COVERED
    assert attribution["bases_diverge"] is True
    assert abs(grid["paused_seconds"] - 200 * DAY) < 1.0
    assert attribution["selected_basis"] == "grid"
    assert any("归属不同" in item for item in attribution["caveats"])
    timeline = platform.cell_timeline(cid, as_of=platform.clock.now())
    assert any(segment["state"] == "out_of_service" for segment in timeline["segments"])


def test_explicit_basis_override_writes_reason(platform):
    cid = "rk-01-p1c2"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=5, rack_id="rk-01")
    attribution = platform.cell_attribution(cid, claim_ts=platform.clock.now() + 5 * DAY, preferred_basis="delivery")
    assert attribution["selected_basis"] == "delivery"
    assert attribution["selection_code"] == "preferred_delivery"
    assert "交付日期" in attribution["basis_reason"]


def test_relocation_stitches_racks_in_order(platform):
    cid = "cell-flow"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=5, rack_id="rk-01")
    _record(platform, cid, "runtime", advance_days=30, rack_id="rk-01", grid_seconds=30 * DAY)
    _record(platform, cid, "decommissioned", reason="relocation")
    platform.clock.advance(3 * DAY)
    _record(platform, cid, "relocated", to_rack="rk-02")
    platform.clock.advance(2 * DAY)
    _record(platform, cid, "recommissioned", rack_id="rk-02")
    _record(platform, cid, "runtime", advance_days=40, rack_id="rk-02", grid_seconds=40 * DAY)
    timeline = platform.cell_timeline(cid)
    assert timeline["structurally_intact"] is True
    assert timeline["racks_visited"] == ["rk-01", "rk-02"]
    in_service = [item for item in timeline["rack_flow"] if item["state"] == "in_service"]
    assert [item["rack_id"] for item in in_service] == ["rk-01", "rk-02"]


def test_relocation_without_decommission_is_break(platform):
    cid = "cell-badmove"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=1, rack_id="rk-01")
    _record(platform, cid, "relocated", advance_days=1, to_rack="rk-02")
    timeline = platform.cell_timeline(cid)
    assert not timeline["structurally_intact"]
    assert any(item["code"] == "relocated_without_decommission" for item in timeline["breaks"])
    attribution = platform.cell_attribution(cid, claim_ts=platform.clock.now())
    assert attribution["conclusion"] == STATUS_INCONCLUSIVE


def test_missing_runtime_evidence_is_explicit(platform):
    cid = "cell-noruntime"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=2, rack_id="rk-01")
    platform.clock.advance(200 * DAY)
    timeline = platform.cell_timeline(cid)
    assert any(gap["code"] == "missing_runtime_evidence" for gap in timeline["gaps"])
    attribution = platform.cell_attribution(cid, claim_ts=platform.clock.now())
    assert attribution["conclusion"] == STATUS_INCONCLUSIVE
    assert attribution["liable_party"] == "unknown"


def test_evidence_gap_between_runs_is_marked(platform):
    cid = "cell-gap"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=2, rack_id="rk-01")
    _record(platform, cid, "runtime", advance_days=10, rack_id="rk-01", grid_seconds=10 * DAY)
    platform.clock.advance(300 * DAY)
    _record(platform, cid, "runtime", rack_id="rk-01", grid_seconds=5 * DAY)
    timeline = platform.cell_timeline(cid, as_of=platform.clock.now())
    assert any(gap["code"] == "evidence_gap" and gap["duration_s"] >= 300 * DAY for gap in timeline["gaps"])


def test_backfilled_event_is_warning_not_silently_accepted(platform):
    cid = "cell-backfill"
    now = platform.clock.now()
    platform.record_cell_event(cid, "commissioned", ts=now - 10 * DAY, payload={"rack_id": "rk-01"})
    platform.record_cell_event(cid, "delivered", ts=now - 20 * DAY)
    timeline = platform.cell_timeline(cid)
    assert timeline["structurally_intact"] is True
    assert any(item["code"] == "event_out_of_order" for item in timeline["anomalies"])


def test_delivery_only_falls_back_to_delivery_basis(platform):
    cid = "cell-delivered"
    _record(platform, cid, "delivered")
    platform.clock.advance(100 * DAY)
    attribution = platform.cell_attribution(cid, claim_ts=platform.clock.now())
    assert attribution["selected_basis"] == "delivery"
    assert attribution["views"]["grid"]["status"] == STATUS_INCONCLUSIVE
    assert attribution["views"]["delivery"]["status"] == STATUS_COVERED


def test_claim_versions_are_append_only(platform):
    cid = "rk-01-p2c4"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=3, rack_id="rk-01")
    _record(platform, cid, "runtime", advance_days=10, rack_id="rk-01", grid_seconds=10 * DAY)
    platform.clock.advance(5 * DAY)
    v1 = platform.issue_claim(cid, note="首版")
    assert v1["version"] == 1
    try:
        platform.issue_claim(cid)
    except WarrantyError:
        pass
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("已有材料包不能再签发首版")
    try:
        platform.review_claim(cid, reviewer="")
    except WarrantyError:
        pass
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("复核必须署名")
    platform.clock.advance(20 * DAY)
    _record(platform, cid, "runtime", rack_id="rk-01", grid_seconds=20 * DAY)
    v2 = platform.review_claim(cid, reviewer="li.gong", note="补证据复核")
    assert v2["version"] == 2
    assert v2["prev_version_hash"] == v1["version_hash"]
    assert v2["package"]["review_note"]["events_added"] == 1
    versions = platform.claim_versions(cid)
    assert len(versions) == 2
    # 老版本字节不变
    assert versions[0]["version_hash"] == v1["version_hash"]
    assert versions[0]["package"]["input_fingerprint"]["count"] == 3
    verification = platform.verify_claim(cid)
    assert verification["package_chain"]["ok"]
    assert verification["event_chain"]["ok"]


def test_claim_tampering_is_detected_on_disk(platform):
    cid = "rk-01-p2c5"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=2, rack_id="rk-01")
    platform.issue_claim(cid)
    store_root = platform.store.root
    path = [
        name
        for name in glob.glob(os.path.join(store_root, "claim-*.jsonl"))
        if os.path.basename(name).startswith(f"claim-{cell_slug(cid)}")
    ][0]
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    record = json.loads(lines[0])
    record["note"] = "篡改备注"
    lines[0] = json.dumps(record, ensure_ascii=False)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    verification = platform.verify_claim(cid)
    assert not verification["package_chain"]["ok"]
    assert any("内容哈希不匹配" in item for item in verification["package_chain"]["errors"])


def test_package_freeze_is_independent_of_later_events(platform):
    cid = "cell-freeze"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=2, rack_id="rk-01")
    claim_ts = platform.clock.now() + 10 * DAY
    v1 = platform.issue_claim(cid, claim_ts=claim_ts)
    # 索赔时点之后又发生的新事件不能影响已固化材料
    platform.clock.advance(400 * DAY)
    _record(platform, cid, "runtime", rack_id="rk-01", grid_seconds=5 * DAY)
    latest = platform.latest_claim(cid)
    assert latest["version_hash"] == v1["version_hash"]
    assert latest["package"]["claim_ts"] == v1["package"]["claim_ts"]


def test_build_package_includes_full_review_material(platform):
    cid = "cell-material"
    _record(platform, cid, "delivered")
    _record(platform, cid, "commissioned", advance_days=2, rack_id="rk-01")
    _record(platform, cid, "runtime", advance_days=5, rack_id="rk-01", grid_seconds=5 * DAY)
    events = platform.cell_events.list_events(cid)
    package = build_claim_package(
        cid, events, claim_ts=platform.clock.now(), warranty_term_s=TERM_5Y
    )
    assert package["kind"] == "cell_warranty_claim"
    assert package["input_fingerprint"]["count"] == 3
    assert package["timeline"]["segments"]
    assert package["conclusion"]["selected_basis"] in ("delivery", "grid")
    # 材料自带完整事件，复核人无需连台账即可重放
    rebuilt = build_cell_timeline([CellEvent.from_dict(item) for item in package["events"]], as_of=package["claim_ts"])
    assert rebuilt["racks_visited"] == ["rk-01"]


def test_warranty_overview_reports_chains(platform):
    cid = "cell-overview"
    _record(platform, cid, "delivered")
    platform.issue_claim(cid)
    overview = platform.warranty_overview()
    assert overview["tracked_cells"] >= 1
    assert overview["claimed_cells"] >= 1
    assert overview["all_intact"] is True
