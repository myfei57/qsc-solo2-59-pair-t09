"""责任期归属判定。

两种切法在同一份证据时间线上各算一遍，绝不只算一种再口头解释差异：

``delivery`` 责任期自交付日起连续计算，检修停运与搬迁离网都不暂停——
              设备已交到现场，风险责任面连续；
``grid``     责任期自首次并网投运起，只累计实际并网时长，因检修/搬迁的
              离网区间从责任期中扣除——"质保跟着实际运行走"。

两种切法只在中间发生过检修停运或搬迁时才会给出不同归属；一旦不同，切法
选择本身就必须写进结论（:func:`attribute_liability` 的 ``basis_reason``），
复核人据此判断选择是否站得住。证据链结构性断裂或缺段覆盖索赔时点时，任何
一种切法都只能判 ``inconclusive``，不允许用相邻证据把缺口补上再认责。
"""

from __future__ import annotations

# 默认责任期：5 年（秒）。合同另有约定时由调用方覆盖。
DEFAULT_WARRANTY_TERM_S = 5 * 365 * 24 * 3600.0

BASES = ("delivery", "grid")
LIABLE_SUPPLIER = "supplier"
LIABLE_OWNER = "owner"
LIABLE_UNKNOWN = "unknown"
STATUS_COVERED = "within_warranty"
STATUS_EXPIRED = "expired"
STATUS_INCONCLUSIVE = "inconclusive"


def attribute_liability(
    timeline: dict,
    *,
    claim_ts: float,
    warranty_term_s: float = DEFAULT_WARRANTY_TERM_S,
    preferred_basis: str | None = None,
) -> dict:
    """对一份单体证据时间线给出责任期归属结论。"""

    term = float(warranty_term_s)
    if term <= 0:
        raise ValueError("责任期长度必须为正")
    claim_ts = round(float(claim_ts), 3)
    delivery = _delivery_view(timeline, claim_ts, term)
    grid = _grid_view(timeline, claim_ts, term)
    selected, reason, selection_code = _select_basis(timeline, delivery, grid, preferred_basis)
    views = {"delivery": delivery, "grid": grid}
    chosen = views[selected]
    return {
        "cell_id": timeline.get("cell_id", ""),
        "claim_ts": claim_ts,
        "warranty_term_s": term,
        "selected_basis": selected,
        "basis_reason": reason,
        "selection_code": selection_code,
        "bases_diverge": _bases_diverge(delivery, grid),
        "conclusion": chosen["status"],
        "liable_party": chosen["liable_party"],
        "covered_until_ts": chosen["covered_until_ts"],
        "remaining_seconds": chosen["remaining_seconds"],
        "accrued_seconds": chosen["accrued_seconds"],
        "views": views,
        "caveats": _caveats(timeline, delivery, grid, selected),
    }


def _delivery_view(timeline: dict, claim_ts: float, term: float) -> dict:
    """按交付日期切：责任期从交付日连续滚动，离网不暂停。"""

    delivered_ts = timeline.get("delivered_ts")
    if delivered_ts is None:
        return _inconclusive("缺少交付记录，无法按交付日期切分")
    breaks = timeline.get("breaks", [])
    if breaks:
        return _inconclusive("证据链存在结构性断点，交付日锚点不可信", anchor_ts=delivered_ts)
    accrued = max(0.0, claim_ts - delivered_ts)
    covered_until = round(delivered_ts + term, 3)
    within = claim_ts <= covered_until
    if within and _gap_overlaps(timeline, delivered_ts, claim_ts):
        return _inconclusive(
            "责任窗口内存在运行证据缺段，无法认定实际运行面",
            anchor_ts=delivered_ts,
            covered_until=covered_until,
            accrued=accrued,
        )
    return {
        "basis": "delivery",
        "status": STATUS_COVERED if within else STATUS_EXPIRED,
        "liable_party": LIABLE_SUPPLIER if within else LIABLE_OWNER,
        "anchor_ts": round(delivered_ts, 3),
        "covered_until_ts": covered_until,
        "accrued_seconds": round(accrued, 3),
        "remaining_seconds": round(max(0.0, covered_until - claim_ts), 3),
        "paused_seconds": 0.0,
        "inconclusive_reason": "",
    }


def _grid_view(timeline: dict, claim_ts: float, term: float) -> dict:
    """按并网投运日期切：只累计并网在役时长，检修/搬迁离网区间扣除。"""

    first_ts = timeline.get("first_commission_ts")
    if first_ts is None:
        return _inconclusive("缺少并网投运记录，无法按并网日期切分")
    breaks = timeline.get("breaks", [])
    if breaks:
        return _inconclusive("证据链存在结构性断点，并网投运锚点不可信", first_ts)
    accrued_full = _in_service_seconds(timeline, claim_ts)
    out_of_service_s = _out_of_service_seconds(timeline, claim_ts)
    covered_until = _project_covered_until(timeline, claim_ts, term)
    within = accrued_full < term
    if within and _gap_overlaps(timeline, first_ts, claim_ts):
        return _inconclusive(
            "并网责任窗口内存在运行证据缺段，实际运行时长无法认定",
            anchor_ts=first_ts,
            covered_until=covered_until,
            accrued=accrued_full,
        )
    return {
        "basis": "grid",
        "status": STATUS_COVERED if within else STATUS_EXPIRED,
        "liable_party": LIABLE_SUPPLIER if within else LIABLE_OWNER,
        "anchor_ts": round(first_ts, 3),
        "covered_until_ts": covered_until,
        "accrued_seconds": round(accrued_full, 3),
        "remaining_seconds": round(max(0.0, term - accrued_full), 3),
        "paused_seconds": round(out_of_service_s, 3),
        "inconclusive_reason": "",
    }


def _inconclusive(
    reason: str,
    anchor_ts: float | None = None,
    covered_until: float | None = None,
    accrued: float | None = None,
) -> dict:
    return {
        "basis": "",
        "status": STATUS_INCONCLUSIVE,
        "liable_party": LIABLE_UNKNOWN,
        "anchor_ts": round(anchor_ts, 3) if anchor_ts is not None else None,
        "covered_until_ts": covered_until,
        "accrued_seconds": round(accrued, 3) if accrued is not None else None,
        "remaining_seconds": None,
        "paused_seconds": None,
        "inconclusive_reason": reason,
    }


def _in_service_seconds(timeline: dict, claim_ts: float) -> float:
    total = 0.0
    for segment in timeline.get("segments", []):
        if segment["state"] != "in_service":
            continue
        end = segment["end_ts"] if segment["end_ts"] is not None else claim_ts
        total += max(0.0, min(end, claim_ts) - segment["start_ts"])
    return total


def _out_of_service_seconds(timeline: dict, claim_ts: float) -> float:
    total = 0.0
    for segment in timeline.get("segments", []):
        if segment["state"] != "out_of_service":
            continue
        end = segment["end_ts"] if segment["end_ts"] is not None else claim_ts
        total += max(0.0, min(end, claim_ts) - segment["start_ts"])
    return total


def _project_covered_until(timeline: dict, claim_ts: float, term: float) -> float | None:
    """按时间线推算并网切法的责任期到期时刻：在役推进、离网暂停。"""

    first_ts = timeline.get("first_commission_ts")
    if first_ts is None:
        return None
    consumed = 0.0
    current_state = timeline["segments"][-1]["state"] if timeline.get("segments") else "pre_delivery"
    for segment in timeline.get("segments", []):
        if segment["state"] != "in_service":
            continue
        end = segment["end_ts"] if segment["end_ts"] is not None else claim_ts
        end = min(end, claim_ts)
        span = max(0.0, end - segment["start_ts"])
        if consumed + span >= term:
            return round(segment["start_ts"] + (term - consumed), 3)
        consumed += span
    # 到索赔时点仍未满期：只有当前处于在役区间才能把到期点投到未来；若正处于
    # 检修/搬迁离网开口段，到期点取决于何时重新投运，无法确定，留空。
    if current_state != "in_service":
        return None
    return round(claim_ts + (term - consumed), 3)


def _gap_overlaps(timeline: dict, start_ts: float, eval_ts: float) -> bool:
    """索赔评估区间 [start_ts, eval_ts] 是否落在某个缺段里。

    开口缺段（末次证据之后再无新证据）的终点取评估时刻：索赔时点之前没有
    新证据，缺口就实质覆盖到索赔时点；评估时刻之后的未来空当不影响认责。
    """

    for gap in timeline.get("gaps", []):
        gap_start = gap["start_ts"]
        gap_end = gap["end_ts"]
        if gap_end is None:
            gap_end = eval_ts if gap.get("open_ended") else gap_start
        if gap_end >= start_ts and gap_start <= eval_ts:
            return True
    return False


def _bases_diverge(delivery: dict, grid: dict) -> bool:
    """两种切法的归属结论是否不同（含一方无法认定）。"""

    return delivery["status"] != grid["status"] or delivery["liable_party"] != grid["liable_party"]


def _select_basis(
    timeline: dict,
    delivery: dict,
    grid: dict,
    preferred_basis: str | None,
) -> tuple[str, str, str]:
    """选定本次结论采用的切法并给出必须写进结论的理由。"""

    if preferred_basis is not None and preferred_basis not in BASES:
        raise ValueError(f"未知的责任期切法: {preferred_basis}")
    if preferred_basis is not None:
        if preferred_basis == "delivery":
            return "delivery", "按合同约定或现场指定，责任期自交付日期起连续计算，检修与搬迁不暂停。", "preferred_delivery"
        return "grid", "按合同约定或现场指定，责任期自首次并网投运起按实际并网时长计算，检修/搬迁离网期间暂停。", "preferred_grid"
    if not timeline.get("structurally_intact", True):
        return "delivery", "并网投运链条存在结构性断点，按交付日期切分是唯一仍有锚点的口径；结论本身判为无法认定。", "fallback_delivery_broken_grid"
    if grid["status"] == STATUS_INCONCLUSIVE and delivery["status"] != STATUS_INCONCLUSIVE:
        return "delivery", "并网投运证据不足以支撑按并网日期切分，退回按交付日期切分。", "fallback_delivery_missing_grid"
    if grid["status"] != STATUS_INCONCLUSIVE:
        return "grid", "并网投运证据链完整，责任期按实际并网运行时长计算，检修/搬迁离网期间不计入责任期。", "auto_grid"
    if delivery["status"] != STATUS_INCONCLUSIVE:
        return "delivery", "缺少可用的并网投运证据，按交付日期切分。", "fallback_delivery"
    return "delivery", "两种切法均缺少必要锚点，无法认定责任归属。", "no_anchor"


def _caveats(timeline: dict, delivery: dict, grid: dict, selected: str) -> list[str]:
    """把复核人需要额外留意的事项挂到结论上（不改变判定）。"""

    caveats: list[str] = []
    if _bases_diverge(delivery, grid):
        caveats.append("按交付日期与按并网日期两种切法归属不同，结论以选定切法为准；差异源于中间检修停运或搬迁。")
    for item in timeline.get("anomalies", []):
        if item["severity"] == "warning":
            caveats.append(f"存在需人工确认的告警：{item['message']}")
    chosen = delivery if selected == "delivery" else grid
    if chosen["status"] == STATUS_INCONCLUSIVE:
        caveats.append(chosen["inconclusive_reason"])
    return caveats
