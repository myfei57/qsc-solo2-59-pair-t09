"""单体履历台账（只追加、带哈希链）。

每只单体一份独立 JSONL 流水（``cellevents-<slug>.jsonl``，分段滚动），事件按
追加顺序获得连续 ``seq``，每条事件的 ``prev_hash`` 指向前一条。这样三种篡改
都会暴露：改写某条字段会破坏它自己的 ``event_hash``；删一条会断链；在中间
插入一条会让后续序号与前向哈希对不上。

台账只负责"记"和"验"，不解释动作先后是否合理——那是
:mod:`batterystore.warranty.timeline` 的职责。
"""

from __future__ import annotations

import os
import re
import threading

from ..errors import WarrantyError
from ..store.journal import JsonlJournal
from .events import GENESIS_HASH, CellEvent, digest_event, validate_cell_id

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def cell_slug(cell_id: str) -> str:
    """把单体标识转成安全的流水文件名片段。"""

    slug = _SLUG_RE.sub("_", validate_cell_id(cell_id)).strip("._-")
    if not slug:
        raise WarrantyError(f"单体标识 {cell_id!r} 无法生成台账文件名", component="warranty")
    return slug


class CellEventLedger:
    """全部单体的履历台账：每只单体一条只追加流水。"""

    def __init__(self, store_root: str, segment_limit: int = 512) -> None:
        self._root = store_root
        self._segment_limit = max(1, segment_limit)
        self._journals: dict[str, JsonlJournal] = {}
        self._lock = threading.RLock()

    def _journal(self, cell_id: str) -> JsonlJournal:
        key = validate_cell_id(cell_id)
        journal = self._journals.get(key)
        if journal is None:
            journal = JsonlJournal(self._root, f"cellevents-{cell_slug(key)}", segment_limit=self._segment_limit)
            self._journals[key] = journal
        return journal

    def record(
        self,
        cell_id: str,
        action: str,
        ts: float,
        payload: dict | None = None,
        source: str = "manual",
    ) -> CellEvent:
        """追加一条履历事件，返回落盘后的事件对象。

        序号与前向哈希在持锁状态内从流水尾部推出，因此并发追加不会产生重号
        或断链。链本身已经断掉时（盘上文件被人动过）直接拒绝再写，避免在被
        污染的证据上继续追加。
        """

        cell_id = validate_cell_id(cell_id)
        with self._lock:
            verification = self.verify_cell(cell_id)
            if not verification["ok"]:
                raise WarrantyError(
                    f"单体 {cell_id} 的证据链已损坏，拒绝继续追加: {verification['errors']}",
                    component="warranty",
                )
            events = self.list_events(cell_id)
            seq = (events[-1].seq + 1) if events else 1
            prev_hash = events[-1].event_hash if events else GENESIS_HASH
            event = CellEvent.create(seq, cell_id, action, ts, payload=payload, source=source, prev_hash=prev_hash)
            self._journal(cell_id).append(event.as_dict())
            return event

    def list_events(self, cell_id: str) -> list[CellEvent]:
        """按序号读出一只单体的全部事件（追加顺序即时间证据顺序）。"""

        with self._lock:
            raw = self._journal(cell_id).read_all()
        return [CellEvent.from_dict(item) for item in raw]

    def known_cells(self) -> list[str]:
        """列出盘上已有台账流水的单体标识。"""

        prefix = "cellevents-"
        suffix = "-00000.jsonl"
        cells: list[str] = []
        seen: set[str] = set()
        with self._lock:
            for journal in self._journals.values():
                if journal.count() > 0 and journal.topic not in seen:
                    seen.add(journal.topic)
                    cells.append(journal.topic[len(prefix) :])
            if os.path.isdir(self._root):
                for name in os.listdir(self._root):
                    if name.startswith(prefix) and name.endswith(suffix):
                        key = name[: -len(suffix)][len(prefix) :]
                        if key not in seen:
                            seen.add(key)
                            cells.append(key)
        return sorted(cells)

    def verify_cell(self, cell_id: str) -> dict:
        """重放并逐链核对一只单体的台账，返回逐节点校验结果。"""

        cell_id = validate_cell_id(cell_id)
        with self._lock:
            try:
                raw = self._journal(cell_id).read_all()
            except Exception as error:  # 流水段损坏（JSON 解析失败等）
                return {"cell_id": cell_id, "ok": False, "checked": 0, "errors": [f"流水无法读取: {error}"]}
        events = [CellEvent.from_dict(item) for item in raw]
        errors: list[str] = []
        expected_seq = 1
        expected_prev = GENESIS_HASH
        for event in events:
            if event.seq != expected_seq:
                errors.append(f"seq 不连续: 期望 {expected_seq}，实际 {event.seq}")
            if event.prev_hash != expected_prev:
                errors.append(f"事件 {event.seq} 前向哈希断链")
            if not event.event_hash:
                errors.append(f"事件 {event.seq} 缺少摘要")
            elif digest_event(event) != event.event_hash:
                errors.append(f"事件 {event.seq} 摘要不匹配，字段被改写")
            expected_seq = event.seq + 1
            expected_prev = event.event_hash or expected_prev
        return {"cell_id": cell_id, "ok": not errors, "checked": len(events), "errors": errors}

    def verify_all(self) -> list[dict]:
        """逐只核对全部台账。"""

        return [self.verify_cell(cell_id) for cell_id in self.known_cells()]
