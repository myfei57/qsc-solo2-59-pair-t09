"""单体责任期归属与运行证据链。"""

from __future__ import annotations

import glob
import json
import os

import pytest

from batterystore.errors import StoreError
from batterystore.warranty.chain import GAP_EVIDENCE, build_chain
from batterystore.warranty.events import (
    COMMISSIONED,
    DELIVERED,
    MAINTENANCE_IN,
    MAINTENANCE_OUT,
    RELOCATED,
    EvidenceRef,
    parse_ts,
)
from batterystore.warranty.membership import build_membership, running_intervals
from batterystore.warranty.policy import COMMISSION_BASIS, DELIVERY_BASIS, decide

REF = EvidenceRef("送货单", "DN-1")
DAY = 86400.0


def _history(service, cell_id="c1", *, delivery="2024-01-01", commission="2024-03-01", rack="rk-01"):
    service.record_event(cell_id=cell_id, kind=DELIVERED, at=delivery, rack_id=rack,
                         evidence=EvidenceRef("送货单", "DN-1"))
    service.record_event(cell_id=cell_id, kind=COMMISSIONED, at=commission, rack_id=rack,
                         evidence=EvidenceRef("并网验收单", "GC-1"))


def test_parse_ts_date_and_epoch():
    assert parse_ts("2024-01-01") == parse_ts(1704067200.0)
    with pytest.raises(StoreError):
        parse_ts("not-a-date")


def test_membership_spans_storage_running_maintenance(platform):
    svc = platform.warranty
    _history(svc)
    svc.record_event(cell_id="c1", kind=MAINTENANCE_IN, at="2024-09-01", rack_id="",
                     evidence=EvidenceRef("检修工单", "WO-1"), expected_resume_at="2024-10-01")
    svc.record_event(cell_id="c1", kind=MAINTENANCE_OUT, at="2024-11-01", rack_id="rk-01",
                     evidence=EvidenceRef("检修工单", "WO-1"))
    events = svc.ledger.events_of("c1")
    segments = build_membership(events)
    assert [segment.state for segment in segments] == ["storage", "running", "maintenance", "running"]
    claim = parse_ts("2025-01-01")
    running = running_intervals(segments, claim)
    # 在簇运行 = 3/1~9/1 加 11/1~年底；检修两个月不计运行。
    assert round(sum(end - start for start, end in running) / DAY, 1) == round((184 + 61) * DAY / DAY, 1)


def test_cell_can_flow_between_racks(platform):
    svc = platform.warranty
    _history(svc)
    svc.record_event(cell_id="c1", kind=RELOCATED, at="2024-06-01", rack_id="rk-02",
                     evidence=EvidenceRef("流转记录", "MV-1"))
    overview = svc.cell_overview("c1")
    assert overview["sequence_problems"] == []
    segments = build_membership(
        type(svc.ledger).events_of(svc.ledger, "c1")  # 已按时间排序
    )
    assert segments[-1].rack_id == "rk-02"
    assert segments[-1].state == "running"


def test_two_bases_conflict_and_auto_prefers_commission(platform):
    svc = platform.warranty
    _history(svc, delivery="2024-06-01", commission="2024-12-01")
    decision = decide(svc.ledger.events_of("c1"), parse_ts("2025-02-01"), 180 * DAY)
    assert decision["conflict"] is True
    assert decision["basis_results"][DELIVERY_BASIS]["covered"] is False
    assert decision["basis_results"][COMMISSION_BASIS]["covered"] is True
    assert decision["basis"] == COMMISSION_BASIS
    assert decision["outcome"] == "covered"
    assert "并网投运" in decision["basis_reason"]


def test_forced_basis_and_missing_commission_falls_back(platform):
    svc = platform.warranty
    _history(svc, delivery="2024-06-01", commission="2024-12-01")
    forced = decide(svc.ledger.events_of("c1"), parse_ts("2025-02-01"), 300 * DAY,
                    basis=DELIVERY_BASIS, basis_reason="合同约定")
    assert forced["basis"] == DELIVERY_BASIS
    assert forced["forced_basis"] is True

    cid = "c2"
    svc.record_event(cell_id=cid, kind=DELIVERED, at="2025-01-01", rack_id="rk-01",
                     evidence=EvidenceRef("送货单", "DN-2"))
    fallback = decide(svc.ledger.events_of(cid), parse_ts("2025-03-01"), 400 * DAY)
    assert fallback["basis"] == DELIVERY_BASIS
    assert fallback["outcome"] == "covered"


def test_maintenance_tolls_warranty_window(platform):
    svc = platform.warranty
    _history(svc)
    svc.record_event(cell_id="c1", kind=MAINTENANCE_IN, at="2024-09-01", rack_id="",
                     evidence=EvidenceRef("检修工单", "WO-1"), expected_resume_at="2024-10-01")
    svc.record_event(cell_id="c1", kind=MAINTENANCE_OUT, at="2024-11-01", rack_id="rk-01",
                     evidence=EvidenceRef("检修工单", "WO-1"))
    decision = decide(svc.ledger.events_of("c1"), parse_ts("2025-03-01"), 365 * DAY)
    commission = decision["basis_results"][COMMISSION_BASIS]
    # 2024-03-01 + 365 天 = 2025-03-01（恰好到期），检修 61 天顺延到 5 月初。
    assert commission["toll_days"] == 61.0
    assert commission["covered"] is True
    assert commission["end_date"] == "2025-05-01"


def test_chain_marks_gaps_and_cross_rack_evidence(platform):
    svc = platform.warranty
    _history(svc)
    svc.record_rack_operation(ts="2024-03-05", rack_id="rk-01", op_id="op-1", action="grid_tie", ok=True)
    svc.record_event(cell_id="c1", kind=RELOCATED, at="2024-06-01", rack_id="rk-02",
                     evidence=EvidenceRef("流转记录", "MV-1"))
    svc.record_rack_operation(ts="2024-06-05", rack_id="rk-02", op_id="op-2", action="charge", ok=True)
    chain = build_chain(svc.ledger, "c1", parse_ts("2024-08-01"), max_gap_s=7 * DAY)
    gap_types = [block["gap_type"] for block in chain["blocks"] if block["kind"] == "gap"]
    assert "storage_before_commission" in gap_types
    assert gap_types.count(GAP_EVIDENCE) >= 2  # 两个运行段末段都超 7 天无证据
    ops = [block for block in chain["blocks"] if block["kind"] == "operation"]
    assert {block["rack_id"] for block in ops} == {"rk-01", "rk-02"}
    assert all(block["source"]["journal"] == "warranty_rack_ops" for block in ops)
    assert chain["chain_digest"]


def test_issue_publishes_versioned_immutable_reports(platform):
    svc = platform.warranty
    _history(svc)
    first = svc.issue_conclusion(cell_id="c1", claim_at="2025-06-01", claim_ref="CLM-1", warranty_days=400)
    second = svc.issue_conclusion(cell_id="c1", claim_at="2025-06-01", warranty_days=500,
                                  basis=COMMISSION_BASIS, basis_reason="复核改口径")
    assert first["index"]["version"] == 1
    assert second["index"]["version"] == 2
    assert second["report"]["supersedes"] == first["index"]["report_id"]
    loaded = svc.get_conclusion("c1", 1)
    assert loaded["body_digest"] == first["report"]["body_digest"]
    assert svc.get_conclusion("c1")["version"] == 2
    assert svc.verify_all()["ok"] is True


def test_report_tampering_is_detected(platform):
    svc = platform.warranty
    _history(svc)
    issued = svc.issue_conclusion(cell_id="c1", claim_at="2025-06-01", warranty_days=400)
    doc_id = issued["index"]["doc_id"]
    path = platform.store.path_of(doc_id)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["headline"] = "被改过"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    report = svc.verify_all()
    assert report["ok"] is False
    assert report["archive"]["tampered_reports"] == [doc_id]
    with pytest.raises(Exception):
        svc.get_conclusion("c1", 1)


def test_journal_hash_chain_detects_file_edit(platform):
    svc = platform.warranty
    _history(svc)
    journal = sorted(glob.glob(os.path.join(platform.store.root, "warranty_cells-*.jsonl")))[0]
    with open(journal, encoding="utf-8") as handle:
        lines = handle.readlines()
    record = json.loads(lines[0])
    record["payload"]["note"] = "事后补写"
    lines[0] = json.dumps(record, ensure_ascii=False) + "\n"
    with open(journal, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    assert svc.verify_all()["ledger"]["cells"]["breaks"] == [0]


def test_republish_same_version_rejected(platform):
    svc = platform.warranty
    _history(svc)
    issued = svc.issue_conclusion(cell_id="c1", claim_at="2025-06-01", warranty_days=400)
    with pytest.raises(Exception):
        svc.archive.publish(issued["report"])


def test_rack_operations_auto_recorded_from_platform(platform):
    platform.start()
    platform.transfer_grid_tie()
    ops = platform.warranty.ledger.rack_ops()
    assert any(op.action == "grid_tie" and op.rack_id == "rk-01" for op in ops)
    assert all(op.op_id for op in ops)


def test_sequence_problem_after_decommission(platform):
    svc = platform.warranty
    _history(svc)
    from batterystore.warranty.events import DECOMMISSIONED

    svc.record_event(cell_id="c1", kind=DECOMMISSIONED, at="2024-08-01", rack_id="rk-01",
                     evidence=EvidenceRef("退役单", "RM-1"))
    svc.record_event(cell_id="c1", kind=MAINTENANCE_OUT, at="2024-09-01", rack_id="rk-01",
                     evidence=EvidenceRef("工单", "WO-9"))
    report = svc.issue_conclusion(cell_id="c1", claim_at="2024-10-01", warranty_days=400)["report"]
    assert "event_after_decommission" in report["sequence_problems"]


def test_evidence_ref_requires_doc_identity(platform):
    with pytest.raises(Exception):
        platform.warranty.record_event(
            cell_id="c1", kind=DELIVERED, at="2024-01-01", rack_id="rk-01",
            evidence={"doc_type": "", "doc_no": ""},
        )


def test_maintenance_state_machine_validated(platform):
    svc = platform.warranty
    svc.record_event(cell_id="c1", kind=DELIVERED, at="2024-01-01", rack_id="rk-01",
                     evidence=EvidenceRef("送货单", "D"))
    svc.record_event(cell_id="c1", kind=MAINTENANCE_OUT, at="2024-03-10", rack_id="rk-01",
                     evidence=EvidenceRef("工单", "W"))
    assert "maintenance_out_without_in" in svc.cell_overview("c1")["sequence_problems"]

    svc.record_event(cell_id="c2", kind=DELIVERED, at="2024-01-01", rack_id="rk-01",
                     evidence=EvidenceRef("送货单", "D"))
    svc.record_event(cell_id="c2", kind=MAINTENANCE_IN, at="2024-03-01", rack_id="",
                     evidence=EvidenceRef("工单", "W1"))
    svc.record_event(cell_id="c2", kind=MAINTENANCE_IN, at="2024-03-05", rack_id="",
                     evidence=EvidenceRef("工单", "W2"))
    assert "maintenance_in_without_resume" in svc.cell_overview("c2")["sequence_problems"]

    # 同一履历按逆序录入但时间自洽，不应误报。
    svc.record_event(cell_id="c3", kind=DELIVERED, at="2024-01-01", rack_id="rk-01",
                     evidence=EvidenceRef("送货单", "D"))
    svc.record_event(cell_id="c3", kind=COMMISSIONED, at="2024-02-01", rack_id="rk-01",
                     evidence=EvidenceRef("并网单", "G"))
    svc.record_event(cell_id="c3", kind=MAINTENANCE_OUT, at="2024-03-10", rack_id="rk-01",
                     evidence=EvidenceRef("工单", "W"))
    svc.record_event(cell_id="c3", kind=MAINTENANCE_IN, at="2024-03-01", rack_id="",
                     evidence=EvidenceRef("工单", "W"))
    assert svc.cell_overview("c3")["sequence_problems"] == []
