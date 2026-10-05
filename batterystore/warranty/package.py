"""索赔材料包。

一只单体一份"责任期归属结论 + 运行证据链"材料包。材料包一旦生成就把当时
台账上的全部事件连同时间线、归属结论一起固化进版本记录：版本之间用
``prev_version_hash`` 串成链，每个版本自带 ``input_fingerprint``（输入事件
序列指纹）。后续哪怕台账又追加了新事件，老版本的字节也不会变——复核人拿
到老版本重算哈希，结果必然一致；新版本只允许追加，并通过指纹差异显式说明
"相对上一版多看到了哪些事件"。
"""

from __future__ import annotations

import hashlib
import json

from ..clock import Clock
from .events import CellEvent, fingerprint_events
from .liability import attribute_liability
from .timeline import DEFAULT_GAP_THRESHOLD_S, build_cell_timeline

PACKAGE_KIND = "cell_warranty_claim"
VERSION_INITIAL = 1
GENESIS_VERSION_HASH = "0" * 64


def build_claim_package(
    cell_id: str,
    events: list[CellEvent],
    *,
    claim_ts: float,
    warranty_term_s: float,
    preferred_basis: str | None = None,
    as_of: float | None = None,
    gap_threshold_s: float = DEFAULT_GAP_THRESHOLD_S,
) -> dict:
    """在不触碰台账的前提下，把一只单体的证据组装成材料包主体。"""

    timeline = build_cell_timeline(
        events,
        # 材料包面向"索赔时点"复核：时间线在索赔时点收口，索赔时点之后才
        # 出现的空当不属于本次责任认定，开口缺段也只在索赔时点之前成立。
        as_of=as_of if as_of is not None else claim_ts,
        gap_threshold_s=gap_threshold_s,
    )
    timeline["cell_id"] = cell_id
    conclusion = attribute_liability(
        timeline,
        claim_ts=claim_ts,
        warranty_term_s=warranty_term_s,
        preferred_basis=preferred_basis,
    )
    conclusion["cell_id"] = cell_id
    return {
        "kind": PACKAGE_KIND,
        "cell_id": cell_id,
        "claim_ts": round(float(claim_ts), 3),
        "as_of": round(float(as_of), 3) if as_of is not None else None,
        "warranty_term_s": float(warranty_term_s),
        "preferred_basis": preferred_basis,
        "gap_threshold_s": float(gap_threshold_s),
        "input_fingerprint": fingerprint_events(events),
        "events": [event.as_dict() for event in events],
        "timeline": timeline,
        "conclusion": conclusion,
    }


def canonical_package_body(package: dict, version: int, issued_at: str, prev_version_hash: str, issued_by: str = "", note: str = "") -> bytes:
    """版本记录参与哈希的规范化字节（版本元数据在内，哈希字段在外）。"""

    body = {
        "kind": package["kind"],
        "cell_id": package["cell_id"],
        "version": int(version),
        "issued_at": issued_at,
        "issued_by": issued_by,
        "note": note,
        "prev_version_hash": prev_version_hash,
        "package": package,
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_version(
    package: dict,
    version: int,
    issued_at: str,
    prev_version_hash: str,
    issued_by: str = "",
    note: str = "",
) -> str:
    """计算一条版本记录的 SHA-256 摘要。"""

    return hashlib.sha256(
        canonical_package_body(package, version, issued_at, prev_version_hash, issued_by, note)
    ).hexdigest()


def now_iso(clock: Clock) -> str:
    """材料签发时间（UTC ISO）。"""

    return clock.utc_iso()
