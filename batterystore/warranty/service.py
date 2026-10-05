"""单体责任服务：对外编排入口。

平台、控制台与命令行只跟这一层打交道：录入履历（含整包检修的批量展开）、
接收簇运行证据、按索赔时点出版不可改结论、追加复核新版本、核验全部只追加
材料。结论出版时嵌入台账完整性自检，链路哈希对不上会显式写进报告，而不是
悄悄带过。
"""

from __future__ import annotations

from ..clock import Clock
from ..errors import StoreError
from ..store.jsonfile import JsonStore
from .archive import ClaimArchive
from .chain import DEFAULT_MAX_GAP_S, build_chain
from .events import MAINTENANCE_IN, MAINTENANCE_OUT, EvidenceRef, parse_ts, validate_sequence
from .ledger import CellLedger, RackOpEvidence
from .policy import BASES, decide
from .report import build_report

DEFAULT_WARRANTY_DAYS = 365 * 3


class WarrantyService:
    """单体责任期归属与证据链服务。"""

    def __init__(self, store: JsonStore, clock: Clock, *, warranty_days: float = DEFAULT_WARRANTY_DAYS) -> None:
        self._store = store
        self._clock = clock
        self._ledger = CellLedger(store)
        self._archive = ClaimArchive(store)
        self._warranty_s = float(warranty_days) * 86400.0

    @property
    def ledger(self) -> CellLedger:
        return self._ledger

    @property
    def archive(self) -> ClaimArchive:
        return self._archive

    @property
    def warranty_days(self) -> float:
        return self._warranty_s / 86400.0

    def _ref(self, evidence: EvidenceRef | dict) -> EvidenceRef:
        if isinstance(evidence, EvidenceRef):
            return evidence
        if isinstance(evidence, dict):
            return EvidenceRef.from_dict(evidence)
        raise StoreError("履历事件必须附带凭证", component="warranty")

    def record_event(
        self,
        *,
        cell_id: str,
        kind: str,
        at: float | str | int,
        rack_id: str = "",
        evidence: EvidenceRef | dict,
        recorded_by: str = "",
        expected_resume_at: float | str | int | None = None,
        note: str = "",
    ) -> dict:
        """录入一条单体履历事件。"""

        event = self._ledger.record_event(
            cell_id=cell_id,
            kind=kind,
            ts=parse_ts(at),
            rack_id=rack_id,
            evidence=self._ref(evidence),
            recorded_ts=self._clock.now(),
            recorded_by=recorded_by,
            expected_resume_ts=None if expected_resume_at is None else parse_ts(expected_resume_at),
            note=note,
        )
        return event.as_dict()

    def record_maintenance_pack(
        self,
        *,
        cell_ids: list[str],
        in_at: float | str | int,
        out_at: float | str | int,
        rack_id: str,
        evidence: EvidenceRef | dict,
        recorded_by: str = "",
        expected_resume_at: float | str | int | None = None,
        note: str = "",
    ) -> dict:
        """整包检修：对包内每只单体各展开入厂／复装两条履历。"""

        if not cell_ids:
            raise StoreError("整包检修至少要包含一只单体", component="warranty")
        begin = parse_ts(in_at)
        finish = parse_ts(out_at)
        if finish < begin:
            raise StoreError("复装时间不能早于入厂时间", component="warranty")
        ref = self._ref(evidence)
        recorded = []
        for cell_id in cell_ids:
            self._ledger.record_event(
                cell_id,
                MAINTENANCE_IN,
                begin,
                "",
                ref,
                recorded_ts=self._clock.now(),
                recorded_by=recorded_by,
                expected_resume_ts=None if expected_resume_at is None else parse_ts(expected_resume_at),
                note=note,
            )
            event = self._ledger.record_event(
                cell_id,
                MAINTENANCE_OUT,
                finish,
                rack_id,
                ref,
                recorded_ts=self._clock.now(),
                recorded_by=recorded_by,
                note=note,
            )
            recorded.append(event.cell_id)
        return {"cells": recorded, "in_ts": round(begin, 3), "out_ts": round(finish, 3)}

    def record_rack_operation(
        self,
        *,
        ts: float | str | int | None = None,
        rack_id: str,
        op_id: str,
        action: str,
        ok: bool,
        note: str = "",
    ) -> RackOpEvidence:
        """追加一条簇级实际运行证据（并网／充电／放电／均衡）。"""

        return self._ledger.record_rack_op(
            ts=self._clock.now() if ts is None else parse_ts(ts),
            rack_id=rack_id,
            op_id=op_id,
            action=action,
            ok=ok,
            recorded_ts=self._clock.now(),
            note=note,
        )

    def cell_overview(self, cell_id: str) -> dict:
        """给出单体履历、簇归属与证据链预检（不出报告）。"""

        events = self._ledger.events_of(cell_id)
        if not events:
            raise StoreError(f"台账中没有该单体的履历: {cell_id}", component="warranty")
        return {
            "cell_id": cell_id,
            "events": [event.as_dict() for event in events],
            "sequence_problems": validate_sequence(events),
        }

    def cell_ids(self) -> list[str]:
        return self._ledger.all_cell_ids()

    def issue_conclusion(
        self,
        *,
        cell_id: str,
        claim_at: float | str | int,
        claim_ref: str = "",
        station: str = "",
        issued_by: str = "",
        basis: str = "",
        basis_reason: str = "",
        warranty_days: float | None = None,
        max_gap_s: float = DEFAULT_MAX_GAP_S,
    ) -> dict:
        """出版一份责任归属结论（新版本自动接续，旧版本保留不动）。"""

        events = self._ledger.events_of(cell_id)
        if not events:
            raise StoreError(f"台账中没有该单体的履历: {cell_id}", component="warranty")
        if basis and basis not in BASES:
            raise StoreError(f"未知的责任期切法: {basis}", component="warranty")
        claim_ts = parse_ts(claim_at)
        warranty_s = self._warranty_s if warranty_days is None else float(warranty_days) * 86400.0

        decision = decide(events, claim_ts, warranty_s, basis=basis, basis_reason=basis_reason)
        chain = build_chain(self._ledger, cell_id, claim_ts, max_gap_s=max_gap_s)
        integrity = self._ledger.verify()
        problems = validate_sequence(events)
        version = self._archive.next_version(cell_id)
        latest = self._archive.latest(cell_id)
        supersedes = "" if latest is None else str(latest["report_id"])

        report = build_report(
            cell_id=cell_id,
            station=station,
            claim_ref=claim_ref,
            decision=decision,
            chain=chain,
            events=events,
            ledger_verify=integrity,
            issued_ts=self._clock.now(),
            issued_by=issued_by,
            sequence_problems=problems,
            version=version,
            supersedes=supersedes,
        )
        index_item = self._archive.publish(report)
        return {"report": report, "index": index_item}

    def get_conclusion(self, cell_id: str, version: int | None = None) -> dict:
        """读取已出版结论（默认最新版本）。"""

        return self._archive.load_latest(cell_id) if version is None else self._archive.load(cell_id, version)

    def list_versions(self, cell_id: str) -> list[dict]:
        return self._archive.versions(cell_id)

    def verify_all(self) -> dict:
        """核验台账哈希链与报告归档的完整性。"""

        ledger = self._ledger.verify()
        archive = self._archive.verify()
        return {"ledger": ledger, "archive": archive, "ok": ledger["ok"] and archive["ok"]}
