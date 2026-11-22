/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

export type PartitionEvidence = {
  partition: string
  verdict: string
  basis: string
  basis_scan_ids: string[]
  collected_on: string
  anomalies: number
  included_in_week_score: boolean
  weekly_scan_ids: string[]
  historical_scan_ids: string[]
  weekly_collected_on: string[]
  historical_collected_on: string[]
}

export type ScanEvidenceView = {
  scan_id: string
  rule_id: string
  asset_id: string
  partitions: string[]
  collected_on: string
  basis: string
  included_in_week_score: boolean
  checked_count: number
  violations: number
  anomalies: { partition: string; count: number; detail: unknown }[]
  passed: boolean
  note: string
}

export type EvidenceConflict = {
  partition: string
  type: string
  scan_ids: string[]
  dates: string[]
  weekly_anomalies?: number
  historical_anomalies?: number
  result_conflict?: boolean
  resolution: string
}

export type AccuracyEvidence = {
  report_id: string
  report_entry_id?: number
  asset_id: string
  reference_date: string
  report_week: string
  week_start: string
  week_end: string
  status: string
  证据状态: string
  accuracy: string
  准确性: string
  rate: number | null
  numerator: number
  denominator: number
  本周分子: number
  本周分母: number
  coverage: {
    scanned: number
    total: number
    label: string
    covered_partitions: string[]
    uncovered_partitions: string[]
    historical_only_partitions: string[]
  }
  覆盖率: string
  partial_coverage: boolean
  whole_table_passed: boolean
  conclusion: string
  结论: string
  partition_universe: string[]
  partition_details: PartitionEvidence[]
  conflicts: EvidenceConflict[]
  冲突: EvidenceConflict[]
  evidence: ScanEvidenceView[]
  证据列表: ScanEvidenceView[]
  evidence_count: number
  weekly_evidence_count: number
  historical_evidence_count: number
  uncovered_partitions: string[]
}
