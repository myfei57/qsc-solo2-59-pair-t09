"""单体证据时间线。

台账只保证"事件没被改过"，时间线负责把一只单体的事件按时间铺开、解释成
人能复核的履历，并完成三件索赔时绕不开的事：

1. **状态机核对**：交付 → 投运 →（停运检修/搬迁 → 重新投运）* 的先后是否
   成立。顺序矛盾不会被跳过，而是显式落成 ``break``（结构性断点），结论
   侧据此只能判"无法认定"。
2. **缺段标注**：在役区间内运行证据之间的空当超过阈值，或整段在役没有任何
   运行证据，显式落成 ``gap``，绝不拿相邻证据把空当悄悄盖过去。
3. **跨簇流转拼接**：连续在役区间按所在簇合并成一条流转链，搬迁（含先停运
   后搬迁的离网空窗）在链上看得见。
"""

from __future__ import annotations

from .events import CellEvent

# 在役区间内，相邻运行证据间隔超过 90 天即判缺段。
DEFAULT_GAP_THRESHOLD_S = 90 * 24 * 3600.0

_SEVERITY_BREAK = "break"
_SEVERITY_GAP = "gap"
_SEVERITY_WARNING = "warning"


def build_cell_timeline(
    events: list[CellEvent],
    *,
    as_of: float | None = None,
    gap_threshold_s: float = DEFAULT_GAP_THRESHOLD_S,
) -> dict:
    """把一只单体的事件组装成带断点与缺段标注的证据时间线。"""

    threshold = max(0.0, float(gap_threshold_s))
    chrono = sorted(events, key=lambda event: (event.ts, event.seq))
    cutoff = round(float(as_of), 3) if as_of is not None else None
    if cutoff is not None:
        chrono = [event for event in chrono if event.ts <= cutoff]

    anomalies: list[dict] = []

    def flag(code: str, severity: str, message: str, seq: int | None = None, ts: float | None = None) -> None:
        anomalies.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "seq": seq,
                "ts": round(ts, 3) if ts is not None else None,
            }
        )

    # 追加顺序与时间顺序不一致：事件本身没被改（哈希链仍成立），但有人事后
    # 补录了更早的证据，必须点出来让复核人判断补录是否可信。
    for earlier, later in zip(chrono, chrono[1:]):
        if earlier.seq > later.seq:
            flag(
                "event_out_of_order",
                _SEVERITY_WARNING,
                f"事件 {later.seq} 的时间早于其追加顺序，疑似事后补录",
                seq=later.seq,
                ts=later.ts,
            )
            break

    lifecycle: list[dict] = []
    runtime_entries: list[dict] = []
    raw_segments: list[dict] = []
    state = "pre_delivery"
    rack: str | None = None
    seg_start: float | None = None
    seg_start_seq: int | None = None
    seg_reason: str | None = None
    delivered_ts: float | None = None
    first_commission_ts: float | None = None

    def open_segment(new_state: str, ts: float, seq: int, reason: str | None = None) -> None:
        nonlocal seg_start, seg_start_seq, seg_reason
        seg_start, seg_start_seq, seg_reason = ts, seq, reason

    def close_segment(end_ts: float, end_seq: int | None) -> None:
        nonlocal seg_start, seg_start_seq, seg_reason
        if seg_start is None:
            return
        raw_segments.append(
            {
                "state": state,
                "rack_id": rack,
                "start_ts": round(seg_start, 3),
                "end_ts": round(end_ts, 3),
                "start_seq": seg_start_seq,
                "end_seq": end_seq,
                "reason": seg_reason,
            }
        )
        seg_start, seg_start_seq, seg_reason = None, None, None

    for event in chrono:
        action = event.action
        if action == "delivered":
            if state != "pre_delivery":
                flag("duplicate_delivery", _SEVERITY_BREAK, "单体出现第二次交付记录", event.seq, event.ts)
            else:
                delivered_ts = event.ts
                state = "idle"
                open_segment(state, event.ts, event.seq)
        elif action in ("commissioned", "recommissioned"):
            if action == "commissioned":
                if state == "pre_delivery":
                    flag("commission_before_delivery", _SEVERITY_BREAK, "未交付先并网投运", event.seq, event.ts)
                elif state == "in_service":
                    flag("commissioned_while_in_service", _SEVERITY_BREAK, "已在役又出现首次投运记录", event.seq, event.ts)
                elif state == "out_of_service":
                    flag("expected_recommission", _SEVERITY_BREAK, "停运后应记录 recommissioned，却记录了 commissioned", event.seq, event.ts)
            elif state != "out_of_service":
                flag("recommission_without_decommission", _SEVERITY_BREAK, "未记录停运就出现重新投运", event.seq, event.ts)
            close_segment(event.ts, event.seq)
            rack = event.payload.get("rack_id") or rack
            state = "in_service"
            open_segment(state, event.ts, event.seq)
            first_commission_ts = event.ts if first_commission_ts is None else first_commission_ts
        elif action == "decommissioned":
            if state != "in_service":
                flag("decommission_not_in_service", _SEVERITY_BREAK, "单体不在役却出现停运记录", event.seq, event.ts)
            close_segment(event.ts, event.seq)
            state = "out_of_service"
            open_segment(state, event.ts, event.seq, reason=event.payload.get("reason"))
        elif action == "relocated":
            if state == "in_service":
                flag(
                    "relocated_without_decommission",
                    _SEVERITY_BREAK,
                    "搬迁前没有停运记录，跨簇流转缺检修/搬迁凭据",
                    event.seq,
                    event.ts,
                )
            elif state == "out_of_service" and seg_reason != "relocation":
                flag(
                    "relocated_after_maintenance_stop",
                    _SEVERITY_BREAK,
                    "停运原因是检修却出现搬迁记录",
                    event.seq,
                    event.ts,
                )
            rack = event.payload.get("to_rack") or rack
        elif action == "runtime":
            if state != "in_service":
                flag(
                    "runtime_not_in_service",
                    _SEVERITY_BREAK,
                    "运行证据落在非在役区间（未投运或已停运）",
                    event.seq,
                    event.ts,
                )
            runtime_entries.append(
                {
                    "type": "runtime",
                    "seq": event.seq,
                    "ts": event.ts,
                    "rack_id": event.payload.get("rack_id") or rack,
                    "grid_seconds": event.payload["grid_seconds"],
                    "charge_kwh": event.payload["charge_kwh"],
                    "discharge_kwh": event.payload["discharge_kwh"],
                    "source": event.source,
                    "in_service": state == "in_service",
                }
            )
        if event.is_lifecycle():
            lifecycle.append(
                {
                    "type": "lifecycle",
                    "seq": event.seq,
                    "ts": event.ts,
                    "action": action,
                    "rack_id": event.payload.get("rack_id") or event.payload.get("to_rack") or rack,
                    "source": event.source,
                    "payload": dict(event.payload),
                }
            )

    # 末段：指定截止时刻就在该时刻收口；否则保持开口，缺口与时长按已知末证据推。
    if seg_start is not None:
        raw_segments.append(
            {
                "state": state,
                "rack_id": rack,
                "start_ts": round(seg_start, 3),
                "end_ts": cutoff,
                "start_seq": seg_start_seq,
                "end_seq": None,
                "reason": seg_reason,
                "open": cutoff is None,
            }
        )

    last_ts = chrono[-1].ts if chrono else None
    if delivered_ts is None:
        flag("missing_delivery", _SEVERITY_BREAK, "整条履历没有交付记录")
    if first_commission_ts is None and delivered_ts is not None:
        flag("never_commissioned", _SEVERITY_WARNING, "已交付但从未记录并网投运")

    segments: list[dict] = []
    gaps: list[dict] = []
    for index, segment in enumerate(raw_segments):
        seg_gaps = _detect_gaps(segment, runtime_entries, threshold, index, gaps)
        segment_view = {key: value for key, value in segment.items() if key != "open"}
        segments.append(
            {
                "type": "segment",
                "id": f"seg-{index}",
                **segment_view,
                "gaps": [gap["id"] for gap in seg_gaps],
            }
        )

    break_refs = [item for item in anomalies if item["severity"] == _SEVERITY_BREAK]
    timeline = _merge_timeline(lifecycle, runtime_entries, segments, gaps, break_refs)
    flow = _rack_flow(raw_segments)
    horizon = cutoff if cutoff is not None else last_ts
    totals = _totals(raw_segments, runtime_entries, horizon)

    return {
        "cell_id": chrono[0].cell_id if chrono else "",
        "timeline": timeline,
        "segments": segments,
        "gaps": gaps,
        "breaks": break_refs,
        "anomalies": anomalies,
        "rack_flow": flow,
        "racks_visited": _racks_visited(flow),
        "totals": totals,
        "delivered_ts": round(delivered_ts, 3) if delivered_ts is not None else None,
        "first_commission_ts": round(first_commission_ts, 3) if first_commission_ts is not None else None,
        "last_ts": round(last_ts, 3) if last_ts is not None else None,
        "as_of": cutoff,
        "gap_threshold_s": threshold,
        "structurally_intact": not break_refs,
    }


def _detect_gaps(segment: dict, runtime_entries: list[dict], threshold: float, seg_index: int, gaps: list[dict]) -> list[dict]:
    """在一段在役区间内找缺段，返回落在该段内的缺段列表。"""

    if segment["state"] != "in_service":
        return []
    start = segment["start_ts"]
    open_ended = bool(segment.get("open"))
    end = segment["end_ts"]
    if end is None:
        candidates = [entry["ts"] for entry in runtime_entries if entry["ts"] >= start]
        end = max(candidates) if candidates else None
    inside = sorted(
        (
            entry
            for entry in runtime_entries
            if entry["ts"] >= start and (end is None or entry["ts"] <= end)
        ),
        key=lambda item: item["ts"],
    )
    found: list[dict] = []
    if not inside:
        # 整段在役却没有任何运行证据：不论区间长短都要显式标出来。
        found.append(
            _add_gap(
                gaps,
                seg_index,
                start,
                None if open_ended else end,
                "missing_runtime_evidence",
                "该在役区间没有任何实际运行证据",
                open_ended=open_ended,
            )
        )
        return found
    cursor = start
    for entry in inside:
        if entry["ts"] - cursor > threshold:
            found.append(
                _add_gap(
                    gaps,
                    seg_index,
                    cursor,
                    entry["ts"],
                    "evidence_gap",
                    "在役区间内运行证据中断超过阈值",
                    open_ended=False,
                )
            )
        cursor = max(cursor, entry["ts"])
    tail_end = None if open_ended else end
    if tail_end is None:
        found.append(
            _add_gap(
                gaps,
                seg_index,
                cursor,
                None,
                "evidence_gap",
                "末次运行证据之后再无证据，区间保持开口",
                open_ended=True,
            )
        )
    elif tail_end - cursor > threshold:
        found.append(
            _add_gap(
                gaps,
                seg_index,
                cursor,
                tail_end,
                "evidence_gap",
                "末次运行证据之后长时间无证据",
                open_ended=False,
            )
        )
    return found


def _add_gap(
    gaps: list[dict],
    seg_index: int,
    start_ts: float,
    end_ts: float | None,
    code: str,
    message: str,
    *,
    open_ended: bool,
) -> dict:
    gap = {
        "type": "gap",
        "id": f"gap-{len(gaps)}",
        "segment_id": f"seg-{seg_index}",
        "code": code,
        "start_ts": round(start_ts, 3),
        "end_ts": round(end_ts, 3) if end_ts is not None else None,
        "duration_s": round(end_ts - start_ts, 3) if end_ts is not None else None,
        "open_ended": open_ended,
        "message": message,
    }
    gaps.append(gap)
    return gap


def _merge_timeline(
    lifecycle: list[dict],
    runtime_entries: list[dict],
    segments: list[dict],
    gaps: list[dict],
    breaks: list[dict],
) -> list[dict]:
    """把锚点事件、区间、运行证据、缺段与断点按时间位置排成一条可复核序列。"""

    items: list[tuple[float, int, dict]] = []
    for segment in segments:
        items.append((segment["start_ts"], 0, segment))
    for entry in lifecycle:
        items.append((entry["ts"], 1, entry))
    for entry in runtime_entries:
        items.append((entry["ts"], 2, entry))
    for gap in gaps:
        items.append((gap["start_ts"], 3, gap))
    for index, item in enumerate(breaks):
        items.append((item["ts"] if item["ts"] is not None else 0.0, 4, {"type": "break", "id": f"brk-{index}", **item}))
    items.sort(key=lambda pair: (pair[0], pair[1]))
    return [item for _ts, _rank, item in items]


def _rack_flow(raw_segments: list[dict]) -> list[dict]:
    """把相邻同簇、同状态的区间合并成流转链。"""

    flow: list[dict] = []
    for segment in raw_segments:
        current = {
            "state": segment["state"],
            "rack_id": segment["rack_id"],
            "from_ts": segment["start_ts"],
            "to_ts": segment["end_ts"],
            "reason": segment["reason"],
        }
        if flow and flow[-1]["state"] == current["state"] and flow[-1]["rack_id"] == current["rack_id"]:
            flow[-1]["to_ts"] = current["to_ts"]
        else:
            flow.append(dict(current))
    return flow


def _racks_visited(flow: list[dict]) -> list[str]:
    """按时间顺序返回单体在役停留过的簇（去重保序）。"""

    visited: list[str] = []
    for item in flow:
        rack_id = item["rack_id"]
        if item["state"] == "in_service" and rack_id and rack_id not in visited:
            visited.append(rack_id)
    return visited


def _totals(raw_segments: list[dict], runtime_entries: list[dict], horizon: float | None) -> dict:
    """汇总在役/离网时长与实际运行证据量。"""

    in_service_s = 0.0
    out_of_service_s = 0.0
    idle_s = 0.0
    for segment in raw_segments:
        end = segment["end_ts"]
        if end is None:
            end = horizon if horizon is not None else segment["start_ts"]
        duration = max(0.0, end - segment["start_ts"])
        if segment["state"] == "in_service":
            in_service_s += duration
        elif segment["state"] == "out_of_service":
            out_of_service_s += duration
        elif segment["state"] == "idle":
            idle_s += duration
    grid_seconds = sum(entry["grid_seconds"] for entry in runtime_entries if entry["in_service"])
    return {
        "in_service_seconds": round(in_service_s, 3),
        "out_of_service_seconds": round(out_of_service_s, 3),
        "idle_seconds": round(idle_s, 3),
        "runtime_grid_seconds": round(grid_seconds, 3),
        "charge_kwh": round(sum(entry["charge_kwh"] for entry in runtime_entries if entry["in_service"]), 3),
        "discharge_kwh": round(sum(entry["discharge_kwh"] for entry in runtime_entries if entry["in_service"]), 3),
        "runtime_evidence_count": sum(1 for entry in runtime_entries if entry["in_service"]),
    }
