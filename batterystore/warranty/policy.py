"""责任期切分与归属判定。

责任期有两种合法切法，结论里两种都必须算清楚：

* ``delivery`` 按交付日期切：责任自送货单交付日起算；
* ``commission`` 按并网投运日期切：责任自并网验收日起算。

检修停用期单体不在运行，两种切法都扣除（停用不是运行消耗）。两种切法给出
的归属不一致时必须二选一并把理由写进结论；选择不是空喊口径，而是绑定具体
履历节点：没有并网凭证就不能主张按并网切，硬选会得到一条证据缺口备注。
"""

from __future__ import annotations

from .events import COMMISSIONED, DELIVERED, CellEvent, iso_date
from .membership import build_membership, maintenance_intervals, running_intervals

DELIVERY_BASIS = "delivery"
COMMISSION_BASIS = "commission"
BASES = (DELIVERY_BASIS, COMMISSION_BASIS)

BASIS_LABELS = {
    DELIVERY_BASIS: "按交付日期切",
    COMMISSION_BASIS: "按并网投运日期切",
}

COVERED = "covered"
EXPIRED = "expired"
UNDETERMINED = "undetermined"

CONCLUSION_LABELS = {
    COVERED: "责任期内，供货方承担",
    EXPIRED: "责任期外，协商处理",
    UNDETERMINED: "起点凭证不足，归属待定",
}

DAY_S = 86400.0

# 自动选基准时优先采用并网口径（责任跟实际运行消耗走）。
AUTO_PREFER = COMMISSION_BASIS


def _event_ts(events: list[CellEvent], kind: str) -> float | None:
    hits = [event.ts for event in events if event.kind == kind]
    return min(hits) if hits else None


def _tolled_window(start_ts: float, maintenance: list[tuple[float, float]], claim_ts: float) -> dict:
    """在起点之外按检修停用区间逐段顺延责任期终点。"""

    toll_s = 0.0
    applied: list[dict] = []
    for begin, finish in maintenance:
        if finish <= start_ts or begin >= claim_ts:
            continue
        clipped_begin = max(begin, start_ts)
        clipped_finish = min(finish, claim_ts)
        amount = max(0.0, clipped_finish - clipped_begin)
        if amount > 0:
            toll_s += amount
            applied.append(
                {
                    "start_ts": round(clipped_begin, 3),
                    "end_ts": round(clipped_finish, 3),
                    "days": round(amount / DAY_S, 3),
                }
            )
    return {"toll_s": round(toll_s, 3), "segments": applied}


def evaluate_basis(
    events: list[CellEvent],
    basis: str,
    claim_ts: float,
    warranty_s: float,
) -> dict:
    """按一种切法计算责任期窗口与是否覆盖索赔时点。"""

    start_ts = _event_ts(events, DELIVERY_BASIS_KIND[basis])
    result = {
        "basis": basis,
        "basis_label": BASIS_LABELS[basis],
        "start_ts": None,
        "start_date": None,
        "raw_end_ts": None,
        "raw_end_date": None,
        "end_ts": None,
        "end_date": None,
        "warranty_days": round(warranty_s / DAY_S, 3),
        "toll_days": 0.0,
        "toll_segments": [],
        "ready": start_ts is not None,
        "covered": None,
        "missing": [] if start_ts is not None else [DELIVERY_BASIS_MISSING[basis]],
    }
    if start_ts is None:
        return result

    segments = build_membership(events)
    toll = _tolled_window(start_ts, maintenance_intervals(segments, claim_ts), claim_ts)
    raw_end = start_ts + warranty_s
    end = raw_end + toll["toll_s"]
    result.update(
        {
            "start_ts": round(start_ts, 3),
            "start_date": iso_date(start_ts),
            "raw_end_ts": round(raw_end, 3),
            "raw_end_date": iso_date(raw_end),
            "end_ts": round(end, 3),
            "end_date": iso_date(end),
            "toll_days": round(toll["toll_s"] / DAY_S, 3),
            "toll_segments": toll["segments"],
            "covered": claim_ts <= end,
        }
    )
    return result


DELIVERY_BASIS_KIND = {
    DELIVERY_BASIS: DELIVERED,
    COMMISSION_BASIS: COMMISSIONED,
}

DELIVERY_BASIS_MISSING = {
    DELIVERY_BASIS: "missing_delivery",
    COMMISSION_BASIS: "missing_commissioning",
}


def _auto_select(evaluated: dict[str, dict]) -> tuple[str, str, list[str]]:
    """两法不一致时按凭证完备性与自动优先级选基准，返回（基准、理由、备注）。"""

    preferred = evaluated[AUTO_PREFER]
    delivery = evaluated[DELIVERY_BASIS]
    notes: list[str] = []
    if preferred["ready"]:
        reason = (
            "责任期跟随单体实际运行消耗：已取得并网投运凭证，按并网投运日期切，"
            "到货后待并网的库存期不计入责任期；检修停用期已扣除。"
        )
        if delivery["ready"] and delivery["covered"] != preferred["covered"]:
            notes.append(
                "两种切法归属不一致：按交付日期切为"
                f"{'责任期内' if delivery['covered'] else '责任期外'}，本结论选择按并网投运日期切。"
            )
        return COMMISSION_BASIS, reason, notes
    if delivery["ready"]:
        reason = "缺少并网投运凭证，无法按并网投运日期切；退回按交付日期切。"
        return DELIVERY_BASIS, reason, notes
    return "", "交付与并网投运凭证均缺失，两种切法都无法起算责任期。", notes


def decide(
    events: list[CellEvent],
    claim_ts: float,
    warranty_s: float,
    *,
    basis: str = "",
    basis_reason: str = "",
) -> dict:
    """计算两种切法，选定基准并给出责任归属结论。"""

    if basis and basis not in BASES:
        raise ValueError(f"未知的责任期切法: {basis}")
    evaluated = {name: evaluate_basis(events, name, claim_ts, warranty_s) for name in BASES}
    conflict = (
        evaluated[DELIVERY_BASIS]["ready"]
        and evaluated[COMMISSION_BASIS]["ready"]
        and evaluated[DELIVERY_BASIS]["covered"] != evaluated[COMMISSION_BASIS]["covered"]
    )

    notes: list[str] = []
    forced = bool(basis)
    if forced:
        chosen = basis
        selected = evaluated[chosen]
        if not selected["ready"]:
            reason = basis_reason or f"指定{BASIS_LABELS[chosen]}，但起点凭证缺失，归属无法判定。"
        else:
            reason = basis_reason or f"按索赔双方约定采用{BASIS_LABELS[chosen]}。"
    else:
        chosen, reason, notes = _auto_select(evaluated)
        selected = evaluated.get(chosen, {})

    chosen_result = evaluated.get(chosen, None)
    if chosen_result and chosen_result["ready"]:
        outcome = COVERED if chosen_result["covered"] else EXPIRED
    else:
        outcome = UNDETERMINED

    segments = build_membership(events)
    running = running_intervals(segments, claim_ts)
    running_s = sum(end - start for start, end in running)
    maintenance = maintenance_intervals(segments, claim_ts)
    maintenance_s = sum(end - start for start, end in maintenance)
    first = events[0].ts if events else None

    if conflict and not forced:
        notes.append("两种切法归属不同，自动选择的理由见 basis_reason；复核可改指定基准重新出版本。")

    return {
        "basis_results": evaluated,
        "conflict": conflict,
        "forced_basis": forced,
        "basis": chosen,
        "basis_label": BASIS_LABELS.get(chosen, ""),
        "basis_reason": reason,
        "notes": notes,
        "outcome": outcome,
        "outcome_label": CONCLUSION_LABELS[outcome],
        "running_s": round(running_s, 3),
        "running_days": round(running_s / DAY_S, 3),
        "maintenance_s": round(maintenance_s, 3),
        "maintenance_days": round(maintenance_s / DAY_S, 3),
        "timeline_start_ts": round(first, 3) if first is not None else None,
        "timeline_start_date": iso_date(first) if first is not None else None,
        "claim_ts": round(claim_ts, 3),
        "claim_date": iso_date(claim_ts),
        "running_intervals": [
            {"start_ts": round(start, 3), "end_ts": round(end, 3), "days": round((end - start) / DAY_S, 3)}
            for start, end in running
        ],
    }
