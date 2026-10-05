"""追加式单体台账与簇级运行证据。

台账是责任归属的唯一事实来源，分两条只追加流水：

* 单体履历流水（交付、并网、检修、流转、退役）；
* 簇运行证据流水（并网、充电、放电、均衡等实际运行动作）。

每条落盘记录都带全局序号与前一条记录的 SHA-256 摘要，形成防篡改哈希链。
重启后从盘上回放重建链，任何事后改动都会让链校验失败——这是"材料生成后
不能再被后续数据改动"的基础：结论里嵌入的每条证据都能回到这里逐条复核。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..errors import StoreError
from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal
from .events import (
    CELL_KINDS,
    CellEvent,
    EvidenceRef,
)
from .hashing import canonical_digest

CELL_JOURNAL = "warranty_cells"
RACK_OP_JOURNAL = "warranty_rack_ops"

# 簇级动作 → 证据链上展示给复核人的中文说明。
RACK_OP_LABELS = {
    "grid_tie": "并网运行",
    "charge": "充电运行",
    "discharge": "放电运行",
    "balance": "均衡运行",
}


@dataclass(frozen=True)
class LedgerEntry:
    """哈希链上的一个信封：序号、录入时间、载荷与摘要。"""

    seq: int
    recorded_ts: float
    payload: dict
    digest: str

    @classmethod
    def from_dict(cls, raw: dict) -> "LedgerEntry":
        return cls(
            seq=int(raw.get("seq", -1)),
            recorded_ts=float(raw.get("recorded_ts", 0.0)),
            payload=dict(raw.get("payload", {})),
            digest=str(raw.get("digest", "")),
        )


@dataclass(frozen=True)
class RackOpEvidence:
    """一条簇级实际运行证据（单体在簇期间发生的运行动作）。"""

    seq: int
    ts: float
    rack_id: str
    op_id: str
    action: str
    ok: bool
    recorded_ts: float = 0.0
    note: str = ""

    @classmethod
    def create(
        cls,
        seq: int,
        ts: float,
        rack_id: str,
        op_id: str,
        action: str,
        ok: bool,
        *,
        recorded_ts: float = 0.0,
        note: str = "",
    ) -> "RackOpEvidence":
        if not str(rack_id).strip():
            raise StoreError("簇运行证据必须指明簇标识", component="warranty")
        if action not in RACK_OP_LABELS:
            raise StoreError(f"未知的簇运行动作: {action}", component="warranty")
        return cls(
            seq=int(seq),
            ts=round(float(ts), 3),
            rack_id=str(rack_id).strip(),
            op_id=str(op_id),
            action=action,
            ok=bool(ok),
            recorded_ts=round(float(recorded_ts), 3),
            note=str(note),
        )

    @classmethod
    def from_entry(cls, entry: LedgerEntry) -> "RackOpEvidence":
        payload = entry.payload
        return cls.create(
            seq=entry.seq,
            ts=float(payload.get("ts", 0.0)),
            rack_id=str(payload.get("rack_id", "")),
            op_id=str(payload.get("op_id", "")),
            action=str(payload.get("action", "")),
            ok=bool(payload.get("ok", False)),
            recorded_ts=entry.recorded_ts,
            note=str(payload.get("note", "")),
        )

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "rack_id": self.rack_id,
            "op_id": self.op_id,
            "action": self.action,
            "action_label": RACK_OP_LABELS.get(self.action, self.action),
            "ok": self.ok,
            "recorded_ts": self.recorded_ts,
            "note": self.note,
        }


def _digest_of(previous: str, payload: dict, recorded_ts: float) -> str:
    return canonical_digest(
        {"previous": previous, "recorded_ts": round(recorded_ts, 3), "payload": payload}
    )


class HashedJournal:
    """在 :class:`JsonlJournal` 之上维护序号与 SHA-256 哈希链。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal
        self._lock = threading.RLock()
        self._entries: list[LedgerEntry] = []
        self._rebuild()

    @property
    def topic(self) -> str:
        return self._journal.topic

    def _rebuild(self) -> None:
        for item in self._journal.read_all_indexed():
            raw = item["record"]
            if isinstance(raw, dict) and "payload" in raw:
                self._entries.append(LedgerEntry.from_dict(raw))
            else:
                # 兼容不带信封的裸记录：按其序号补一个空摘要占位。
                self._entries.append(
                    LedgerEntry(
                        seq=item["seq"],
                        recorded_ts=float(raw.get("recorded_ts", 0.0)) if isinstance(raw, dict) else 0.0,
                        payload=raw if isinstance(raw, dict) else {"value": raw},
                        digest="",
                    )
                )

    def append(self, payload: dict, recorded_ts: float) -> LedgerEntry:
        """追加一条载荷，自动编号并接上哈希链。"""

        with self._lock:
            seq = len(self._entries)
            previous = self._entries[-1].digest if self._entries else ""
            digest = _digest_of(previous, payload, recorded_ts)
            envelope = {"seq": seq, "recorded_ts": round(recorded_ts, 3), "payload": payload, "digest": digest}
            self._journal.append(envelope)
            entry = LedgerEntry(seq=seq, recorded_ts=round(recorded_ts, 3), payload=dict(payload), digest=digest)
            self._entries.append(entry)
            return entry

    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)

    def locate(self, seq: int) -> dict | None:
        """返回某序号的物理定位（段号、行号、文件路径）。"""

        return self._journal.locate(seq)

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def verify(self) -> dict:
        """从盘上重读全部信封并重算哈希链，返回逐条核对结果与断点清单。

        刻意不走内存副本：复核可能发生在重启后的新进程，且必须能发现
        绕过服务直接改盘的行为。
        """

        entries: list[LedgerEntry] = []
        for item in self._journal.read_all_indexed():
            raw = item["record"]
            if isinstance(raw, dict) and "payload" in raw:
                entries.append(LedgerEntry.from_dict(raw))
        breaks: list[int] = []
        previous = ""
        for entry in entries:
            expected = _digest_of(previous, entry.payload, entry.recorded_ts)
            if entry.digest != expected:
                breaks.append(entry.seq)
            previous = entry.digest
        return {
            "topic": self.topic,
            "count": len(entries),
            "breaks": breaks,
            "ok": not breaks,
        }


class CellLedger:
    """单体履历与簇运行证据的追加式台账。"""

    def __init__(self, store: JsonStore, segment_limit: int = 512) -> None:
        self._cells = HashedJournal(JsonlJournal(store.root, CELL_JOURNAL, segment_limit=segment_limit))
        self._rack_ops = HashedJournal(JsonlJournal(store.root, RACK_OP_JOURNAL, segment_limit=segment_limit))

    @property
    def cell_journal(self) -> HashedJournal:
        return self._cells

    @property
    def rack_op_journal(self) -> HashedJournal:
        return self._rack_ops

    def record_event(
        self,
        cell_id: str,
        kind: str,
        ts: float,
        rack_id: str,
        evidence: EvidenceRef,
        *,
        recorded_ts: float,
        recorded_by: str = "",
        expected_resume_ts: float | None = None,
        note: str = "",
    ) -> CellEvent:
        """追加一条单体履历事件。"""

        seq = self._cells.count()
        event = CellEvent.create(
            seq,
            cell_id,
            kind,
            ts,
            rack_id,
            evidence,
            expected_resume_ts=expected_resume_ts,
            recorded_ts=recorded_ts,
            recorded_by=recorded_by,
            note=note,
        )
        self._cells.append(event.as_dict(), recorded_ts)
        return event

    def record_rack_op(
        self,
        *,
        ts: float,
        rack_id: str,
        op_id: str,
        action: str,
        ok: bool,
        recorded_ts: float,
        note: str = "",
    ) -> RackOpEvidence:
        """追加一条簇级实际运行证据。"""

        seq = self._rack_ops.count()
        evidence = RackOpEvidence.create(
            seq, ts, rack_id, op_id, action, ok, recorded_ts=recorded_ts, note=note
        )
        self._rack_ops.append(
            {key: value for key, value in evidence.as_dict().items() if key != "action_label"},
            recorded_ts,
        )
        return evidence

    def events_of(self, cell_id: str) -> list[CellEvent]:
        """按时间顺序返回某只单体的全部履历事件。"""

        events = [
            CellEvent.from_dict(entry.payload)
            for entry in self._cells.entries()
            if entry.payload.get("cell_id") == cell_id and entry.payload.get("kind") in CELL_KINDS
        ]
        return sorted(events, key=lambda event: (event.ts, event.seq))

    def all_cell_ids(self) -> list[str]:
        """返回台账中出现过的全部单体标识。"""

        return sorted({entry.payload.get("cell_id") for entry in self._cells.entries() if entry.payload.get("cell_id")})

    def rack_ops(self) -> list[RackOpEvidence]:
        """按时间顺序返回全部簇运行证据。"""

        ops = [RackOpEvidence.from_entry(entry) for entry in self._rack_ops.entries()]
        return sorted(ops, key=lambda op: (op.ts, op.seq))

    def verify(self) -> dict:
        """核对两条哈希链的完整性。"""

        cells = self._cells.verify()
        ops = self._rack_ops.verify()
        return {"cells": cells, "rack_ops": ops, "ok": cells["ok"] and ops["ok"]}
