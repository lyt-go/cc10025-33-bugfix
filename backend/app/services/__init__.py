"""跨模块服务容器：提供统一的交叉索引和上游消费入口。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.asset_catalog import AssetCatalogService
from app.services.tier_policy import TierPolicyService
from app.services.lineage_graph import LineageGraphService
from app.services.retention_rule import RetentionRuleService
from app.services.compliance_check import ComplianceCheckService
from app.services.cold_storage import ColdStorageService
from app.services.hot_prefetch import HotPrefetchService
from app.services.version_snapshot import VersionSnapshotService
from app.services.label_tag import LabelTagService
from app.services.audit_log import AuditLogService
from app.services.access_gate import AccessGateService
from app.services.cost_tracker import CostTrackerService
from app.services.schema_registry import SchemaRegistryService
from app.services.partition_plan import PartitionPlanService
from app.services.backfill_job import BackfillJobService
from app.services.quality_report import QualityReportService
from app.services.reclaim_job import ReclaimJobService
from app.services.change_notice import ChangeNoticeService

_SERVICE_CLASSES = [
    AssetCatalogService,
    TierPolicyService,
    LineageGraphService,
    RetentionRuleService,
    ComplianceCheckService,
    ColdStorageService,
    HotPrefetchService,
    VersionSnapshotService,
    LabelTagService,
    AuditLogService,
    AccessGateService,
    CostTrackerService,
    SchemaRegistryService,
    PartitionPlanService,
    BackfillJobService,
    QualityReportService,
    ReclaimJobService,
    ChangeNoticeService,
]

_INSTANCES: dict[str, object] = {}

for cls in _SERVICE_CLASSES:
    instance = cls()
    _INSTANCES[cls.MODULE] = instance


def service_for(module: str) -> object | None:
    """按模块key返回领域服务实例。"""
    return _INSTANCES.get(module)


def all_services() -> list:
    return list(_INSTANCES.values())


# 演示用的扫描证据（本周增量 + 上周历史）在服务加载时补齐；已有证据不会被覆盖。
from app.services import scan_evidence as _scan_evidence  # noqa: E402

_scan_evidence.ensure_seed_evidence()


__all__ = [
    "service_for",
    "all_services",
    "AssetCatalogService",
    "TierPolicyService",
    "LineageGraphService",
    "RetentionRuleService",
    "ComplianceCheckService",
    "ColdStorageService",
    "HotPrefetchService",
    "VersionSnapshotService",
    "LabelTagService",
    "AuditLogService",
    "AccessGateService",
    "CostTrackerService",
    "SchemaRegistryService",
    "PartitionPlanService",
    "BackfillJobService",
    "QualityReportService",
    "ReclaimJobService",
    "ChangeNoticeService",
]