"""质量准确率证据与分区复查回归测试。

覆盖场景：
    * 同一资产旧扫描覆盖三个分区、本周扫描只覆盖两个分区且发现一个异常；
    * 历史证据可证明第三个分区，但采集日期不同，不得混入本周分数；
    * 从未扫描的资产不得借用别处的合规率；
    * 从报告提交分区复查时，必须注明并保存本周/历史依据。
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

WORKDIR = tempfile.mkdtemp(prefix="quality-evidence-")
REPORTDIR = tempfile.mkdtemp(prefix="quality-reports-")
os.environ["QUALITY_EVIDENCE_DIR"] = WORKDIR
os.environ["QUALITY_REPORT_DIR"] = REPORTDIR

from app.services import scan_evidence, service_for  # noqa: E402
from app.store import store  # noqa: E402

PASS = 0
FAIL = 0


def check(desc: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        print(f"  PASS {desc}")
        PASS += 1
        return True
    print(f"  FAIL {desc} {detail}")
    FAIL += 1
    return False


def reset_store() -> None:
    from app.seed import SEED_ROWS

    for module, rows in SEED_ROWS.items():
        store.rows(module).clear()
        store.rows(module).extend(copy.deepcopy(rows))


def reset_evidence() -> None:
    for name in os.listdir(WORKDIR):
        os.remove(os.path.join(WORKDIR, name))


ASSET = "tbl_orders"
OTHER_ASSET = "tbl_users"
PART_A, PART_B, PART_C = "dt=2026-10-01", "dt=2026-10-02", "dt=2026-10-03"
HIST_DATE = "2026-11-10"
WEEK_DATE = "2026-11-18"
REFERENCE = "2026-11-22"  # 2026-W47


def seed_evidence() -> None:
    scan_evidence.record_evidence({
        "scan_id": "OLD-3P",
        "asset_id": ASSET,
        "partitions": [PART_A, PART_B, PART_C],
        "collected_on": HIST_DATE,
        "checked_count": 3,
        "violations": 0,
    })
    scan_evidence.record_evidence({
        "scan_id": "NEW-2P",
        "asset_id": ASSET,
        "partitions": [PART_A, PART_B],
        "collected_on": WEEK_DATE,
        "checked_count": 2,
        "violations": 1,
        "anomalies": [{"partition": PART_B, "count": 1}],
    })
    # 另一个资产的历史扫描，用于验证不会“借”到本资产。
    scan_evidence.record_evidence({
        "scan_id": "OTHER-CLEAN",
        "asset_id": OTHER_ASSET,
        "partitions": ["dt=2026-10-01", "dt=2026-10-02", "dt=2026-10-03"],
        "collected_on": HIST_DATE,
        "checked_count": 3,
        "violations": 0,
    })


def test_week_split():
    print("\n=== 本周/历史证据分离 ===")
    detail = scan_evidence.build_accuracy_evidence(ASSET, reference_date=REFERENCE)
    check("状态为部分覆盖", detail["证据状态"] == "部分覆盖", detail["证据状态"])
    check("本周分子=1", detail["本周分子"] == 1, str(detail["本周分子"]))
    check("本周分母=2", detail["本周分母"] == 2, str(detail["本周分母"]))
    check("覆盖率=2/3", detail["覆盖率"] == "2/3", detail["覆盖率"])
    check("部分覆盖不能整表通过", detail["whole_table_passed"] is False)
    check("异常分区被识别", detail["this_week"]["anomaly_count"] == 1)
    uncovered = detail["uncovered_partitions"]
    check("未覆盖分区为第三个分区", uncovered == [PART_C], str(uncovered))
    check("结论明确不能冒充整表通过", "不能判定整表通过" in detail["结论"])

    partitions = {item["partition"]: item for item in detail["partition_details"]}
    check("第三分区依据为历史", partitions[PART_C]["basis"] == scan_evidence.HISTORICAL_BASIS)
    check("第三分区采集日期与本周不同",
          partitions[PART_C]["collected_on"] == HIST_DATE, partitions[PART_C]["collected_on"])
    check("第三分区不计入本周分数",
          partitions[PART_C]["included_in_week_score"] is False)
    check("本周分区只引用本周扫描",
          partitions[PART_A]["basis_scan_ids"] == ["NEW-2P"])

    conflicts = [item for item in detail["conflicts"] if item["type"] == "适用时间冲突"]
    check("记录不同采集日期的依据冲突", len(conflicts) == 2, str(len(conflicts)))
    check("第二分区结果存在冲突",
          any(c["partition"] == PART_B and c["result_conflict"] for c in conflicts))


def test_no_borrowed_rate():
    print("\n=== 从未扫描资产不借合规率 ===")
    detail = scan_evidence.build_accuracy_evidence("tbl_never_scanned",
                                                   reference_date=REFERENCE)
    check("状态为未扫描", detail["证据状态"] == "未扫描", detail["证据状态"])
    check("准确性为未评估", detail["准确性"] == "未评估", detail["准确性"])
    check("本周分母为 0", detail["本周分母"] == 0)
    check("没有覆盖分区", detail["coverage"]["total"] == 0)
    check("证据列表为空", detail["evidence_count"] == 0)
    check("没有可判定通过", detail["whole_table_passed"] is False)


def test_compliance_run_records_metadata():
    print("\n=== 合规检查记录分区/采集日期/异常 ===")
    rule = next(row for row in store.rows("compliance_check")
                if row.get("规则编号") == "COMP-0002")
    svc = service_for("compliance_check")
    entry, message = svc.run_check(rule["id"])
    check("扫描执行成功", entry is not None, message)
    result = svc.evidence_for_rule(rule["id"])
    check("结果含资产归属", result.get("asset_id") == "dwd_order_detail", str(result.get("asset_id")))
    check("结果含覆盖分区",
          result.get("partitions") == ["dt=2026-10-01", "dt=2026-10-02"],
          str(result.get("partitions")))
    check("结果含采集日期", bool(result.get("collected_on")))
    check("结果含异常明细", isinstance(result.get("anomalies"), list))
    check("结果含异常数", result.get("violations") == 1, str(result.get("violations")))
    evidence = scan_evidence.load_evidence("dwd_order_detail")
    check("证据已落盘且带 scan_id",
          any(item["scan_id"] == result["scan_id"] for item in evidence))
    check("未归属的扫描不会登记",
          scan_evidence.normalize_evidence({"partitions": [PART_A], "collected_on": HIST_DATE}) is None)


def test_report_and_recheck():
    print("\n=== 报告生成与分区复查 ===")
    quality = store.rows("quality_report")
    quality.clear()
    quality.extend([
        {"id": 1, "status": "生成中", "pending": True, "abnormal": False,
         "报告编号": "QUAL-T1", "关联资产": ASSET, "检核时间": "2026-11-16"},
        {"id": 2, "status": "生成中", "pending": True, "abnormal": False,
         "报告编号": "QUAL-T2", "关联资产": "tbl_never_scanned", "检核时间": "2026-11-16"},
    ])
    svc = service_for("quality_report")

    report, message = svc.generate_report(1, reference_date=REFERENCE)
    check("订单报告生成成功", report is not None, message)
    check("报告准确性含覆盖范围", "2/3" in str(report.get("准确性")), str(report.get("准确性")))
    check("报告记录本周分母", report.get("本周分母") == "2", str(report.get("本周分母")))
    check("报告记录本周分子", report.get("本周分子") == "1", str(report.get("本周分子")))

    never, message = svc.generate_report(2, reference_date=REFERENCE)
    check("未扫描报告生成成功", never is not None, message)
    check("未扫描报告准确性为未评估", never.get("准确性") == "未评估", str(never.get("准确性")))
    check("未扫描报告分母为 0", never.get("本周分母") == "0")

    comparison = svc.compare_reports([1, 2], reference_date=REFERENCE)
    rows = {row["entry_id"]: row for row in comparison["reports"]}
    check("对比中订单报告分母为 2", rows[1]["本周分母"] == 2, str(rows[1]["本周分母"]))
    check("对比中未扫描报告分母为 0", rows[2]["本周分母"] == 0, str(rows[2]["本周分母"]))
    check("未扫描报告没有借来的百分比", rows[2]["borrowed_percentage"] is False)

    rejected, message = svc.submit_partition_recheck(
        1, PART_C, "本周", reference_date=REFERENCE)
    check("第三分区不能凭历史冒充本周复查", rejected is None, message)
    check("提示先补扫", "补扫" in message, message)

    record, message = svc.submit_partition_recheck(
        1, PART_C, "历史", reviewer="复核员甲",
        note="引用上周采集", reference_date=REFERENCE)
    check("可以凭注明日期的历史依据发起复查", record is not None, message)
    check("保存依据类型", record.get("依据类型") == scan_evidence.HISTORICAL_BASIS)
    check("保存采集日期", record.get("采集日期") == HIST_DATE, str(record.get("采集日期")))
    check("历史复查不并入本周", record.get("进入本周分数") is False)

    weekly, message = svc.submit_partition_recheck(
        1, PART_B, "本周", reference_date=REFERENCE)
    check("本周依据复查成功", weekly is not None, message)
    check("本周复查并入本周分数", weekly.get("进入本周分数") is True)

    missing, message = svc.submit_partition_recheck(
        1, PART_C, "", reference_date=REFERENCE)
    check("未注明依据类型被拒绝", missing is None, message)

    listed = svc.list_rechecks(1)
    check("复查记录已保存", len(listed) == 2, str(len(listed)))

    # 路由参数入口
    routed, message = svc.run_action_with_params(
        1, "提交分区复查",
        {"partition": PART_A, "basis": "本周", "reference_date": REFERENCE})
    check("动作参数可提交分区复查", routed is not None, message)


def test_old_report_recompute_keeps_original():
    print("\n=== 旧报告复算保留原始证据 ===")
    quality = store.rows("quality_report")
    quality.clear()
    quality.append({
        "id": 1, "status": "已生成", "pending": False, "abnormal": False,
        "报告编号": "QUAL-OLD", "关联资产": ASSET, "检核时间": "2026-11-09",
    })
    svc = service_for("quality_report")
    # 2026-11-09 属于 W46，只有 OLD-3P 在本周内。
    first, message = svc.generate_report(1)
    check("旧报告首次生成成功", first is not None, message)
    check("按报告自身周结算", first.get("本周分母") == "3", str(first.get("本周分母")))
    saved = svc.get_accuracy_evidence(1)["saved"]
    original_evidence = saved.get("原始证据", [])
    check("报告保存原始证据", len(original_evidence) == 2, str(len(original_evidence)))

    second, message = svc.generate_report(1)
    check("旧报告复算成功", second is not None, message)
    saved_again = svc.get_accuracy_evidence(1)["saved"]
    revisions = saved_again.get("revisions", [])
    check("复算后旧快照进入 revisions", len(revisions) == 1, str(len(revisions)))
    check("旧快照保留原始证据",
          len(revisions[0].get("原始证据", [])) == 2)
    check("复算结果仍按同一周",
          saved_again["week_metrics"]["label"] == "2026-W46",
          saved_again["week_metrics"]["label"])


def test_partial_scan_cannot_fake_pass():
    print("\n=== 部分覆盖不能冒充整表通过 ===")
    # 先有一份历史全量扫描确定分区全集，本周只复扫了一个分区。
    scan_evidence.record_evidence({
        "scan_id": "PARTIAL-OLD",
        "asset_id": "tbl_partial",
        "partitions": [PART_A, PART_B, PART_C],
        "collected_on": HIST_DATE,
        "checked_count": 3,
        "violations": 0,
    })
    scan_evidence.record_evidence({
        "scan_id": "ONLY-1P",
        "asset_id": "tbl_partial",
        "partitions": [PART_A],
        "collected_on": WEEK_DATE,
        "checked_count": 1,
        "violations": 0,
    })
    detail = scan_evidence.build_accuracy_evidence("tbl_partial", reference_date=REFERENCE)
    check("部分覆盖状态", detail["证据状态"] == "部分覆盖", detail["证据状态"])
    check("本周分母只算本周扫描", detail["本周分母"] == 1, str(detail["本周分母"]))
    check("不能判定整表通过", detail["whole_table_passed"] is False)
    check("结论提示不能冒充整表通过", "不能判定整表通过" in detail["结论"])


def main() -> int:
    try:
        reset_store()
        reset_evidence()
        seed_evidence()
        test_week_split()
        test_no_borrowed_rate()
        test_compliance_run_records_metadata()
        test_report_and_recheck()
        test_old_report_recompute_keeps_original()
        test_partial_scan_cannot_fake_pass()
    finally:
        shutil.rmtree(WORKDIR, ignore_errors=True)
        shutil.rmtree(REPORTDIR, ignore_errors=True)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总计")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
