"""单体簇归属与状态分段。

履历事件是离散节点，复核需要的是连续区间：每段时间单体到底在不在簇里、
在哪一簇、是否在实际运行。这里把事件流展开成「待并网库存 / 在簇运行 /
检修停用 / 退役」四类区间，检修区间还携带预计复装时间，超出部分单独标记。
同一只单体跨簇流转的轨迹因此能被逐段读出来，而不是只看到最后一簇。
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import (
    COMMISSIONED,
    DECOMMISSIONED,
    DELIVERED,
    MAINTENANCE_IN,
    MAINTENANCE_OUT,
    RELOCATED,
    CellEvent,
)

STORAGE = "storage"
RUNNING = "running"
MAINTENANCE = "maintenance"
RETIRED = "retired"

STATE_LABELS = {
    STORAGE: "到货待并网",
    RUNNING: "在簇运行",
    MAINTENANCE: "检修停用",
    RETIRED: "已退役",
}

# 事件之后单体进入的状态。
_NEXT_STATE = {
    DELIVERED: STORAGE,
    COMMISSIONED: RUNNING,
    MAINTENANCE_IN: MAINTENANCE,
    MAINTENANCE_OUT: RUNNING,
    RELOCATED: RUNNING,
    DECOMMISSIONED: RETIRED,
}


@dataclass(frozen=True)
class MembershipSegment:
    """一段连续的单体状态。"""

    state: str
    start_ts: float
    end_ts: float | None
    rack_id: str
    start_kind: str
    start_seq: int
    end_kind: str = ""
    end_seq: int | None = None
    expected_resume_ts: float | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, self.state),
            "rack_id": self.rack_id,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "start_kind": self.start_kind,
            "start_seq": self.start_seq,
            "end_kind": self.end_kind,
            "end_seq": self.end_seq,
            "expected_resume_ts": self.expected_resume_ts,
        }


def build_membership(events: list[CellEvent]) -> list[MembershipSegment]:
    """把履历事件展开成连续状态区间。"""

    segments: list[MembershipSegment] = []
    current: MembershipSegment | None = None
    for event in sorted(events, key=lambda item: (item.ts, item.seq)):
        if current is not None:
            segments.append(
                MembershipSegment(
                    state=current.state,
                    start_ts=current.start_ts,
                    end_ts=event.ts,
                    rack_id=current.rack_id,
                    start_kind=current.start_kind,
                    start_seq=current.start_seq,
                    end_kind=event.kind,
                    end_seq=event.seq,
                    expected_resume_ts=current.expected_resume_ts,
                )
            )
        state = _NEXT_STATE[event.kind]
        current = MembershipSegment(
            state=state,
            start_ts=event.ts,
            end_ts=None,
            rack_id=event.rack_id,
            start_kind=event.kind,
            start_seq=event.seq,
            expected_resume_ts=event.expected_resume_ts if event.kind == MAINTENANCE_IN else None,
        )
    if current is not None:
        segments.append(current)
    return segments


def running_intervals(segments: list[MembershipSegment], until_ts: float | None = None) -> list[tuple[float, float]]:
    """返回全部在簇运行区间（开区间），可用 ``until_ts`` 截断末段。"""

    intervals: list[tuple[float, float]] = []
    for segment in segments:
        if segment.state != RUNNING:
            continue
        end = segment.end_ts if segment.end_ts is not None else until_ts
        if end is None:
            continue
        if until_ts is not None:
            end = min(end, until_ts)
        if end > segment.start_ts:
            intervals.append((segment.start_ts, end))
    return intervals


def maintenance_intervals(
    segments: list[MembershipSegment], until_ts: float | None = None
) -> list[tuple[float, float]]:
    """返回全部检修停用区间，未复装的用 ``until_ts`` 收口。"""

    intervals: list[tuple[float, float]] = []
    for segment in segments:
        if segment.state != MAINTENANCE:
            continue
        end = segment.end_ts if segment.end_ts is not None else until_ts
        if end is None:
            continue
        if until_ts is not None:
            end = min(end, until_ts)
        if end > segment.start_ts:
            intervals.append((segment.start_ts, end))
    return intervals
