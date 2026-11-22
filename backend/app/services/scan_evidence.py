"""合规扫描证据登记：按资产数据范围保存、读取并汇总质量准确率依据。

设计要点：
    * 每份扫描证据都必须带 ``asset_id``、``partitions``、``collected_on`` 和异常明细，
      没有资产归属的证据不进入任何报告，避免“借来”的合规率。
    * 资产数据范围用精确匹配（``same_asset``），不做子串匹配。
    * 汇总时按采集日期归入 ISO 周：本周采集的扫描计入本周分子/分母，
      历史采集只作为可复核依据，绝不混入本周分数。
    * 原始证据由调用方另行快照保存，本模块只负责规范化与计算。
"""
from __future__ import annotations

import copy
import json
import os
import re
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

SCAN_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "quality", "scans")

WEEKLY_BASIS = "本周扫描"
HISTORICAL_BASIS = "历史证据"

# 扫描证据文件里允许的字段别名，兼容中英文数据产物。
_ASSET_KEYS = ("asset_id", "assetId", "关联资产", "目标资产", "资产编号")
_PARTITION_KEYS = ("partitions", "covered_partitions", "scanned_partitions", "覆盖分区", "扫描分区")
_COLLECTED_KEYS = ("collected_on", "collectedOn", "采集日期", "checked_at", "检查时间")
_ANOMALY_KEYS = ("anomalies", "anomaly_details", "异常明细", "异常")
_VIOLATION_KEYS = ("violations", "violation_count", "异常数", "违规数")
_CHECKED_KEYS = ("checked_count", "checked", "检查数", "扫描数")


def evidence_dir() -> str:
    """证据目录；测试或换环境时可用 QUALITY_EVIDENCE_DIR 指向独立目录。"""
    override = os.environ.get("QUALITY_EVIDENCE_DIR")
    return os.path.abspath(override) if override else SCAN_DIR


def _ensure_dir() -> None:
    os.makedirs(evidence_dir(), exist_ok=True)


# ---------- 规范化 ----------

def canonical_asset(value: Any) -> str:
    """资产标识的规范形式；只做大小写与空白归一，绝不截断。"""
    return str(value or "").strip().casefold()


def same_asset(left: Any, right: Any) -> bool:
    """严格判断两个资产标识是否属于同一数据范围。"""
    left_key = canonical_asset(left)
    return bool(left_key) and left_key == canonical_asset(right)


def normalize_partitions(value: Any) -> list[str]:
    """把列表或分隔字符串规范为去重后的分区名列表。"""
    if value is None:
        return []
    collected: list[str] = []
    if isinstance(value, dict):
        value = list(value.keys())
    if isinstance(value, (list, tuple, set)):
        for item in value:
            collected.extend(normalize_partitions(item))
    else:
        text = str(value).strip()
        if not text:
            return []
        # 常见分隔符：中英文逗号、分号、顿号、竖线、换行。
        pieces = [piece for piece in re.split(r"[,，;；、|\n\r]+", text) if piece.strip()]
        if len(pieces) == 1 and " " in pieces[0]:
            # 空格分隔（如 "分区-01 分区-02"）也接受，但保留带空格的单个分区名。
            spaced = [piece for piece in pieces[0].split() if piece]
            if len(spaced) > 1:
                pieces = spaced
        collected.extend(piece.strip() for piece in pieces if piece.strip())
    seen: set[str] = set()
    result: list[str] = []
    for item in collected:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def parse_date(value: Any) -> date | None:
    """解析采集日期，支持 ISO 日期与带时区的 ISO 时间。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def iso_week(value: Any) -> tuple[int, int] | None:
    day = parse_date(value)
    if day is None:
        return None
    calendar = day.isocalendar()
    return calendar.year, calendar.week


def week_window(value: Any) -> dict[str, Any]:
    """返回该日期所在 ISO 周的起止日期与标签。"""
    day = parse_date(value) or date.today()
    calendar = day.isocalendar()
    monday = day - timedelta(days=calendar.weekday - 1)
    sunday = monday + timedelta(days=6)
    return {
        "iso_year": calendar.year,
        "iso_week": calendar.week,
        "label": f"{calendar.year}-W{calendar.week:02d}",
        "start": monday.isoformat(),
        "end": sunday.isoformat(),
        "reference_date": day.isoformat(),
    }


def _first(raw: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _normalize_anomalies(raw: dict[str, Any], partitions: list[str]) -> list[dict[str, Any]]:
    source = _first(raw, _ANOMALY_KEYS)
    anomalies: list[dict[str, Any]] = []
    if isinstance(source, dict):
        for partition, detail in source.items():
            count = parse_int(detail if not isinstance(detail, dict) else detail.get("count"), 1)
            if count > 0:
                anomalies.append({
                    "partition": str(partition).strip(),
                    "count": count,
                    "detail": detail if isinstance(detail, dict) else {},
                })
    elif isinstance(source, (list, tuple)):
        for item in source:
            if isinstance(item, dict):
                partition = str(_first(item, ("partition", "分区", "name", "分区名")) or "").strip()
                count = parse_int(_first(item, ("count", "数量", "异常数")), 1)
                if partition and count > 0:
                    anomalies.append({
                        "partition": partition,
                        "count": count,
                        "detail": item.get("detail", item.get("说明", "")),
                    })
            elif item not in (None, ""):
                # 只有分区名时视为 1 个异常。
                anomalies.append({"partition": str(item).strip(), "count": 1, "detail": {}})
    # 异常明细里出现未声明的分区时，把它补进覆盖范围（证据自身声明优先）。
    for anomaly in anomalies:
        if anomaly["partition"] and anomaly["partition"] not in partitions:
            partitions.append(anomaly["partition"])
    return anomalies


def normalize_evidence(raw: Any, *, require_attribution: bool = True) -> dict[str, Any] | None:
    """把一份扫描记录规范为可计算证据；缺少资产归属或采集日期时返回 None。"""
    if not isinstance(raw, dict):
        return None
    asset_id = str(_first(raw, _ASSET_KEYS) or "").strip()
    partitions = normalize_partitions(_first(raw, _PARTITION_KEYS))
    collected = parse_date(_first(raw, _COLLECTED_KEYS))
    if require_attribution and not asset_id:
        return None
    if not partitions or collected is None:
        return None

    anomalies = _normalize_anomalies(raw, partitions)
    violations = parse_int(_first(raw, _VIOLATION_KEYS), sum(item["count"] for item in anomalies))
    if anomalies:
        violations = max(violations, sum(item["count"] for item in anomalies))
    checked = parse_int(_first(raw, _CHECKED_KEYS), len(partitions))

    scan_id = str(raw.get("scan_id") or raw.get("扫描编号") or raw.get("id") or "").strip()
    if not scan_id:
        digest = "-".join(sorted(partitions))
        scan_id = f"SCAN-{asset_id or 'UNATTRIBUTED'}-{collected.strftime('%Y%m%d')}-{abs(zlib.crc32(digest.encode('utf-8'))) % 10000:04d}"

    window = week_window(collected)
    normalized = {
        "scan_id": scan_id,
        "asset_id": asset_id,
        "asset_name": str(raw.get("asset_name") or raw.get("资产名称") or "").strip(),
        "rule_id": str(raw.get("rule_id") or raw.get("规则编号") or "").strip(),
        "source": str(raw.get("source") or raw.get("来源") or "compliance_check").strip(),
        "partitions": list(partitions),
        "collected_on": collected.isoformat(),
        "collected_at": str(raw.get("collected_at") or raw.get("checked_at") or "").strip(),
        "checked_count": checked,
        "violations": violations,
        "anomalies": anomalies,
        "anomaly_partitions": sorted({item["partition"] for item in anomalies if item["partition"]}),
        "passed": violations == 0,
        "week": window,
        "note": str(raw.get("note") or raw.get("备注") or "").strip(),
        "recorded_at": str(raw.get("recorded_at") or raw.get("登记时间") or "").strip(),
    }
    if require_attribution and not normalized["asset_id"]:
        return None
    return normalized


# ---------- 读写 ----------

def evidence_path(scan_id: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.\-一-鿿]+", "_", scan_id) or "SCAN"
    return os.path.join(evidence_dir(), f"{safe}.json")


def record_evidence(scan: dict[str, Any]) -> dict[str, Any]:
    """登记一份扫描证据并落盘；返回规范化后的证据。"""
    normalized = normalize_evidence(scan)
    if normalized is None:
        raise ValueError("扫描证据缺少资产归属、分区或采集日期，不能登记")
    _ensure_dir()
    record = dict(normalized)
    record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    if not record.get("recorded_at"):
        record["recorded_at"] = datetime.now(timezone.utc).isoformat()
    path = evidence_path(record["scan_id"])
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, default=str)
    return normalize_evidence(record) or record


def load_evidence(
    asset_id: str | None = None,
    *,
    with_rejected: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取扫描证据；给 asset_id 时只返回精确归属于该资产的数据。

    未归属或格式不完整的文件会被隔离，不会参与任何计算。
    """
    _ensure_dir()
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    directory = evidence_dir()
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            rejected.append({"file": filename, "reason": "无法读取 JSON"})
            continue
        normalized = normalize_evidence(raw)
        if normalized is None:
            rejected.append({"file": filename, "reason": "缺少资产归属、分区或采集日期"})
            continue
        if asset_id is not None and not same_asset(normalized["asset_id"], asset_id):
            continue
        valid.append(normalized)
    valid.sort(key=lambda item: (item["collected_on"], item["scan_id"]))
    if with_rejected:
        return valid, rejected
    return valid


def snapshot_evidence(asset_id: str) -> list[dict[str, Any]]:
    """深拷贝某资产当前的全部有效证据，供报告保存原始依据。"""
    return copy.deepcopy(load_evidence(asset_id))


def partition_universe(asset_id: str, evidence: list[dict[str, Any]] | None = None) -> list[str]:
    """该资产可识别的分区全集；没有证据时返回空列表（从未扫描）。"""
    rows = load_evidence(asset_id) if evidence is None else evidence
    partitions: set[str] = set()
    for item in rows:
        if same_asset(item.get("asset_id"), asset_id):
            partitions.update(item.get("partitions", []))
    return sorted(partitions)


# ---------- 示例证据 ----------

SEED_SCAN_IDS = ("SEED-ORDER-OLD", "SEED-ORDER-NEW")


def ensure_seed_evidence() -> None:
    """写入演示场景证据（已存在则跳过）。

    场景：
        * dwd_order_detail 三个分区；上周扫描覆盖三个分区且无异常；
        * 本周扫描只覆盖前两个分区，并在 dt=2026-10-02 发现一个异常；
        * 第三个分区本周未复扫，只能引用上周（历史）采集依据。
        * dwd_user_profile 从未扫描，没有任何证据文件。
    日期按当前 ISO 周推算，保证“本周/历史”的归属稳定。
    """
    _ensure_dir()
    if any(os.path.exists(evidence_path(scan_id)) for scan_id in SEED_SCAN_IDS):
        return

    today = date.today()
    this_monday = today - timedelta(days=today.isocalendar().weekday - 1)
    previous_tuesday = this_monday - timedelta(days=6)
    this_wednesday = this_monday + timedelta(days=2)

    seed_rows = [
        {
            "scan_id": "SEED-ORDER-OLD",
            "asset_id": "dwd_order_detail",
            "asset_name": "订单明细表",
            "rule_id": "COMP-0001",
            "source": "compliance_check",
            "partitions": ["dt=2026-10-01", "dt=2026-10-02", "dt=2026-10-03"],
            "collected_on": previous_tuesday.isoformat(),
            "collected_at": f"{previous_tuesday.isoformat()}T02:00:00+00:00",
            "checked_count": 3,
            "violations": 0,
            "anomalies": [],
            "note": "上周全量扫描，三个分区均无异常",
            "recorded_at": f"{previous_tuesday.isoformat()}T02:00:00+00:00",
        },
        {
            "scan_id": "SEED-ORDER-NEW",
            "asset_id": "dwd_order_detail",
            "asset_name": "订单明细表",
            "rule_id": "COMP-0002",
            "source": "compliance_check",
            "partitions": ["dt=2026-10-01", "dt=2026-10-02"],
            "collected_on": this_wednesday.isoformat(),
            "collected_at": f"{this_wednesday.isoformat()}T03:30:00+00:00",
            "checked_count": 2,
            "violations": 1,
            "anomalies": [
                {"partition": "dt=2026-10-02", "count": 1, "detail": "主键重复 1 条"},
            ],
            "note": "本周增量扫描，dt=2026-10-02 发现异常",
            "recorded_at": f"{this_wednesday.isoformat()}T03:30:00+00:00",
        },
    ]
    for row in seed_rows:
        record_evidence(row)


# ---------- 汇总计算 ----------

def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda item: (item.get("collected_on", ""), item.get("scan_id", "")))


def partition_anomaly_count(evidence: dict[str, Any], partition: str) -> int:
    """某证据在指定分区上的异常数。

    有逐分区异常明细时按明细取数；没有明细但有异常时，为免把异常算到别的分区，
    对多分区证据按“每个声明分区都存在未拆分的异常”保守处理，至少不会误判为通过。
    """
    anomalies = evidence.get("anomalies", [])
    if anomalies:
        # 有逐分区明细时，只有明确落在该分区上的异常才算数。
        explicit = [
            item for item in anomalies
            if str(item.get("partition", "")).strip() == partition
        ]
        return sum(parse_int(item.get("count"), 0) for item in explicit)
    # 没有分区明细时，异常无法归到具体分区，对证据声明的每个分区都按异常处理，
    # 宁可判不过也不把异常悄悄丢掉。
    return max(0, parse_int(evidence.get("violations"), 0))


def _evidence_view(evidence: dict[str, Any], basis: str, included: bool) -> dict[str, Any]:
    return {
        "scan_id": evidence.get("scan_id", ""),
        "rule_id": evidence.get("rule_id", ""),
        "asset_id": evidence.get("asset_id", ""),
        "partitions": list(evidence.get("partitions", [])),
        "collected_on": evidence.get("collected_on", ""),
        "week": evidence.get("week", {}),
        "basis": basis,
        "included_in_week_score": included,
        "checked_count": evidence.get("checked_count", 0),
        "violations": evidence.get("violations", 0),
        "anomalies": copy.deepcopy(evidence.get("anomalies", [])),
        "passed": bool(evidence.get("passed")),
        "note": evidence.get("note", ""),
    }


def build_accuracy_evidence(
    asset_id: str,
    reference_date: Any = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按资产数据范围汇总质量准确率证据明细。

    返回结构同时包含本周分子/分母、覆盖率、结论、逐分区依据和跨日期冲突，
    供质量报告落库与复核使用。
    """
    asset = str(asset_id or "").strip()
    reference = parse_date(reference_date) or date.today()
    window = week_window(reference)
    current_week = (window["iso_year"], window["iso_week"])

    raw_rows = load_evidence(asset) if evidence is None else evidence
    rows = [
        copy.deepcopy(item) for item in raw_rows
        if same_asset(item.get("asset_id"), asset)
    ]
    rows.sort(key=lambda item: (item.get("collected_on", ""), item.get("scan_id", "")))

    universe = sorted({partition for item in rows for partition in item.get("partitions", [])})
    weekly = [item for item in rows if iso_week(item.get("collected_on")) == current_week]
    historical = [item for item in rows if iso_week(item.get("collected_on")) != current_week]

    week_start, week_end = window["start"], window["end"]
    weekly_ids = {item.get("scan_id") for item in weekly}
    historical_ids = {item.get("scan_id") for item in historical}

    partition_details: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    used_weekly_ids: set[str] = set()
    used_historical_ids: set[str] = set()
    weekly_denominator = 0
    weekly_numerator = 0
    weekly_anomaly_total = 0

    for partition in universe:
        covering = [item for item in rows if partition in item.get("partitions", [])]
        week_candidates = [item for item in covering if item.get("scan_id") in weekly_ids]
        history_candidates = [item for item in covering if item.get("scan_id") in historical_ids]
        chosen_week = _latest(week_candidates)
        chosen_history = _latest(history_candidates)

        if chosen_week is not None:
            weekly_denominator += 1
            anomaly_count = partition_anomaly_count(chosen_week, partition)
            weekly_anomaly_total += anomaly_count
            verdict = "异常" if anomaly_count > 0 else "通过"
            if anomaly_count == 0:
                weekly_numerator += 1
            used_weekly_ids.add(chosen_week["scan_id"])
            basis = WEEKLY_BASIS
            collected_on = chosen_week["collected_on"]
            scan_ids = [chosen_week["scan_id"]]
        elif chosen_history is not None:
            anomaly_count = partition_anomaly_count(chosen_history, partition)
            verdict = "异常" if anomaly_count > 0 else "通过"
            basis = HISTORICAL_BASIS
            collected_on = chosen_history["collected_on"]
            scan_ids = [chosen_history["scan_id"]]
            used_historical_ids.add(chosen_history["scan_id"])
        else:  # pragma: no cover - universe 由证据自身构成，不会走到这里
            verdict = "无依据"
            basis = "无"
            collected_on = ""
            scan_ids = []
            anomaly_count = 0

        if len(week_candidates) > 1:
            conflicts.append({
                "partition": partition,
                "type": "同周多次扫描",
                "scan_ids": [item["scan_id"] for item in week_candidates],
                "dates": sorted({item["collected_on"] for item in week_candidates}),
                "resolution": "同周内采用采集日期最新的一份，其余保留待复核",
            })

        if chosen_week is not None and chosen_history is not None:
            week_anomaly = partition_anomaly_count(chosen_week, partition)
            history_anomaly = partition_anomaly_count(chosen_history, partition)
            conflicts.append({
                "partition": partition,
                "type": "适用时间冲突",
                "scan_ids": [chosen_week["scan_id"], chosen_history["scan_id"]],
                "dates": sorted({chosen_week["collected_on"], chosen_history["collected_on"]}),
                "weekly_anomalies": week_anomaly,
                "historical_anomalies": history_anomaly,
                "result_conflict": (week_anomaly > 0) != (history_anomaly > 0),
                "resolution": "本周分数只采用本周采集的扫描；历史采集仅作复核依据，不并入本周分子分母",
            })

        partition_details.append({
            "partition": partition,
            "verdict": verdict,
            "basis": basis,
            "basis_scan_ids": scan_ids,
            "collected_on": collected_on,
            "anomalies": anomaly_count,
            "included_in_week_score": chosen_week is not None,
            "weekly_scan_ids": [item["scan_id"] for item in week_candidates],
            "historical_scan_ids": [item["scan_id"] for item in history_candidates],
            "weekly_collected_on": sorted({item["collected_on"] for item in week_candidates}),
            "historical_collected_on": sorted({item["collected_on"] for item in history_candidates}),
        })

    total_partitions = len(universe)
    coverage_label = f"{weekly_denominator}/{total_partitions}" if total_partitions else "0/0"
    covered_weekly = [detail["partition"] for detail in partition_details
                      if detail["included_in_week_score"]]
    uncovered = [partition for partition in universe if partition not in covered_weekly]
    historical_only = [detail["partition"] for detail in partition_details
                       if detail["basis"] == HISTORICAL_BASIS]

    if not rows:
        status = "未扫描"
        conclusion = "从未扫描：没有任何归属于该资产的扫描证据，不提供合规率。"
        accuracy_text = "未评估"
    elif weekly_denominator == 0:
        status = "本周未覆盖"
        conclusion = (
            "本周无扫描：仅有历史采集依据，不能计入本周分数；"
            "如需本周结论，应先补扫。"
        )
        accuracy_text = "未评估（本周无扫描，仅历史依据）"
    elif weekly_denominator < total_partitions:
        status = "部分覆盖"
        conclusion = (
            f"部分覆盖：本周仅扫描 {weekly_denominator}/{total_partitions} 个分区，"
            f"未覆盖分区 {', '.join(uncovered)} 不纳入本周结论，不能判定整表通过。"
        )
        accuracy_text = f"合规率:{weekly_numerator / weekly_denominator:.1%}（本周已扫描 {coverage_label} 分区）"
    else:
        status = "全量覆盖"
        if weekly_anomaly_total == 0:
            conclusion = (
                f"全量覆盖：本周扫描覆盖全部 {total_partitions} 个分区，未发现异常。"
            )
        else:
            conclusion = (
                f"全量覆盖：本周扫描覆盖全部 {total_partitions} 个分区，"
                f"其中 {len([d for d in partition_details if d['anomalies'] > 0])} 个分区发现异常。"
            )
        accuracy_text = f"合规率:{weekly_numerator / weekly_denominator:.1%}（本周已扫描 {coverage_label} 分区）"

    if conflicts and status not in ("未扫描",):
        conclusion += (
            f" 另有 {len(conflicts)} 处不同采集日期的可证依据冲突，"
            "已按本周/历史归属分别使用，详见证据明细。"
        )

    whole_table_passed = bool(
        total_partitions
        and weekly_denominator == total_partitions
        and weekly_anomaly_total == 0
    )

    evidence_views: list[dict[str, Any]] = []
    for item in rows:
        basis = WEEKLY_BASIS if item.get("scan_id") in weekly_ids else HISTORICAL_BASIS
        included = item.get("scan_id") in used_weekly_ids
        evidence_views.append(_evidence_view(item, basis, included))

    detail = {
        "asset_id": asset,
        "reference_date": reference.isoformat(),
        "week": window,
        "report_week": window["label"],
        "week_start": week_start,
        "week_end": week_end,
        "status": status,
        "证据状态": status,
        "accuracy": accuracy_text,
        "准确性": accuracy_text,
        "rate": (weekly_numerator / weekly_denominator) if weekly_denominator else None,
        "numerator": weekly_numerator,
        "denominator": weekly_denominator,
        "本周分子": weekly_numerator,
        "本周分母": weekly_denominator,
        "coverage": {
            "scanned": weekly_denominator,
            "total": total_partitions,
            "label": coverage_label,
            "covered_partitions": covered_weekly,
            "uncovered_partitions": uncovered,
            "historical_only_partitions": historical_only,
        },
        "覆盖率": coverage_label,
        "partial_coverage": bool(total_partitions) and weekly_denominator < total_partitions,
        "whole_table_passed": whole_table_passed,
        "conclusion": conclusion,
        "结论": conclusion,
        "this_week": {
            "iso_year": window["iso_year"],
            "iso_week": window["iso_week"],
            "label": window["label"],
            "start": week_start,
            "end": week_end,
            "numerator": weekly_numerator,
            "denominator": weekly_denominator,
            "rate": (weekly_numerator / weekly_denominator) if weekly_denominator else None,
            "partitions": covered_weekly,
            "scan_ids": sorted(weekly_ids),
            "anomaly_count": weekly_anomaly_total,
        },
        "history": {
            "scan_ids": sorted(historical_ids),
            "partitions": sorted({p for item in historical for p in item.get("partitions", [])}),
            "collected_on": sorted({item["collected_on"] for item in historical}),
            "used_scan_ids": sorted(used_historical_ids),
        },
        "partition_universe": universe,
        "partition_details": partition_details,
        "partitions": partition_details,
        "conflicts": conflicts,
        "冲突": conflicts,
        "evidence": evidence_views,
        "证据列表": evidence_views,
        "evidence_count": len(evidence_views),
        "historical_evidence_count": len(historical),
        "weekly_evidence_count": len(weekly),
        "uncovered_partitions": uncovered,
    }
    return detail


def find_partition_evidence(
    asset_id: str,
    partition: str,
    reference_date: Any = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """查询某分区在本周与历史采集中的可用依据，供分区复查使用。"""
    asset = str(asset_id or "").strip()
    partition = str(partition or "").strip()
    reference = parse_date(reference_date) or date.today()
    current_week = iso_week(reference)
    rows = load_evidence(asset) if evidence is None else evidence
    matching = [
        copy.deepcopy(item) for item in rows
        if same_asset(item.get("asset_id"), asset) and partition in item.get("partitions", [])
    ]
    weekly = [item for item in matching if iso_week(item.get("collected_on")) == current_week]
    historical = [item for item in matching if iso_week(item.get("collected_on")) != current_week]
    return {
        "asset_id": asset,
        "partition": partition,
        "week": week_window(reference),
        "weekly": weekly,
        "historical": historical,
        "latest_weekly": _latest(weekly),
        "latest_historical": _latest(historical),
        "available": bool(matching),
        "has_weekly": bool(weekly),
        "has_historical": bool(historical),
    }
