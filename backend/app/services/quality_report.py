"""quality_report 领域服务：质量报告管理，接收 compliance_check 和 audit_log 上游事件。

跨模块数据流：
    asset_catalog → quality_report: 资产发布时生成质量报告种子
    compliance_check → quality_report: 实际扫描证据形成质量准确率证据明细
    audit_log → quality_report: 审计异常纳入质量报告

准确率口径：
    * 只读取严格归属于「关联资产」的扫描证据，不再从任意一份合规检查结果借合规率；
    * 本周分子 = 本周扫描覆盖且无异常的分区数，本周分母 = 本周扫描覆盖的分区数；
    * 部分覆盖不能判定整表通过；从未扫描的资产准确性为「未评估」；
    * 历史采集证据可复核、可支撑分区复查，但不并入本周分数；
    * 每次生成都把当时的原始证据快照留在报告文件里，复算时旧快照进 revisions。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from typing import Any

from app.services import scan_evidence
from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "quality")

BASIS_WEEKLY = scan_evidence.WEEKLY_BASIS
BASIS_HISTORICAL = scan_evidence.HISTORICAL_BASIS


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.\-一-鿿]+", "_", str(value or "")) or "UNKNOWN"


def report_dir() -> str:
    """报告落盘目录；测试或换环境时可用 QUALITY_REPORT_DIR 指向独立目录。"""
    override = os.environ.get("QUALITY_REPORT_DIR")
    return os.path.abspath(override) if override else DATA_DIR


class QualityReportService(BaseModuleService):
    MODULE = "quality_report"
    ENTITY = "质量报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "报告编号", "关联资产", "检核时间", "准确性", "完整性", "及时性",
        "本周分子", "本周分母", "证据状态", "异常数", "趋势方向",
    ]
    REQUIRED_FIELDS = ["报告编号", "关联资产", "检核时间"]
    STATUS_ORDER = ["生成中", "已生成", "已确认", "已归档"]
    ACTION_RULES = {"确认生成": "已生成", "标记确认": "已确认", "提交归档": "已归档"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(report_dir(), exist_ok=True)

    # ---------- 报告生成与复算 ----------

    def generate_report(self, entry_id: int,
                        reference_date: Any = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", "")).strip()
        if not asset_id:
            return None, f"{self.ENTITY} {entry_id} 未登记关联资产，无法按资产数据范围取证"

        # 未显式给参考日期时沿用报告自身的检核时间：复算旧报告仍按它所属的那一周结算，
        # 不会把旧报告悄悄改成本周口径。
        reference = scan_evidence.parse_date(reference_date) or scan_evidence.parse_date(
            entry.get("检核时间")) or date.today()
        detail = scan_evidence.build_accuracy_evidence(asset_id, reference_date=reference)
        raw_evidence = scan_evidence.snapshot_evidence(asset_id)

        entry["完整性"] = self._calc_completeness(asset_id, detail)
        entry["准确性"] = detail["准确性"]
        entry["及时性"] = self._timeliness(detail)
        entry["异常数"] = str(detail["this_week"]["anomaly_count"])
        entry["趋势方向"] = self._trend(detail)
        entry["本周分子"] = str(detail["本周分子"])
        entry["本周分母"] = str(detail["本周分母"])
        entry["覆盖率"] = detail["覆盖率"]
        entry["证据状态"] = detail["证据状态"]
        entry["结论"] = detail["结论"]
        entry["检核时间"] = detail["reference_date"]
        entry["status"] = "已生成"
        entry["pending"] = False
        entry["abnormal"] = detail["this_week"]["anomaly_count"] > 0

        record = self._persist_report(entry, asset_id, detail, raw_evidence)

        message = (
            f"{self.ENTITY} {entry_id} 已生成：{detail['证据状态']}，"
            f"本周分子/分母={detail['本周分子']}/{detail['本周分母']}，"
            f"覆盖 {detail['覆盖率']} 分区，异常数={detail['this_week']['anomaly_count']}"
        )
        return entry, message

    def get_accuracy_evidence(self, entry_id: int,
                              reference_date: Any = None) -> dict[str, Any] | None:
        """按报告关联资产重新读取证据并汇总，供复核查看。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        asset_id = str(entry.get("关联资产", "")).strip()
        report_ref = scan_evidence.parse_date(entry.get("检核时间"))
        detail = scan_evidence.build_accuracy_evidence(
            asset_id, reference_date=reference_date or report_ref)
        detail["report_id"] = str(entry.get("报告编号", ""))
        detail["report_entry_id"] = entry_id
        detail["saved"] = self._load_report_file(entry_id, asset_id)
        return detail

    def compare_reports(self, entry_ids: list[int],
                        reference_date: Any = None) -> dict[str, Any]:
        """并排比较多份报告的本周分子与分母，核对未扫描资产没有借来的百分比。"""
        rows: list[dict[str, Any]] = []
        for entry_id in entry_ids:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                rows.append({"entry_id": entry_id, "error": f"{self.ENTITY} {entry_id} 不存在"})
                continue
            asset_id = str(entry.get("关联资产", "")).strip()
            reference = reference_date or scan_evidence.parse_date(entry.get("检核时间"))
            detail = scan_evidence.build_accuracy_evidence(asset_id, reference_date=reference)
            saved = self._load_report_file(entry_id, asset_id) or {}
            rate = detail["this_week"]["rate"]
            rows.append({
                "entry_id": entry_id,
                "report_id": str(entry.get("报告编号", "")),
                "asset_id": asset_id,
                "证据状态": detail["证据状态"],
                "本周分子": detail["本周分子"],
                "本周分母": detail["本周分母"],
                "rate": rate,
                "rate_text": f"{rate:.1%}" if rate is not None else "无（未扫描/本周无扫描）",
                "准确性": detail["准确性"],
                "覆盖率": detail["覆盖率"],
                "whole_table_passed": detail["whole_table_passed"],
                "partial_coverage": detail["partial_coverage"],
                "reported_accuracy": str(entry.get("准确性", saved.get("accuracy", ""))),
                "borrowed_percentage": (
                    detail["证据状态"] == "未扫描"
                    and "%" in str(entry.get("准确性", ""))
                ),
            })
        return {"reports": rows, "count": len(rows)}

    # ---------- 分区复查 ----------

    def submit_partition_recheck(
        self,
        entry_id: int,
        partition: str,
        basis: str | None = None,
        *,
        reviewer: str = "",
        note: str = "",
        reference_date: Any = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """从报告提交一次分区复查，并保存本次实际使用的依据类型与采集日期。

        返回 (复查记录, 消息)。历史依据允许发起复查，但会标记为不并入本周分数；
        本周没有该分区扫描依据时会明确提示先补扫。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        asset_id = str(entry.get("关联资产", "")).strip()
        partition = str(partition or "").strip()
        if not partition:
            return None, "分区复查必须指定分区"

        found = scan_evidence.find_partition_evidence(
            asset_id, partition, reference_date=reference_date)
        if not found["available"]:
            return None, (
                f"资产 {asset_id} 的分区 {partition} 没有任何扫描依据，"
                "需先完成扫描后再提交复查"
            )

        requested = self._normalize_basis(basis)
        if requested is None:
            return None, "分区复查的依据类型必须注明为「本周」或「历史」"
        if requested == BASIS_WEEKLY and not found["has_weekly"]:
            collected = "、".join(sorted({
                item["collected_on"]
                for item in found["historical"]
            })) or "无"
            return None, (
                f"分区 {partition} 本周没有扫描依据，现有依据采集日期为 {collected}（历史）。"
                "如需计入本周，请先补扫取得本周来源后再发起复查；"
                "也可以按明确注明日期的历史依据提交复查，但不会并入本周分数。"
            )
        if requested == BASIS_HISTORICAL and not found["has_historical"]:
            return None, (
                f"分区 {partition} 没有历史采集依据，仅有本周扫描；"
                "请改为按「本周」依据提交复查"
            )

        if requested == BASIS_WEEKLY:
            chosen = found["latest_weekly"]
            enters_week_score = True
            basis_label = "本周"
        else:
            chosen = found["latest_historical"]
            enters_week_score = False
            basis_label = "历史"

        now = datetime.now(timezone.utc)
        saved = self._load_report_file(entry_id, asset_id) or {}
        rechecks = list(saved.get("rechecks", []))
        recheck = {
            "recheck_id": f"RECHK-{entry_id}-{len(rechecks) + 1:03d}",
            "report_id": str(entry.get("报告编号", "")),
            "report_entry_id": entry_id,
            "asset_id": asset_id,
            "partition": partition,
            "basis": requested,
            "依据类型": requested,
            "basis_label": basis_label,
            "scan_id": chosen.get("scan_id", ""),
            "collected_on": chosen.get("collected_on", ""),
            "采集日期": chosen.get("collected_on", ""),
            "partitions_in_scan": list(chosen.get("partitions", [])),
            "anomalies": scan_evidence.partition_anomaly_count(chosen, partition),
            "enters_week_score": enters_week_score,
            "进入本周分数": enters_week_score,
            "reviewer": reviewer,
            "note": note,
            "submitted_at": now.isoformat(),
            "reference_date": found["week"]["reference_date"],
            "week": found["week"]["label"],
        }
        rechecks.append(recheck)
        saved["rechecks"] = rechecks
        saved.setdefault("report_id", str(entry.get("报告编号", "")))
        saved.setdefault("asset_id", asset_id)
        self._write_report_file(entry_id, asset_id, saved)

        entry["最近复查分区"] = partition
        entry["最近复查依据"] = basis_label

        if enters_week_score:
            message = (
                f"分区 {partition} 复查已按本周依据（采集日期 {recheck['collected_on']}，"
                f"扫描 {recheck['scan_id']}）登记，可计入本周分数"
            )
        else:
            message = (
                f"分区 {partition} 复查已按历史依据登记（采集日期 {recheck['collected_on']}，"
                f"扫描 {recheck['scan_id']}）；历史采集不并入本周分子/分母。"
                "如需计入本周，请先补扫取得本周来源后再提交。"
            )
        return recheck, message

    def list_rechecks(self, entry_id: int) -> list[dict[str, Any]]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        saved = self._load_report_file(entry_id, str(entry.get("关联资产", ""))) or {}
        return list(saved.get("rechecks", []))

    # ---------- 动作分发 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认生成":
            return self.generate_report(entry_id)
        return super().run_action(entry_id, action)

    def run_action_with_params(
        self, entry_id: int, action: str, params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        params = params or {}
        if action == "提交分区复查":
            return self.submit_partition_recheck(
                entry_id,
                str(params.get("partition") or params.get("分区") or ""),
                params.get("basis") or params.get("依据") or params.get("依据类型"),
                reviewer=str(params.get("reviewer") or params.get("复核员") or ""),
                note=str(params.get("note") or params.get("备注") or ""),
                reference_date=params.get("reference_date") or params.get("参考日期"),
            )
        return self.run_action(entry_id, action)

    # ---------- 仪表盘 ----------

    def get_quality_dashboard(self) -> dict[str, Any]:
        """生成质量仪表盘汇总所有资产质量。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        abnormal = sum(1 for r in rows if r.get("abnormal"))
        never_scanned = 0
        for row in rows:
            asset_id = str(row.get("关联资产", "")).strip()
            if not scan_evidence.load_evidence(asset_id):
                never_scanned += 1

        summary = {
            "total_reports": total,
            "abnormal_assets": abnormal,
            "never_scanned_assets": never_scanned,
            "health_score": f"{max(0, 100 - abnormal * 10)}%",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = os.path.join(report_dir(), "quality_dashboard.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)

        return summary

    # ---------- 内部：指标 ----------

    def _calc_completeness(self, asset_id: str, detail: dict[str, Any]) -> str:
        """完整性：按资产自身数据范围判断证据覆盖情况。"""
        if detail["证据状态"] == "未扫描":
            return "未评估"
        total = len(detail["partition_universe"])
        covered = detail["this_week"]["denominator"]
        if total == 0:
            return "未评估"
        if covered == total:
            return "高" if detail["this_week"]["anomaly_count"] == 0 else "中"
        return "中" if covered > 0 else "低"

    def _timeliness(self, detail: dict[str, Any]) -> str:
        if detail["证据状态"] == "未扫描":
            return "未评估"
        if detail["this_week"]["denominator"] > 0:
            return "实时"
        return "滞后（仅有历史采集）"

    def _trend(self, detail: dict[str, Any]) -> str:
        if detail["证据状态"] == "未扫描":
            return "无依据"
        if detail["本周分母"] == 0:
            return "待补扫"
        return "恶化" if detail["this_week"]["anomaly_count"] > 0 else "稳定"

    def _normalize_basis(self, basis: Any) -> str | None:
        text = str(basis or "").strip().casefold()
        if text in {"本周", "本周扫描", "weekly", "week", "current"}:
            return BASIS_WEEKLY
        if text in {"历史", "历史证据", "历史采集", "historical", "history", "past"}:
            return BASIS_HISTORICAL
        return None

    # ---------- 内部：文件 ----------

    def _report_path(self, entry_id: int, asset_id: str) -> str:
        entry = store.find(self.MODULE, entry_id)
        report_id = _safe_name(str(entry.get("报告编号", ""))) if entry else str(entry_id)
        return os.path.join(
            report_dir(), f"quality_report_{report_id}_{_safe_name(asset_id)}.json")

    def _load_report_file(self, entry_id: int, asset_id: str) -> dict[str, Any] | None:
        path = self._report_path(entry_id, asset_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _write_report_file(self, entry_id: int, asset_id: str,
                           record: dict[str, Any]) -> None:
        path = self._report_path(entry_id, asset_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, default=str)

    def _persist_report(
        self,
        entry: dict[str, Any],
        asset_id: str,
        detail: dict[str, Any],
        raw_evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        report_id = str(entry.get("报告编号", ""))
        previous = self._load_report_file(int(entry.get("id", 0)), asset_id) or {}
        revisions = list(previous.get("revisions", []))
        if previous.get("generated_at"):
            archived = {
                key: value for key, value in previous.items() if key != "revisions"
            }
            archived["superseded_at"] = datetime.now(timezone.utc).isoformat()
            revisions.append(archived)

        record = {
            "report_id": report_id,
            "asset_id": asset_id,
            "reference_date": detail["reference_date"],
            "week": detail["week"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "check_time": datetime.now(timezone.utc).isoformat(),
            "status": detail["证据状态"],
            "accuracy": detail["准确性"],
            "准确性": detail["准确性"],
            "本周分子": detail["本周分子"],
            "本周分母": detail["本周分母"],
            "覆盖率": detail["覆盖率"],
            "conclusion": detail["结论"],
            "结论": detail["结论"],
            "week_metrics": {
                "iso_year": detail["week"]["iso_year"],
                "iso_week": detail["week"]["iso_week"],
                "label": detail["report_week"],
                "start": detail["week_start"],
                "end": detail["week_end"],
                "numerator": detail["本周分子"],
                "denominator": detail["本周分母"],
                "rate": detail["this_week"]["rate"],
                "partitions": detail["this_week"]["partitions"],
                "anomaly_count": detail["this_week"]["anomaly_count"],
            },
            "accuracy_evidence": detail,
            "原始证据": raw_evidence,
            "conflicts": detail["conflicts"],
            "rechecks": previous.get("rechecks", []),
            "revisions": revisions,
        }
        self._write_report_file(int(entry.get("id", 0)), asset_id, record)
        return record
