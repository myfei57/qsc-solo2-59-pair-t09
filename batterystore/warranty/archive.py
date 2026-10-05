"""结论报告的只追加归档（WORM）。

每份报告落成一个内容寻址、永不改写的文件：文件名含版本号与内容摘要，
归档只提供"追加新版本"和"读取既有版本"两个入口，没有更新或删除路径。
全部出版动作另走一条追加式索引链（前一条摘要进后一条），任何对已出版
材料的事后改动，都会在索引链重算或文件摘要比对时暴露。新版本必须显式
声明它取代的旧版本，形成只能加长、不能回改的版本线。
"""

from __future__ import annotations

import os
import threading

from ..errors import StoreError
from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal
from .hashing import canonical_digest
from .report import body_digest_of

REPORT_JOURNAL = "warranty_reports"
REPORT_PREFIX = "warranty_report_"


class ClaimArchive:
    """单体责任结论的只追加归档。"""

    def __init__(self, store: JsonStore, segment_limit: int = 256) -> None:
        self._store = store
        self._journal = JsonlJournal(store.root, REPORT_JOURNAL, segment_limit=segment_limit)
        self._lock = threading.RLock()

    def _path_for(self, cell_id: str, version: int, digest: str) -> str:
        safe = cell_id.replace(os.sep, "_").replace("/", "_")
        return f"{REPORT_PREFIX}{safe}_v{int(version):03d}_{digest[:12]}"

    def _existing_versions(self, cell_id: str) -> list[dict]:
        return [
            item["record"]
            for item in self._journal.read_all_indexed()
            if isinstance(item["record"], dict) and item["record"].get("cell_id") == cell_id
        ]

    def next_version(self, cell_id: str) -> int:
        """返回该单体下一版本号（没有已出版报告时为 1）。"""

        with self._lock:
            versions = self._existing_versions(cell_id)
            if not versions:
                return 1
            return max(int(item.get("version", 0)) for item in versions) + 1

    def latest(self, cell_id: str) -> dict | None:
        """返回某只单体最新版本的索引项，没有则为 ``None``。"""

        with self._lock:
            versions = self._existing_versions(cell_id)
        return max(versions, key=lambda item: int(item.get("version", 0)), default=None)

    def versions(self, cell_id: str) -> list[dict]:
        """按版本升序返回某只单体的全部出版索引项。"""

        with self._lock:
            versions = self._existing_versions(cell_id)
        return sorted(versions, key=lambda item: int(item.get("version", 0)))

    def publish(self, report: dict) -> dict:
        """把一份报告作为新版本落盘并追加索引，返回索引项。

        版本号与取代关系以归档现状为准；目标报告文件若已存在则拒绝，避免
        任何覆盖语义。
        """

        cell_id = str(report["cell_id"])
        version = int(report["version"])
        digest = str(report["body_digest"])
        doc_id = self._path_for(cell_id, version, digest)
        with self._lock:
            known = {int(item["version"]) for item in self._existing_versions(cell_id)}
            if version in known:
                raise StoreError(f"单体 {cell_id} 的 v{version} 已出版，不能覆盖", component="warranty")
            expected_version = (max(known) + 1) if known else 1
            if version != expected_version:
                raise StoreError(f"单体 {cell_id} 的新版本号必须连续追加", component="warranty")
            if self._store.exists(doc_id):
                raise StoreError(f"报告文件已存在，拒绝覆盖: {doc_id}", component="warranty")
            chain = self._journal.read_all_indexed()
            previous = chain[-1]["record"]["index_digest"] if chain else ""
            index_item = {
                "report_id": report["report_id"],
                "doc_id": doc_id,
                "cell_id": cell_id,
                "version": version,
                "supersedes": report.get("supersedes", ""),
                "claim_ref": report.get("claim_ref", ""),
                "issued_ts": report["issued_ts"],
                "issued_by": report.get("issued_by", ""),
                "body_digest": digest,
                "previous_index_digest": previous,
            }
            index_item["index_digest"] = canonical_digest(
                {k: v for k, v in index_item.items() if k != "index_digest"}
            )
            self._store.write(doc_id, report)
            self._journal.append(index_item)
        return index_item

    def load(self, cell_id: str, version: int) -> dict:
        """读取某只单体的指定版本报告，并复核文件内容摘要。"""

        with self._lock:
            versions = self._existing_versions(cell_id)
        match = next((item for item in versions if int(item.get("version", 0)) == int(version)), None)
        if match is None:
            raise StoreError(f"单体 {cell_id} 没有 v{version} 报告", component="warranty")
        report = self._store.read(str(match["doc_id"]))
        if not isinstance(report, dict):
            raise StoreError(f"报告文件损坏: {match['doc_id']}", component="warranty")
        if body_digest_of(report) != str(match["body_digest"]):
            raise StoreError(f"报告内容摘要与索引不符，疑似被改动: {match['doc_id']}", component="warranty")
        return report

    def load_latest(self, cell_id: str) -> dict:
        latest = self.latest(cell_id)
        if latest is None:
            raise StoreError(f"单体 {cell_id} 尚无已出版结论", component="warranty")
        return self.load(cell_id, int(latest["version"]))

    def verify(self) -> dict:
        """重算索引哈希链并逐个复核已出版报告文件摘要。"""

        with self._lock:
            indexed = self._journal.read_all_indexed()
        breaks: list[str] = []
        tampered: list[str] = []
        for item in indexed:
            record = item["record"]
            expected = canonical_digest({k: v for k, v in record.items() if k != "index_digest"})
            if record.get("index_digest") != expected:
                breaks.append(str(record.get("report_id", item["seq"])))
            doc_id = str(record.get("doc_id", ""))
            payload = self._store.read(doc_id)
            if not isinstance(payload, dict) or body_digest_of(payload) != record.get("body_digest"):
                tampered.append(doc_id or str(record.get("report_id", "")))
        return {
            "published": len(indexed),
            "chain_breaks": breaks,
            "tampered_reports": tampered,
            "ok": not breaks and not tampered,
        }
