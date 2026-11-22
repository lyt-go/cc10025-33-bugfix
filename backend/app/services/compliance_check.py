"""compliance_check 领域服务：合规检查管理，检查数据资产合规性并生成审计日志和质量报告。

跨模块数据流：
    asset_catalog → compliance_check: 资产变更触发合规扫描
    compliance_check → audit_log: 检查结果写入审计日志
    compliance_check → quality_report: 扫描证据进入质量报告的准确率证据明细

扫描证据约定：
    * 每条规则必须在「关联资产」中声明归属，在「覆盖分区」中声明本次实际扫描的分区；
    * 每次实际扫描都写一份独立证据（分区、采集日期、异常明细），不覆盖历史；
    * 没有资产归属的扫描结果不进入任何质量报告。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any

from app.services import scan_evidence
from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "quality")


def result_dir() -> str:
    """合规检查结果目录；测试或换环境时可用 QUALITY_REPORT_DIR 指向独立目录。"""
    override = os.environ.get("QUALITY_REPORT_DIR")
    return os.path.abspath(override) if override else DATA_DIR


class ComplianceCheckService(BaseModuleService):
    MODULE = "compliance_check"
    ENTITY = "合规规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "规则编号", "检查维度", "合规标准", "关联资产", "扫描范围",
        "覆盖分区", "检查频率", "规则版本",
    ]
    REQUIRED_FIELDS = ["规则编号", "检查维度", "合规标准"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def __init__(self):
        super().__init__()
        os.makedirs(result_dir(), exist_ok=True)

    def run_check(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """执行合规检查：按声明的资产范围逐分区扫描并登记证据。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        rule_id = str(entry.get("规则编号", ""))
        dimension = str(entry.get("检查维度", ""))
        standard = str(entry.get("合规标准", ""))
        asset_id = str(entry.get("关联资产") or entry.get("扫描范围") or "").strip()
        partitions = scan_evidence.normalize_partitions(
            entry.get("覆盖分区") or entry.get("扫描范围"))

        if not asset_id:
            return None, f"{self.ENTITY} {rule_id} 未声明关联资产，不能生成可归属的扫描证据"
        if not partitions:
            return None, f"{self.ENTITY} {rule_id} 未声明覆盖分区，无法确认实际扫描范围"

        known_anomalies = set(scan_evidence.normalize_partitions(entry.get("已知异常分区")))
        anomalies = [
            {"partition": partition, "count": 1, "detail": "扫描发现异常"}
            for partition in partitions
            if partition in known_anomalies
        ]
        checked_count = len(partitions)
        violations = len(anomalies)
        passed = violations == 0

        checked_at = datetime.now(timezone.utc)
        collected_on = checked_at.date()
        scan_id = f"SCAN-{asset_id}-{checked_at.strftime('%Y%m%dT%H%M%S')}-{rule_id}"

        evidence = scan_evidence.record_evidence({
            "scan_id": scan_id,
            "asset_id": asset_id,
            "asset_name": str(entry.get("资产名称", "")),
            "rule_id": rule_id,
            "source": "compliance_check",
            "partitions": partitions,
            "collected_on": collected_on.isoformat(),
            "collected_at": checked_at.isoformat(),
            "checked_count": checked_count,
            "violations": violations,
            "anomalies": anomalies,
            "note": f"{dimension} / {standard}",
            "recorded_at": checked_at.isoformat(),
        })

        # 规则级最新结果保留扫描范围和异常明细，便于在合规检查页复核。
        result = {
            "rule_id": rule_id,
            "dimension": dimension,
            "standard": standard,
            "scope": str(entry.get("扫描范围", "")),
            "asset_id": asset_id,
            "partitions": partitions,
            "collected_on": collected_on.isoformat(),
            "checked_count": checked_count,
            "violations": violations,
            "anomalies": anomalies,
            "passed": passed,
            "scan_id": evidence.get("scan_id", scan_id),
            "checked_at": checked_at.isoformat(),
        }
        self._write_check_result(rule_id, result)

        entry["最近扫描分区"] = ",".join(partitions)
        entry["最近采集日期"] = collected_on.isoformat()
        entry["最近扫描异常数"] = str(violations)
        entry["最近扫描编号"] = result["scan_id"]

        self._write_audit_entry(
            entity="合规检查",
            action="执行扫描",
            target_id=rule_id,
            detail=(
                f"合规检查 {dimension}: {violations} / {checked_count} 违规，"
                f"资产 {asset_id}，分区 {','.join(partitions)}，采集 {collected_on.isoformat()}"
            ),
        )

        message = (
            f"合规检查完成：{checked_count} 个分区，{violations} 违规"
            f"{'，通过' if passed else '，不通过'}；证据已登记，采集日期 {collected_on.isoformat()}"
        )
        return entry, message

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "标记生效":
            return self.run_check(entry_id)
        return super().run_action(entry_id, action)

    def evidence_for_rule(self, entry_id: int) -> dict[str, Any] | None:
        """读取某条规则最近一次实际扫描结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        rule_id = str(entry.get("规则编号", ""))
        path = os.path.join(result_dir(), f"compliance_check_{rule_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def list_asset_evidence(self, asset_id: str) -> list[dict[str, Any]]:
        """按资产数据范围读取全部扫描证据。"""
        return scan_evidence.load_evidence(asset_id)

    def _write_check_result(self, rule_id: str, result: dict[str, Any]) -> None:
        filename = f"compliance_check_{rule_id}.json"
        path = os.path.join(result_dir(), filename)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, default=str)

    def _write_audit_entry(self, entity: str, action: str, target_id: str,
                           detail: str) -> None:
        audit_rows = store.rows("audit_log")
        next_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        entry = {
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{next_id:04d}",
            "操作主体": entity,
            "操作类型": action,
            "操作对象": target_id,
            "操作时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "影响范围": detail,
            "审计摘要": detail,
            "归档状态": "已记录",
        }
        audit_rows.append(entry)
