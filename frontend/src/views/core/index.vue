<template>
  <section class="page" data-module="core">
    <header class="page-head">
      <div>
        <h2>岩心管理</h2>
        <p class="page-desc">
          岩心样本按所属钻孔与取样深度区间套用「版本生效台」的生效阈值；换版发布时已编录岩心按新口径重算并转入待复检，
          送样结论按送样当时版本冻结留档。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/quality_versions">前往版本生效台</RouterLink>
        <button class="btn" type="button" @click="exportRows">导出岩心管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>岩心编号</span>
        <input v-model="keyword" placeholder="按岩心编号检索" />
      </label>
      <label class="filter-item">
        <span>业务状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="state in statuses" :key="state" :value="state">{{ state }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section v-if="todoRows.length" class="todo-panel">
      <h3>编录待办（换版重算后待复检，共 {{ todoRows.length }} 件）</h3>
      <ul class="todo-list">
        <li v-for="row in todoRows" :key="`todo-${row.id}`">
          <span class="todo-code">{{ row['岩心编号'] }}</span>
          <span>{{ row['所属钻孔'] }} · {{ row['取样深度起'] }}~{{ row['取样深度止'] }}m</span>
          <span :class="['tag', row['检验结论'] === '合格' ? 'tag-ok' : 'tag-bad']">
            {{ row['检验结论'] }}（{{ row['判定版本号'] }}，阈值 ≥ {{ row['判定阈值'] }}%）
          </span>
          <button class="link" type="button" @click="runAction('复检确认', row)">复检确认</button>
        </li>
      </ul>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>业务状态</th>
          <th>检验结论 / 口径</th>
          <th>送样留档</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td><span :class="['tag', stateClass(row.status)]">{{ row.status }}</span></td>
          <td class="verdict-cell">
            <template v-if="row['判定版本号']">
              <span :class="['tag', row['检验结论'] === '合格' ? 'tag-ok' : 'tag-bad']">{{ row['检验结论'] }}</span>
              <span class="muted">{{ row['判定版本号'] }} · {{ row['判定规则'] }}</span>
            </template>
            <span v-else class="muted">未编录，暂无判定</span>
          </td>
          <td class="verdict-cell">
            <template v-if="row['送样冻结版本号']">
              <span class="muted">{{ row['送样编号'] }} · {{ row['送样冻结结论'] }} · {{ row['送样冻结版本号'] }}</span>
            </template>
            <span v-else class="muted">—</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actionsFor(row.status)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!actionsFor(row.status).length" class="muted">—</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无岩心样本</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条岩心记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Cell = string | number | boolean | null
type Row = Record<string, Cell>

const ENDPOINT = '/api/core'
const columns = ['岩心编号', '所属钻孔', '取样深度起', '取样深度止', '岩性描述', '采取率', '存放位置', '样本状态']
const statuses = ['待编录', '待复检', '已编录', '送样中', '已归还']
const actionByStatus: Record<string, string[]> = {
  待编录: ['地质编录'],
  待复检: ['复检确认'],
  已编录: ['送样分析'],
  送样中: ['归还原箱'],
  已归还: [],
}

const rows = ref<Row[]>([])
const todoRows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')

const stats = computed(() => [
  { label: '待编录', value: rows.value.filter((r) => r.status === '待编录').length },
  { label: '待复检（编录待办）', value: todoRows.value.length },
  { label: '送样中', value: rows.value.filter((r) => r.status === '送样中').length },
  { label: '不合格台账', value: rows.value.filter((r) => r['检验结论'] === '不合格').length },
])

function actionsFor(status: Cell): string[] {
  return actionByStatus[String(status)] ?? []
}

function stateClass(status: Cell): string {
  if (status === '已编录' || status === '已归还') return 'tag-ok'
  if (status === '待复检') return 'tag-warn'
  if (status === '送样中') return 'tag-info'
  return 'tag-idle'
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${String(row.id)}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const result = await response.json()
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '岩心操作失败'
  }
}

async function reloadTodos() {
  try {
    const response = await request(`${ENDPOINT}/review-todos`)
    if (!response.ok) return
    const payload = await response.json()
    todoRows.value = payload.items ?? []
  } catch {
    todoRows.value = []
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) throw new Error('岩心样本列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await reloadTodos()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '岩心管理列表读取失败'
  }
}

onMounted(reload)
</script>
