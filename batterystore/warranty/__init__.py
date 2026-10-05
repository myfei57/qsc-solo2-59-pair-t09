"""单体责任期与索赔证据链。

面向"客户拿单体索赔"场景的证据子系统，三层职责分开：

:mod:`batterystore.warranty.events` / :mod:`batterystore.warranty.ledger`
    只追加、带 SHA-256 哈希链的单体履历台账（交付、投运、停运检修/搬迁、
    重新投运、搬迁、运行证据）；
:mod:`batterystore.warranty.timeline`
    把一只单体的事件按时间拼成可复核履历，状态机矛盾显式标为断点、在役
    区间缺运行证据显式标为缺段、跨簇流转按时间合并；
:mod:`batterystore.warranty.liability`
    按交付日期与按并网投运日期两种切法各算一遍责任归属，选定其中一种并把
    选择理由写进结论；
:mod:`batterystore.warranty.package` / :mod:`batterystore.warranty.registry`
    材料包固化与只追加版本登记，老版本字节不可变，复核只能追加新版本。
"""

from __future__ import annotations

from .events import EVENT_ACTIONS, LIFECYCLE_ACTIONS, CellEvent, fingerprint_events
from .ledger import CellEventLedger, cell_slug
from .liability import (
    DEFAULT_WARRANTY_TERM_S,
    BASES,
    attribute_liability,
)
from .package import PACKAGE_KIND, build_claim_package
from .registry import ClaimRegistry
from .timeline import DEFAULT_GAP_THRESHOLD_S, build_cell_timeline

__all__ = [
    "EVENT_ACTIONS",
    "LIFECYCLE_ACTIONS",
    "CellEvent",
    "CellEventLedger",
    "ClaimRegistry",
    "PACKAGE_KIND",
    "BASES",
    "DEFAULT_GAP_THRESHOLD_S",
    "DEFAULT_WARRANTY_TERM_S",
    "attribute_liability",
    "build_cell_timeline",
    "build_claim_package",
    "cell_slug",
    "fingerprint_events",
]
