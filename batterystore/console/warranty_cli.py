"""责任期命令行子命令。

提供 ``warranty`` 子命令组：录入履历、整包检修、查看单体轨迹、出版责任
结论、读取已出版版本与完整性核验。输出一律为 JSON，字段与控制台接口一致，
便于脚本化归档与复核。
"""

from __future__ import annotations

import argparse
import json
import sys

from ..errors import StoreError


def build_warranty_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """注册 ``warranty`` 子命令组。"""

    warranty = sub.add_parser("warranty", help="单体责任期归属与证据链", parents=[common])
    wsub = warranty.add_subparsers(dest="warranty_command", required=True)

    def add(name: str, **kwargs: object) -> argparse.ArgumentParser:
        return wsub.add_parser(name, parents=[common], **kwargs)

    event = add("event", help="录入一条单体履历节点")
    event.add_argument("--cell-id", required=True)
    event.add_argument("--kind", required=True, choices=("delivered", "commissioned", "maintenance_in", "maintenance_out", "relocated", "decommissioned"))
    event.add_argument("--at", required=True, help="Unix 秒或 YYYY-MM-DD")
    event.add_argument("--rack-id", default="")
    event.add_argument("--doc-type", required=True)
    event.add_argument("--doc-no", required=True)
    event.add_argument("--doc-note", default="")
    event.add_argument("--expected-resume-at", default=None)
    event.add_argument("--recorded-by", default="cli")
    event.add_argument("--note", default="")
    event.set_defaults(func=_cmd_event)

    pack = add("maintenance-pack", help="登记整包检修（入厂+复装）")
    pack.add_argument("--cell-id", action="append", required=True, dest="cell_ids")
    pack.add_argument("--in-at", required=True)
    pack.add_argument("--out-at", required=True)
    pack.add_argument("--rack-id", required=True)
    pack.add_argument("--doc-type", default="检修工单")
    pack.add_argument("--doc-no", required=True)
    pack.add_argument("--expected-resume-at", default=None)
    pack.add_argument("--recorded-by", default="cli")
    pack.set_defaults(func=_cmd_maintenance_pack)

    overview = add("overview", help="查看单体履历与预检")
    overview.add_argument("--cell-id", required=True)
    overview.set_defaults(func=_cmd_overview)

    cells = add("cells", help="列出台账中全部单体")
    cells.set_defaults(func=_cmd_cells)

    issue = add("issue", help="出版责任归属结论（自动追加新版本）")
    issue.add_argument("--cell-id", required=True)
    issue.add_argument("--claim-at", required=True)
    issue.add_argument("--claim-ref", default="")
    issue.add_argument("--issued-by", default="cli")
    issue.add_argument("--basis", default="", choices=("", "delivery", "commission"))
    issue.add_argument("--basis-reason", default="")
    issue.add_argument("--warranty-days", type=float, default=None)
    issue.add_argument("--max-gap-s", type=float, default=None)
    issue.set_defaults(func=_cmd_issue)

    get = add("get", help="读取已出版结论（默认最新版本）")
    get.add_argument("--cell-id", required=True)
    get.add_argument("--version", type=int, default=None)
    get.set_defaults(func=_cmd_get)

    versions = add("versions", help="列出单体全部出版版本")
    versions.add_argument("--cell-id", required=True)
    versions.set_defaults(func=_cmd_versions)

    verify = add("verify", help="核验台账与归档哈希链")
    verify.set_defaults(func=_cmd_verify)


def _dump(payload: object) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _evidence(args: argparse.Namespace) -> dict:
    return {"doc_type": args.doc_type, "doc_no": args.doc_no, "note": args.doc_note}


def _cmd_event(args: argparse.Namespace) -> int:
    platform = args.platform()
    return _dump(
        platform.warranty_record_event(
            cell_id=args.cell_id,
            kind=args.kind,
            at=args.at,
            rack_id=args.rack_id,
            evidence=_evidence(args),
            recorded_by=args.recorded_by,
            expected_resume_at=args.expected_resume_at,
            note=args.note,
        )
    )


def _cmd_maintenance_pack(args: argparse.Namespace) -> int:
    platform = args.platform()
    return _dump(
        platform.warranty_record_maintenance_pack(
            cell_ids=args.cell_ids,
            in_at=args.in_at,
            out_at=args.out_at,
            rack_id=args.rack_id,
            evidence={"doc_type": args.doc_type, "doc_no": args.doc_no},
            recorded_by=args.recorded_by,
            expected_resume_at=args.expected_resume_at,
        )
    )


def _cmd_overview(args: argparse.Namespace) -> int:
    return _dump(args.platform().warranty_overview(args.cell_id))


def _cmd_cells(args: argparse.Namespace) -> int:
    return _dump({"cells": args.platform().warranty_cell_ids()})


def _cmd_issue(args: argparse.Namespace) -> int:
    return _dump(
        args.platform().warranty_issue(
            cell_id=args.cell_id,
            claim_at=args.claim_at,
            claim_ref=args.claim_ref,
            issued_by=args.issued_by,
            basis=args.basis,
            basis_reason=args.basis_reason,
            warranty_days=args.warranty_days,
            max_gap_s=args.max_gap_s,
        )
    )


def _cmd_get(args: argparse.Namespace) -> int:
    return _dump(args.platform().warranty_get(args.cell_id, args.version))


def _cmd_versions(args: argparse.Namespace) -> int:
    return _dump({"versions": args.platform().warranty_versions(args.cell_id)})


def _cmd_verify(args: argparse.Namespace) -> int:
    result = args.platform().warranty_verify()
    _dump(result)
    return 0 if result["ok"] else 1


def run_warranty(args: argparse.Namespace) -> int:
    """``warranty`` 子命令入口，捕获台账校验异常并转成退出码 2。"""

    try:
        return int(args.func(args))
    except StoreError as error:
        print(json.dumps(error.as_record(), ensure_ascii=False), file=sys.stderr)
        return 2
