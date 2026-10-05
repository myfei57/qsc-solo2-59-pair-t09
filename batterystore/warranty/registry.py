"""索赔材料登记处（只追加版本）。

材料包按单体落 ``claim-<slug>`` JSONL 流水：首版由 :meth:`ClaimRegistry.issue`
签发，之后只能用 :meth:`ClaimRegistry.review` 追加复核版本。新版本可以引用
当时台账上更多的事件（例如复核时补录了检修单），但：

* 老版本的字节永不重写，复核人重算其 ``version_hash`` 必须一致；
* 每条版本记录的 ``prev_version_hash`` 指向前一版，删掉中间任何一版都会断链；
* 新版本相对上一版的输入指纹差异（新增事件 seq 列表、结论是否翻转）显式写在
  ``review_note`` 里，杜绝"结论悄悄变了但没人知道"。
"""

from __future__ import annotations

import threading

from ..clock import Clock
from ..errors import WarrantyError
from ..store.journal import JsonlJournal
from .ledger import cell_slug
from .package import (
    GENESIS_VERSION_HASH,
    VERSION_INITIAL,
    build_claim_package,
    digest_version,
    now_iso,
)


class ClaimRegistry:
    """全部单体的索赔材料包：每只单体一条只追加版本流水。"""

    def __init__(self, store_root: str, clock: Clock, segment_limit: int = 128) -> None:
        self._root = store_root
        self._clock = clock
        self._segment_limit = max(1, segment_limit)
        self._journals: dict[str, JsonlJournal] = {}
        self._lock = threading.RLock()

    def _journal(self, cell_id: str) -> JsonlJournal:
        slug = cell_slug(cell_id)
        journal = self._journals.get(slug)
        if journal is None:
            journal = JsonlJournal(self._root, f"claim-{slug}", segment_limit=self._segment_limit)
            self._journals[slug] = journal
        return journal

    def issue(
        self,
        cell_id: str,
        events: list,
        *,
        claim_ts: float,
        warranty_term_s: float,
        preferred_basis: str | None = None,
        as_of: float | None = None,
        gap_threshold_s: float | None = None,
        note: str = "",
    ) -> dict:
        """为首只尚未建档的单体签发第一版材料包。"""

        with self._lock:
            existing = self._versions(cell_id)
            if existing:
                raise WarrantyError(f"单体 {cell_id} 已有材料包，追加内容必须走复核版本", component="warranty")
            package = build_claim_package(
                cell_id,
                events,
                claim_ts=claim_ts,
                warranty_term_s=warranty_term_s,
                preferred_basis=preferred_basis,
                as_of=as_of,
                **({"gap_threshold_s": gap_threshold_s} if gap_threshold_s is not None else {}),
            )
            return self._append(cell_id, package, VERSION_INITIAL, GENESIS_VERSION_HASH, note, reviewer="")

    def review(
        self,
        cell_id: str,
        events: list,
        *,
        claim_ts: float,
        warranty_term_s: float,
        reviewer: str,
        preferred_basis: str | None = None,
        as_of: float | None = None,
        gap_threshold_s: float | None = None,
        note: str = "",
    ) -> dict:
        """在已有材料包上追加一版复核材料；复核人必须署名。"""

        if not str(reviewer or "").strip():
            raise WarrantyError("复核版本必须署名 reviewer", component="warranty")
        with self._lock:
            versions = self._versions(cell_id)
            if not versions:
                raise WarrantyError(f"单体 {cell_id} 还没有材料包，首版必须先签发", component="warranty")
            previous = versions[-1]
            package = build_claim_package(
                cell_id,
                events,
                claim_ts=claim_ts,
                warranty_term_s=warranty_term_s,
                preferred_basis=preferred_basis,
                as_of=as_of,
                **({"gap_threshold_s": gap_threshold_s} if gap_threshold_s is not None else {}),
            )
            self._annotate_review(package, previous, note)
            return self._append(
                cell_id, package, int(previous["version"]) + 1, previous["version_hash"], note, reviewer=reviewer
            )

    def versions(self, cell_id: str) -> list[dict]:
        """返回一只单体的全部材料版本（按版本号升序，只读副本）。"""

        with self._lock:
            return [dict(item) for item in self._versions(cell_id)]

    def latest(self, cell_id: str) -> dict | None:
        """返回最新一版材料，没有则返回 ``None``。"""

        versions = self.versions(cell_id)
        return versions[-1] if versions else None

    def known_cells(self) -> list[str]:
        """列出已签发过材料包的单体。"""

        import os

        prefix, suffix = "claim-", "-00000.jsonl"
        found: list[str] = []
        seen: set[str] = set()
        with self._lock:
            for slug, journal in self._journals.items():
                if journal.count() > 0:
                    seen.add(slug)
                    found.append(slug)
            if os.path.isdir(self._root):
                for name in os.listdir(self._root):
                    if name.startswith(prefix) and name.endswith(suffix):
                        slug = name[: -len(suffix)][len(prefix) :]
                        if slug not in seen:
                            seen.add(slug)
                            found.append(slug)
        return sorted(found)

    def verify_chain(self, cell_id: str) -> dict:
        """重放并逐链核对一只单体的材料包版本是否被改过或删过。"""

        with self._lock:
            raw = self._journal(cell_id).read_all()
        errors: list[str] = []
        expected_version = VERSION_INITIAL
        expected_prev = GENESIS_VERSION_HASH
        for record in raw:
            if int(record.get("version", 0)) != expected_version:
                errors.append(f"版本号不连续: 期望 {expected_version}，实际 {record.get('version')}")
            if record.get("prev_version_hash") != expected_prev:
                errors.append(f"版本 {record.get('version')} 的前向版本哈希断链")
            expected = digest_version(
                record["package"],
                int(record.get("version", 0)),
                str(record.get("issued_at", "")),
                str(record.get("prev_version_hash", "")),
                issued_by=str(record.get("issued_by", "")),
                note=str(record.get("note", "")),
            )
            if record.get("version_hash") != expected:
                errors.append(f"版本 {record.get('version')} 内容哈希不匹配，材料被改写")
            expected_prev = str(record.get("version_hash", ""))
            expected_version += 1
        return {"cell_id": cell_id, "ok": not errors, "versions": len(raw), "errors": errors}

    def _versions(self, cell_id: str) -> list[dict]:
        return self._journal(cell_id).read_all()

    def _append(self, cell_id: str, package: dict, version: int, prev_version_hash: str, note: str, *, reviewer: str) -> dict:
        issued_at = now_iso(self._clock)
        version_hash = digest_version(
            package, version, issued_at, prev_version_hash, issued_by=reviewer, note=note
        )
        record = {
            "version": version,
            "cell_id": cell_id,
            "issued_at": issued_at,
            "issued_by": reviewer,
            "note": str(note or ""),
            "prev_version_hash": prev_version_hash,
            "version_hash": version_hash,
            "package": package,
        }
        self._journal(cell_id).append(record)
        return dict(record)

    def _annotate_review(self, package: dict, previous: dict, note: str) -> None:
        prior_fingerprint = dict(previous["package"].get("input_fingerprint", {}))
        current_fingerprint = dict(package["input_fingerprint"])
        prior_seqs = {event["seq"] for event in previous["package"].get("events", [])}
        new_seqs = [event["seq"] for event in package["events"] if event["seq"] not in prior_seqs]
        prior_conclusion = previous["package"]["conclusion"]
        conclusion = package["conclusion"]
        flipped = (
            prior_conclusion.get("selected_basis") != conclusion["selected_basis"]
            or prior_conclusion.get("conclusion") != conclusion["conclusion"]
            or prior_conclusion.get("liable_party") != conclusion["liable_party"]
        )
        package["review_of_version"] = int(previous["version"])
        package["review_note"] = {
            "note": str(note or ""),
            "previous_fingerprint": prior_fingerprint,
            "current_fingerprint": current_fingerprint,
            "new_event_seqs": new_seqs,
            "events_added": len(new_seqs),
            "conclusion_changed": flipped,
            "previous_conclusion": {
                "selected_basis": prior_conclusion.get("selected_basis"),
                "conclusion": prior_conclusion.get("conclusion"),
                "liable_party": prior_conclusion.get("liable_party"),
            },
        }
