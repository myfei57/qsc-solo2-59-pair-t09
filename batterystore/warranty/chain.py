"""证据链拼装。

复核人拿到结论后，第一件事是沿时间轴核对：什么时候在哪一簇、有什么运行
证据、哪段时间没有证据。证据链因此不是"把命中的记录罗列出来"，而是把单体
全部状态段按时间铺开：履历节点给凭证定位，运行段内挂簇级运行动作，相邻
证据间隔超过阈值时插入显式缺证标记；检修超期、索赔时仍在检修、到货前索赔
等异常也各有显式标记，绝不静默跳过。
"""

from __future__ import annotations

from .events import KIND_EVIDENCE_LABELS, CellEvent, iso_date
from .hashing import canonical_digest
from .ledger import RACK_OP_LABELS, CellLedger, RackOpEvidence
from .membership import (
    MAINTENANCE,
    RETIRED,
    RUNNING,
    STORAGE,
    build_membership,
)

# 运行段内相邻运行证据超过该间隔（秒）即判定为证据缺段（默认 7 天）。
DEFAULT_MAX_GAP_S = 7 * 24 * 3600.0

LIFECYCLE = "lifecycle"
OPERATION = "operation"
GAP = "gap"

GAP_STORAGE = "storage_before_commission"
GAP_EVIDENCE = "evidence_gap"
GAP_BEFORE_FIRST_OP = "running_before_first_evidence"
GAP_MAINTENANCE = "maintenance"
GAP_MAINTENANCE_OVERDUE = "maintenance_overdue"
GAP_RETIRED = "retired"
GAP_UNDER_MAINTENANCE = "under_maintenance_at_claim"
GAP_BEFORE_DELIVERY = "before_delivery"

GAP_LABELS = {
    GAP_STORAGE: "到货待并网（非运行期）",
    GAP_EVIDENCE: "运行证据缺段",
    GAP_BEFORE_FIRST_OP: "在簇但本段尚无运行动作记录",
    GAP_MAINTENANCE: "检修停用（非运行期）",
    GAP_MAINTENANCE_OVERDUE: "检修超期未复装",
    GAP_RETIRED: "退役后（非运行期）",
    GAP_UNDER_MAINTENANCE: "索赔时仍在检修中",
    GAP_BEFORE_DELIVERY: "交付之前",
}


def _lifecycle_item(ledger: CellLedger, event: CellEvent) -> dict:
    locate = ledger.cell_journal.locate(event.seq)
    return {
        "kind": LIFECYCLE,
        "ts": event.ts,
        "date": iso_date(event.ts),
        "title": KIND_EVIDENCE_LABELS.get(event.kind, event.kind),
        "event_kind": event.kind,
        "rack_id": event.rack_id,
        "evidence": event.evidence.as_dict(),
        "note": event.note,
        "source": {
            "journal": "warranty_cells",
            "seq": event.seq,
            "segment": None if locate is None else locate.get("segment"),
            "line": None if locate is None else locate.get("line"),
        },
    }


def _operation_item(ledger: CellLedger, op: RackOpEvidence) -> dict:
    locate = ledger.rack_op_journal.locate(op.seq)
    return {
        "kind": OPERATION,
        "ts": op.ts,
        "date": iso_date(op.ts),
        "title": RACK_OP_LABELS.get(op.action, op.action),
        "rack_id": op.rack_id,
        "op_id": op.op_id,
        "action": op.action,
        "ok": op.ok,
        "note": op.note,
        "source": {
            "journal": "warranty_rack_ops",
            "seq": op.seq,
            "segment": None if locate is None else locate.get("segment"),
            "line": None if locate is None else locate.get("line"),
        },
    }


def _gap_item(gap_type: str, start_ts: float, end_ts: float, *, rack_id: str = "", detail: str = "") -> dict:
    duration_s = max(0.0, round(end_ts - start_ts, 3))
    return {
        "kind": GAP,
        "gap_type": gap_type,
        "title": GAP_LABELS.get(gap_type, gap_type),
        "ts": round(start_ts, 3),
        "end_ts": round(end_ts, 3),
        "date": iso_date(start_ts),
        "end_date": iso_date(end_ts),
        "duration_s": duration_s,
        "rack_id": rack_id,
        "detail": detail,
    }


def _running_blocks(
    ledger: CellLedger,
    start_ts: float,
    end_ts: float,
    ops: list[RackOpEvidence],
    max_gap_s: float,
    rack_id: str,
) -> list[dict]:
    """铺开一个运行段：运行证据按时间插入，相邻间隔过大处放缺证标记。"""

    blocks: list[dict] = []
    if end_ts <= start_ts:
        return blocks
    marks: list[tuple[float, RackOpEvidence | None]] = [(start_ts, None)]
    marks.extend((op.ts, op) for op in ops)
    marks.append((end_ts, None))
    for pair_index, ((left_ts, op), (right_ts, _unused)) in enumerate(zip(marks, marks[1:])):
        if op is not None:
            blocks.append(_operation_item(ledger, op))
        if right_ts - left_ts > max_gap_s:
            # 段起点到首条证据的间隔表示"本段尚无运行动作"，不是证据间断档。
            gap_type = GAP_EVIDENCE if pair_index > 0 else GAP_BEFORE_FIRST_OP
            blocks.append(
                _gap_item(
                    gap_type,
                    left_ts,
                    right_ts,
                    rack_id=rack_id,
                    detail="该簇在这段时间内没有落到台账的运行动作记录",
                )
            )
    return blocks


def build_chain(
    ledger: CellLedger,
    cell_id: str,
    claim_ts: float,
    *,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
) -> dict:
    """为一只单体拼装按时间排序的证据链与缺段清单。"""

    events = ledger.events_of(cell_id)
    events_by_key = {(event.kind, event.seq): event for event in events}
    segments = build_membership(events)
    all_ops = ledger.rack_ops()

    blocks: list[dict] = []
    if events and events[0].ts > claim_ts:
        blocks.append(_gap_item(GAP_BEFORE_DELIVERY, claim_ts, events[0].ts))

    for segment in segments:
        if segment.start_ts >= claim_ts:
            break
        start = segment.start_ts
        end = claim_ts if segment.end_ts is None else min(segment.end_ts, claim_ts)

        start_event = events_by_key.get((segment.start_kind, segment.start_seq))
        if start_event is not None:
            blocks.append(_lifecycle_item(ledger, start_event))

        if segment.state == STORAGE:
            blocks.append(_gap_item(GAP_STORAGE, start, end))
        elif segment.state == MAINTENANCE:
            gap_type = GAP_UNDER_MAINTENANCE if segment.end_ts is None else GAP_MAINTENANCE
            detail = ""
            if (
                gap_type == GAP_MAINTENANCE
                and segment.expected_resume_ts is not None
                and segment.end_ts is not None
                and segment.end_ts > segment.expected_resume_ts
            ):
                gap_type = GAP_MAINTENANCE_OVERDUE
                detail = f"实际复装晚于工单预计 {round((segment.end_ts - segment.expected_resume_ts) / 86400.0, 1)} 天"
            blocks.append(_gap_item(gap_type, start, end, rack_id=segment.rack_id, detail=detail))
        elif segment.state == RETIRED:
            blocks.append(_gap_item(GAP_RETIRED, start, end, rack_id=segment.rack_id))
        elif segment.state == RUNNING:
            ops = [op for op in all_ops if op.rack_id == segment.rack_id and start <= op.ts < end and op.ts < claim_ts]
            blocks.extend(_running_blocks(ledger, start, end, ops, max_gap_s, segment.rack_id))

    ordered = sorted(blocks, key=lambda item: (item["ts"], 0 if item["kind"] != GAP else 1))
    for item in ordered:
        item["digest"] = canonical_digest({k: v for k, v in item.items() if k != "digest"})
    gaps = [item for item in ordered if item["kind"] == GAP]
    evidence = [item for item in ordered if item["kind"] != GAP]
    return {
        "cell_id": cell_id,
        "claim_ts": round(claim_ts, 3),
        "max_gap_s": round(max_gap_s, 3),
        "blocks": ordered,
        "evidence_count": len(evidence),
        "gap_count": len(gaps),
        "gap_seconds": round(sum(item["duration_s"] for item in gaps if item["gap_type"] == GAP_EVIDENCE), 3),
        "gaps": gaps,
        "chain_digest": canonical_digest([item["digest"] for item in ordered]),
        "delivery_ts": events[0].ts if events else None,
    }
