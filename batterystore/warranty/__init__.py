"""单体责任期归属与运行证据链。

面向客户索赔场景：为每只单体出具一份可人工复核的责任期归属结论，附按时间
拼好的完整运行证据链。责任期支持「按交付日期」与「按并网投运日期」两种
切法，两法结论不一致时必须选一种并写明理由；跨簇流转轨迹逐段保留，证据
缺段显式标注；结论出版后不可修改，只能追加声明取代关系的新版本。
"""

from __future__ import annotations

from .archive import ClaimArchive
from .chain import build_chain
from .events import (
    COMMISSIONED,
    DECOMMISSIONED,
    DELIVERED,
    MAINTENANCE_IN,
    MAINTENANCE_OUT,
    RELOCATED,
    CellEvent,
    EvidenceRef,
    iso_date,
    parse_ts,
)
from .ledger import CellLedger, RackOpEvidence
from .membership import build_membership
from .policy import COMMISSION_BASIS, DELIVERY_BASIS, decide
from .report import build_report
from .service import WarrantyService

__all__ = [
    "ClaimArchive",
    "CellEvent",
    "CellLedger",
    "COMMISSIONED",
    "COMMISSION_BASIS",
    "DELIVERED",
    "DELIVERY_BASIS",
    "DECOMMISSIONED",
    "EvidenceRef",
    "MAINTENANCE_IN",
    "MAINTENANCE_OUT",
    "RELOCATED",
    "RackOpEvidence",
    "WarrantyService",
    "build_chain",
    "build_membership",
    "build_report",
    "decide",
    "iso_date",
    "parse_ts",
]
