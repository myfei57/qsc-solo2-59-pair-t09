"""单体履历事件与凭证引用。

责任归属只能建立在履历之上：每只单体从出厂到索赔会经过交付、并网投运、
检修、跨簇流转与退役等节点。事件只追加不修改；现场凭单（送货单、并网验收
单、检修工单、流转记录）通过 :class:`EvidenceRef` 挂到事件上，复核人据此
能找到原始单据。节点时间统一折算成 Unix 秒，日期串按 UTC 当日零点解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..errors import StoreError

# 交付：风险与表面责任的起点（按交付日期切责任期时使用）。
DELIVERED = "delivered"
# 并网投运：实际运行起点（按并网投运日期切责任期时使用）。
COMMISSIONED = "commissioned"
# 检修入厂：单体离开簇、停止运行（可带预计复装时间）。
MAINTENANCE_IN = "maintenance_in"
# 检修复装：单体重新装回某簇，恢复运行。
MAINTENANCE_OUT = "maintenance_out"
# 跨簇流转：单体从一簇拆出装入另一簇。
RELOCATED = "relocated"
# 退役：单体彻底退出运行，之后不再计入运行期。
DECOMMISSIONED = "decommissioned"

CELL_KINDS = (
    DELIVERED,
    COMMISSIONED,
    MAINTENANCE_IN,
    MAINTENANCE_OUT,
    RELOCATED,
    DECOMMISSIONED,
)

# 履历事件自身的证据类型，出证据链时直接展示给复核人。
KIND_EVIDENCE_LABELS = {
    DELIVERED: "交付凭证",
    COMMISSIONED: "并网投运凭证",
    MAINTENANCE_IN: "检修工单（入厂）",
    MAINTENANCE_OUT: "检修工单（复装）",
    RELOCATED: "跨簇流转记录",
    DECOMMISSIONED: "退役记录",
}


@dataclass(frozen=True)
class EvidenceRef:
    """一张原始单据的定位信息。"""

    doc_type: str
    doc_no: str
    note: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "EvidenceRef":
        doc_type = str(raw.get("doc_type", "")).strip()
        doc_no = str(raw.get("doc_no", "")).strip()
        if not doc_type or not doc_no:
            raise StoreError("凭证必须给出单据类型与单据编号", component="warranty")
        return cls(doc_type=doc_type, doc_no=doc_no, note=str(raw.get("note", "")))

    def as_dict(self) -> dict:
        return {"doc_type": self.doc_type, "doc_no": self.doc_no, "note": self.note}


@dataclass(frozen=True)
class CellEvent:
    """一条单体履历事件。

    ``seq`` 是台账全局序号，落盘后即定位坐标；``ts`` 为节点 Unix 秒；
    ``rack_id`` 表示事件之后单体所在簇（检修入厂时传空串）。
    """

    seq: int
    cell_id: str
    kind: str
    ts: float
    rack_id: str
    evidence: EvidenceRef
    expected_resume_ts: float | None = None
    recorded_ts: float = 0.0
    recorded_by: str = ""
    note: str = ""

    @classmethod
    def create(
        cls,
        seq: int,
        cell_id: str,
        kind: str,
        ts: float,
        rack_id: str,
        evidence: EvidenceRef,
        *,
        expected_resume_ts: float | None = None,
        recorded_ts: float = 0.0,
        recorded_by: str = "",
        note: str = "",
    ) -> "CellEvent":
        cell_id = str(cell_id).strip()
        if not cell_id:
            raise StoreError("单体标识不能为空", component="warranty")
        if kind not in CELL_KINDS:
            raise StoreError(f"未知的履历事件类型: {kind}", component="warranty")
        if kind != MAINTENANCE_IN and not str(rack_id).strip():
            raise StoreError(f"{kind} 事件必须指明所在簇", component="warranty")
        if expected_resume_ts is not None and float(expected_resume_ts) < float(ts):
            raise StoreError("预计复装时间不能早于入厂时间", component="warranty")
        return cls(
            seq=int(seq),
            cell_id=cell_id,
            kind=kind,
            ts=round(float(ts), 3),
            rack_id=str(rack_id).strip(),
            evidence=evidence,
            expected_resume_ts=None if expected_resume_ts is None else round(float(expected_resume_ts), 3),
            recorded_ts=round(float(recorded_ts), 3),
            recorded_by=str(recorded_by),
            note=str(note),
        )

    @classmethod
    def from_dict(cls, raw: dict) -> "CellEvent":
        resume = raw.get("expected_resume_ts")
        return cls.create(
            seq=int(raw.get("seq", 0)),
            cell_id=str(raw.get("cell_id", "")),
            kind=str(raw.get("kind", "")),
            ts=float(raw.get("ts", 0.0)),
            rack_id=str(raw.get("rack_id", "")),
            evidence=EvidenceRef.from_dict(raw.get("evidence", {}) if isinstance(raw.get("evidence"), dict) else {}),
            expected_resume_ts=None if resume is None else float(resume),
            recorded_ts=float(raw.get("recorded_ts", 0.0)),
            recorded_by=str(raw.get("recorded_by", "")),
            note=str(raw.get("note", "")),
        )

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "cell_id": self.cell_id,
            "kind": self.kind,
            "rack_id": self.rack_id,
            "ts": self.ts,
            "evidence": self.evidence.as_dict(),
            "expected_resume_ts": self.expected_resume_ts,
            "recorded_ts": self.recorded_ts,
            "recorded_by": self.recorded_by,
            "note": self.note,
        }


def parse_ts(value: float | str | int) -> float:
    """把 Unix 秒或 ``YYYY-MM-DD`` 日期串折算成 Unix 秒（UTC 当日零点）。"""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 3)
    text = str(value).strip()
    if not text:
        raise StoreError("时间不能为空", component="warranty")
    try:
        if len(text) == 10:
            parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
    except ValueError as error:
        raise StoreError(f"无法解析时间: {value!r}", component="warranty") from error
    return round(parsed.timestamp(), 3)


def iso_date(ts: float) -> str:
    """把 Unix 秒格式化成 UTC 日期串。"""

    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def validate_sequence(events: list[CellEvent]) -> list[str]:
    """检查单只单体的履历先后是否自洽，返回问题代码列表（不抛异常）。"""

    problems: list[str] = []
    ordered = sorted(events, key=lambda event: (event.ts, event.seq))
    kinds = [event.kind for event in ordered]
    if DELIVERED not in kinds:
        problems.append("missing_delivery")
    if kinds and kinds[0] != DELIVERED:
        problems.append("delivery_not_first")
    commissioned_seen = False
    in_maintenance = False
    for event in ordered:
        if event.kind == COMMISSIONED:
            if commissioned_seen:
                problems.append("duplicate_commissioning")
            commissioned_seen = True
    for earlier, later in zip(ordered, ordered[1:]):
        if later.ts < earlier.ts:
            problems.append("ts_out_of_order")
        if earlier.kind == DECOMMISSIONED:
            problems.append("event_after_decommission")
    # 按时间走一遍状态机：复装必须有未闭合的入厂，入厂不能重复开张。
    for event in ordered:
        if event.kind == MAINTENANCE_IN:
            if in_maintenance:
                problems.append("maintenance_in_without_resume")
            in_maintenance = True
        elif event.kind == MAINTENANCE_OUT:
            if not in_maintenance:
                problems.append("maintenance_out_without_in")
            in_maintenance = False
        elif event.kind == DECOMMISSIONED:
            in_maintenance = False
    return sorted(set(problems))
