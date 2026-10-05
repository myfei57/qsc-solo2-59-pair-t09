"""单体台账事件。

索赔证据链的最小单元是"事件"，只有六类，全部围绕一只单体的真实履历：

``delivered``     交付（责任期按交付日期切分的锚点）；
``commissioned``  并网投运（责任期按并网日期切分的锚点）；
``decommissioned`` 因检修或搬迁离网停运；
``recommissioned`` 检修或搬迁完成后重新并网投运；
``relocated``     搬迁并重新就位（携带迁入簇标识，跨簇流转靠它拼接）；
``runtime``       一段实际运行证据（并网时长与充放电量，缺段检测以它为准）。

事件一旦追加进台账就不再改写；每条事件带显式序号 ``seq`` 与前一条事件的
SHA-256 ``prev_hash``，任何事后插入、删除或修改都会在
:func:`batterystore.warranty.ledger.verify_event_chain` 里暴露。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..errors import WarrantyError

LIFECYCLE_ACTIONS = (
    "delivered",
    "commissioned",
    "decommissioned",
    "recommissioned",
    "relocated",
)
RUNTIME_ACTION = "runtime"
EVENT_ACTIONS = LIFECYCLE_ACTIONS + (RUNTIME_ACTION,)
GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class CellEvent:
    """一只单体台账里的一条履历事件。"""

    seq: int
    cell_id: str
    action: str
    ts: float
    payload: dict = field(default_factory=dict)
    source: str = ""
    prev_hash: str = GENESIS_HASH
    event_hash: str = ""

    @classmethod
    def create(
        cls,
        seq: int,
        cell_id: str,
        action: str,
        ts: float,
        payload: dict | None = None,
        source: str = "",
        prev_hash: str = GENESIS_HASH,
    ) -> "CellEvent":
        """按入参构造并校验一条事件，当场算出自身哈希。"""

        normalized = normalize_payload(action, payload or {})
        event = cls(
            seq=int(seq),
            cell_id=str(cell_id),
            action=action,
            ts=round(float(ts), 3),
            payload=normalized,
            source=str(source or "manual"),
            prev_hash=str(prev_hash),
        )
        object.__setattr__(event, "event_hash", digest_event(event))
        return event

    @classmethod
    def from_dict(cls, raw: dict) -> "CellEvent":
        """从落盘字段还原一条事件（不重新计算哈希，供防篡改比对）。"""

        return cls(
            seq=int(raw.get("seq", 0)),
            cell_id=str(raw.get("cell_id", "")),
            action=str(raw.get("action", "")),
            ts=round(float(raw.get("ts", 0.0)), 3),
            payload=dict(raw.get("payload", {})),
            source=str(raw.get("source", "")),
            prev_hash=str(raw.get("prev_hash", GENESIS_HASH)),
            event_hash=str(raw.get("event_hash", "")),
        )

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "cell_id": self.cell_id,
            "action": self.action,
            "ts": self.ts,
            "payload": dict(self.payload),
            "source": self.source,
            "prev_hash": self.prev_hash,
            "event_hash": self.event_hash,
        }

    def is_lifecycle(self) -> bool:
        """是否属于履历锚点事件（相对运行证据而言）。"""

        return self.action in LIFECYCLE_ACTIONS


def validate_cell_id(cell_id: str) -> str:
    """单体标识必须是非空字符串。"""

    if not isinstance(cell_id, str) or not cell_id.strip():
        raise WarrantyError("单体标识不能为空", component="warranty")
    return cell_id.strip()


def normalize_payload(action: str, payload: dict) -> dict:
    """按事件类型校验并规范化附属字段。

    时间戳类字段统一保留三位小数；搬迁必须给出迁入簇；运行证据必须给出正的
    并网时长，充放电量缺省按 0 计。校验在这里一次做完，台账与时间线不再重复
    判断接线错误。
    """

    if action not in EVENT_ACTIONS:
        raise WarrantyError(f"未知的单体履历事件类型: {action}", component="warranty")
    data = dict(payload)

    def number(key: str, default: float | None = None, minimum: float | None = None) -> float:
        raw = data.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise WarrantyError(f"事件 {action} 的字段 {key} 必须是数值", component="warranty")
        value = round(float(raw), 3)
        if minimum is not None and value < minimum:
            raise WarrantyError(f"事件 {action} 的字段 {key} 不能小于 {minimum}", component="warranty")
        return value

    result: dict = {}
    if action in ("commissioned", "recommissioned"):
        result["rack_id"] = str(data.get("rack_id", "")).strip()
        if not result["rack_id"]:
            raise WarrantyError(f"事件 {action} 必须携带 rack_id", component="warranty")
    if action == "decommissioned":
        reason = str(data.get("reason", "")).strip()
        if reason not in ("maintenance", "relocation"):
            raise WarrantyError("事件 decommissioned 的 reason 必须是 maintenance 或 relocation", component="warranty")
        result["reason"] = reason
    if action == "relocated":
        result["to_rack"] = str(data.get("to_rack", "")).strip()
        if not result["to_rack"]:
            raise WarrantyError("事件 relocated 必须携带 to_rack", component="warranty")
    if action == RUNTIME_ACTION:
        result["grid_seconds"] = number("grid_seconds", minimum=0.0)
        result["charge_kwh"] = number("charge_kwh", default=0.0, minimum=0.0)
        result["discharge_kwh"] = number("discharge_kwh", default=0.0, minimum=0.0)
        result["rack_id"] = str(data.get("rack_id", "")).strip()
        if result["grid_seconds"] <= 0.0:
            raise WarrantyError("运行证据的并网时长必须为正", component="warranty")
    return result


def canonical_event_body(event: CellEvent) -> bytes:
    """事件参与哈希的规范化字节（不含哈希字段本身）。"""

    body = {
        "seq": event.seq,
        "cell_id": event.cell_id,
        "action": event.action,
        "ts": event.ts,
        "payload": event.payload,
        "source": event.source,
        "prev_hash": event.prev_hash,
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_event(event: CellEvent) -> str:
    """计算一条事件的 SHA-256 摘要。"""

    return hashlib.sha256(canonical_event_body(event)).hexdigest()


def fingerprint_events(events: list[CellEvent]) -> dict:
    """给一段事件序列算指纹，供索赔材料固化后比对台账是否被后续追加改写。"""

    joined = "\n".join(f"{event.seq}:{event.event_hash}" for event in events)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return {"count": len(events), "last_seq": events[-1].seq if events else 0, "digest": digest}
