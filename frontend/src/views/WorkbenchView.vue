<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="isQualityReport"
              class="link"
              type="button"
              :data-testid="`evidence-${row.id}`"
              @click="openEvidence(row)"
            >
              查看证据明细
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <section v-if="isQualityReport && evidence" class="evidence-panel" data-testid="accuracy-evidence">
      <header class="evidence-head">
        <div>
          <h3>质量准确率证据明细 · {{ evidence.report_id }} / {{ evidence.asset_id }}</h3>
          <p class="page-desc">
            报告周 {{ evidence.report_week }}（{{ evidence.week_start }} ~ {{ evidence.week_end }}），
            {{ evidence.证据状态 }}：本周分子/分母 = {{ evidence.本周分子 }}/{{ evidence.本周分母 }}，
            覆盖 {{ evidence.覆盖率 }} 分区。
          </p>
        </div>
        <button class="btn ghost" type="button" data-testid="evidence-close" @click="evidence = null">收起</button>
      </header>

      <p class="evidence-conclusion" data-testid="evidence-conclusion">{{ evidence.结论 }}</p>

      <table class="data-table" data-testid="partition-evidence">
        <thead>
          <tr>
            <th>分区</th><th>结论</th><th>依据归属</th><th>采集日期</th>
            <th>异常数</th><th>本周计入</th><th>依据扫描</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="detail in evidence.partition_details"
            :key="detail.partition"
            :data-testid="`partition-${detail.partition}`"
            :data-basis="detail.basis"
          >
            <td>{{ detail.partition }}</td>
            <td>{{ detail.verdict }}</td>
            <td>{{ detail.basis }}</td>
            <td>{{ detail.collected_on || '—' }}</td>
            <td>{{ detail.anomalies }}</td>
            <td>{{ detail.included_in_week_score ? '是' : '否（历史不并入本周）' }}</td>
            <td>{{ detail.basis_scan_ids.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="evidence.conflicts.length" class="evidence-conflicts" data-testid="evidence-conflicts">
        <h4>采集日期冲突（{{ evidence.conflicts.length }}）</h4>
        <ul>
          <li v-for="conflict in evidence.conflicts" :key="`${conflict.partition}-${conflict.type}`">
            {{ conflict.partition }} · {{ conflict.type }}：{{ conflict.dates.join(' / ') }}，
            {{ conflict.resolution }}
          </li>
        </ul>
      </div>

      <form class="recheck-form" data-testid="recheck-form" @submit.prevent="submitRecheck">
        <label>
          <span>复查分区</span>
          <select v-model="recheck.partition" :data-testid="'recheck-partition'">
            <option value="">请选择分区</option>
            <option v-for="detail in evidence.partition_details" :key="detail.partition" :value="detail.partition">
              {{ detail.partition }}（{{ detail.basis }} / {{ detail.collected_on || '无依据' }}）
            </option>
          </select>
        </label>
        <label>
          <span>本次依据</span>
          <select v-model="recheck.basis" :data-testid="'recheck-basis'">
            <option value="历史">历史依据（不并入本周分数）</option>
            <option value="本周">本周扫描（计入本周分数）</option>
          </select>
        </label>
        <label>
          <span>复核员</span>
          <input v-model="recheck.reviewer" :data-testid="'recheck-reviewer'" placeholder="复核员" />
        </label>
        <label>
          <span>备注</span>
          <input v-model="recheck.note" :data-testid="'recheck-note'" placeholder="说明" />
        </label>
        <button class="btn primary" data-testid="recheck-submit" type="submit" :disabled="rechecking">
          {{ rechecking ? '提交中…' : '提交分区复查' }}
        </button>
      </form>
      <p v-if="recheckMessage" class="evidence-conclusion" data-testid="recheck-message">{{ recheckMessage }}</p>

      <h4>扫描证据（本周 {{ evidence.weekly_evidence_count }} / 历史 {{ evidence.historical_evidence_count }}）</h4>
      <table class="data-table" data-testid="scan-evidence">
        <thead>
          <tr>
            <th>扫描编号</th><th>规则</th><th>覆盖分区</th><th>采集日期</th>
            <th>归属</th><th>本周计入</th><th>异常数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in evidence.evidence" :key="item.scan_id" :data-testid="`scan-${item.scan_id}`">
            <td>{{ item.scan_id }}</td>
            <td>{{ item.rule_id || '—' }}</td>
            <td>{{ item.partitions.join('、') }}</td>
            <td>{{ item.collected_on }}</td>
            <td>{{ item.basis }}</td>
            <td>{{ item.included_in_week_score ? '是' : '否' }}</td>
            <td>{{ item.violations }}</td>
          </tr>
          <tr v-if="!evidence.evidence.length">
            <td colspan="7" class="empty-state">该资产从未扫描，没有可引用的扫描证据</td>
          </tr>
        </tbody>
      </table>
    </section>


    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadAccuracyEvidence,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { AccuracyEvidence, EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const isQualityReport = meta.key === 'quality_report'
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const evidence = ref<AccuracyEvidence | null>(null)
const activeReportId = ref(0)
const rechecking = ref(false)
const recheckMessage = ref('')
const recheck = ref({ partition: '', basis: '历史', reviewer: '', note: '' })
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (isQualityReport && activeReportId.value === Number(row.id)) {
      await openEvidence(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function openEvidence(row: EntryRow) {
  errorMessage.value = ''
  recheckMessage.value = ''
  activeReportId.value = Number(row.id)
  try {
    evidence.value = await loadAccuracyEvidence(Number(row.id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '证据明细读取失败'
  }
}

async function submitRecheck() {
  if (!evidence.value) return
  errorMessage.value = ''
  recheckMessage.value = ''
  rechecking.value = true
  try {
    const result = await applyAction(meta.key, activeReportId.value, '提交分区复查', {
      partition: recheck.value.partition,
      basis: recheck.value.basis,
      reviewer: recheck.value.reviewer,
      note: recheck.value.note,
    })
    if (!result.ok) throw new Error(result.message)
    recheckMessage.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区复查提交失败'
  } finally {
    rechecking.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
