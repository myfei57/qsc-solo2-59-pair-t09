"""单体责任期归属结论报告。

报告是把三份材料钉在一起的不可变快照：归属判定（两种切法 + 选定理由）、
逐段簇归属、完整证据链（含显式缺段）。快照生成时输入台账可能还在增长，但
报告一旦出版，其内容摘要与证据坐标就固定下来；后续数据只能追加，不能回头
改这份报告，复核人拿旧版本号必须能还原出一模一样的内容。
"""

from __future__ import annotations

from .hashing import canonical_digest
from .membership import build_membership

REPORT_KIND = "cell_warranty_conclusion"
REPORT_VERSION = 1

# 摘要在 ``report_id`` 之前计算，复核重算时必须同时排除这两个后加字段。
_DIGEST_EXCLUDE = ("body_digest", "report_id")


def body_digest_of(report: dict) -> str:
    """从报告内容重算摘要（不信任文件里自带的摘要字段）。"""

    return canonical_digest({k: v for k, v in report.items() if k not in _DIGEST_EXCLUDE})


def build_report(
    *,
    cell_id: str,
    station: str,
    claim_ref: str,
    decision: dict,
    chain: dict,
    events: list,
    ledger_verify: dict,
    issued_ts: float,
    issued_by: str,
    sequence_problems: list[str],
    version: int = 1,
    supersedes: str = "",
) -> dict:
    """组装一份完整结论报告（调用方负责落 WORM 归档）。"""

    segments = build_membership(events)
    body = {
        "report_kind": REPORT_KIND,
        "report_version": REPORT_VERSION,
        "version": int(version),
        "supersedes": supersedes,
        "station": station,
        "cell_id": cell_id,
        "claim_ref": claim_ref,
        "issued_ts": round(issued_ts, 3),
        "issued_by": issued_by,
        "decision": decision,
        "membership": [segment.as_dict() for segment in segments],
        "chain": chain,
        "ledger_integrity": ledger_verify,
        "sequence_problems": sequence_problems,
    }
    body["headline"] = _headline(cell_id, decision)
    body["body_digest"] = body_digest_of(body)
    body["report_id"] = f"rpt-{cell_id}-v{int(version):03d}-{body['body_digest'][:12]}"
    return body


def _headline(cell_id: str, decision: dict) -> str:
    outcome = decision["outcome"]
    claim_date = decision["claim_date"]
    basis_label = decision["basis_label"] or "无可用切法"
    selected = decision["basis_results"].get(decision["basis"])
    if outcome == "undetermined":
        return f"单体 {cell_id}：{decision['outcome_label']}（索赔日 {claim_date}）"
    end_date = selected["end_date"] if selected else "?"
    return (
        f"单体 {cell_id}：{decision['outcome_label']}；{basis_label}，"
        f"责任期截止 {end_date}（含检修顺延 {selected['toll_days'] if selected else 0} 天），索赔日 {claim_date}。"
    )
