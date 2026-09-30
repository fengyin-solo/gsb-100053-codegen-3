<template>
  <section class="page" data-module="quality_versions">
    <header class="page-head">
      <div>
        <h2>版本生效台</h2>
        <p class="page-desc">
          岩心质量阈值按版本审批生效：岩心按所属钻孔与取样深度区间套用规则，冲突时以最新审批生效版本为准。
          发布与早期岩心重算同一事务落库，任一岩心新版规则覆盖不到则整版回滚，绝不回落旧阈值。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">新建阈值版本</button>
        <button
          v-if="checkpoint"
          class="btn warn"
          type="button"
          :disabled="publishing"
          @click="publishVersion"
        >
          从 {{ checkpoint.failed_version }} 续发
        </button>
        <button v-else class="btn" type="button" :disabled="publishing" @click="publishVersion">
          发布已审批版本
        </button>
      </div>
    </header>

    <div v-if="checkpoint" class="callout warn">
      <strong>上一批发布已中断并整体回滚。</strong>
      失败版本：{{ checkpoint.failed_version }}；原因：{{ checkpoint.error }}
      续发将从失败那一版重新取，已回滚的岩心会整版重算，不会跳过、也不会回落旧阈值。
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>版本号</th>
          <th>状态</th>
          <th>规则数</th>
          <th>阈值规则（钻孔 / 深度区间 / 最低采取率）</th>
          <th>说明</th>
          <th>审批时间</th>
          <th>生效时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in versions" :key="row.版本号">
          <td>{{ row.版本号 }}</td>
          <td>
            <span :class="['tag', stateClass(row.状态)]">{{ row.状态 }}</span>
          </td>
          <td>{{ row.规则数 }}</td>
          <td class="rule-cell">
            <div v-for="rule in row.规则" :key="rule.id" class="rule-line">
              {{ rule.所属钻孔 === '*' ? '全部钻孔' : rule.所属钻孔 }}
              · {{ fmt(rule.深度起) }}~{{ fmt(rule.深度止) }}m
              · ≥ {{ fmt(rule.最低采取率) }}%
            </div>
          </td>
          <td>{{ row.说明 || '—' }}</td>
          <td>{{ row.审批时间 || '—' }}</td>
          <td>{{ row.生效时间 || '—' }}</td>
          <td class="row-actions">
            <template v-if="row.状态 === '草稿' || row.状态 === '已驳回'">
              <button class="link" type="button" @click="editRules(row)">改规则</button>
              <button class="link" type="button" @click="act(row.版本号, 'submit')">提交审批</button>
            </template>
            <template v-else-if="row.状态 === '待审批'">
              <button class="link" type="button" @click="act(row.版本号, 'approve')">审批通过</button>
              <button class="link danger" type="button" @click="rejectVersion(row.版本号)">驳回</button>
            </template>
            <template v-else-if="row.状态 === '已审批'">
              <span class="muted">待发布</span>
            </template>
            <template v-else>
              <span class="muted">生效留档不可改</span>
            </template>
          </td>
        </tr>
        <tr v-if="!versions.length">
          <td colspan="8" class="empty-state">还没有阈值版本，点右上角新建第一版口径</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ versions.length }} 个版本</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="editing" class="modal-mask" @click.self="closeEditor">
      <div class="modal">
        <h3>{{ editing.isCreate ? `新建阈值版本` : `编辑 ${editing.code} 规则` }}</h3>
        <label class="form-line">
          <span>版本号</span>
          <input v-model="editing.code" :disabled="!editing.isCreate" placeholder="例如 QY-V2" />
        </label>
        <label class="form-line">
          <span>口径说明</span>
          <input v-model="editing.note" placeholder="这次为什么调阈值" />
        </label>
        <div class="form-lines">
          <div class="rule-edit-head">
            <span>规则（所属钻孔填 * 表示全部钻孔；深度区间单位 m；阈值为最低采取率%）</span>
            <button class="btn small" type="button" @click="addRule">加一条</button>
          </div>
          <div v-for="(rule, index) in editing.rules" :key="index" class="rule-edit-row">
            <input v-model="rule.所属钻孔" placeholder="钻孔编号 / *" />
            <input v-model.number="rule.深度起" type="number" step="0.1" placeholder="深度起" />
            <input v-model.number="rule.深度止" type="number" step="0.1" placeholder="深度止" />
            <input v-model.number="rule.最低采取率" type="number" step="0.1" placeholder="最低采取率" />
            <button class="link danger" type="button" @click="editing.rules.splice(index, 1)">删除</button>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn" type="button" @click="closeEditor">取消</button>
          <button class="btn primary" type="button" @click="saveEditor">保存版本与规则</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Rule = { id?: number; 所属钻孔: string; 深度起: number | null; 深度止: number | null; 最低采取率: number | null }
type Version = {
  版本号: string
  状态: string
  规则数: number
  规则: Rule[]
  说明: string
  审批时间: string | null
  生效时间: string | null
}
type Checkpoint = {
  failed_version: string
  error: string
  queue: string[]
  attempts: number
} | null

const ENDPOINT = '/api/quality/versions'

const versions = ref<Version[]>([])
const checkpoint = ref<Checkpoint>(null)
const errorMessage = ref('')
const publishing = ref(false)
const editing = ref<{
  isCreate: boolean
  code: string
  note: string
  rules: Rule[]
} | null>(null)

const stats = computed(() => [
  { label: '已生效版本', value: versions.value.filter((v) => v.状态 === '已生效').length },
  { label: '待审批', value: versions.value.filter((v) => v.状态 === '待审批').length },
  { label: '待发布', value: versions.value.filter((v) => v.状态 === '已审批').length },
  { label: '中断批次', value: checkpoint.value ? checkpoint.value.queue.length : 0 },
])

function fmt(value: number | null): string {
  return value === null || value === undefined ? '—' : String(value)
}

function stateClass(state: string): string {
  if (state === '已生效') return 'tag-ok'
  if (state === '已驳回') return 'tag-bad'
  if (state === '待审批' || state === '已审批') return 'tag-warn'
  return 'tag-idle'
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT)
    if (!response.ok) throw new Error('版本生效台数据读取失败')
    const payload = await response.json()
    versions.value = payload.items ?? []
    checkpoint.value = payload.checkpoint ?? null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本生效台数据读取失败'
  }
}

async function callJson(path: string, body?: unknown) {
  const response = await request(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
  return response.json()
}

async function act(code: string, action: string) {
  errorMessage.value = ''
  try {
    const result = await callJson(`${ENDPOINT}/${code}/${action}`)
    if (!result.ok) errorMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function rejectVersion(code: string) {
  const reason = window.prompt(`驳回版本 ${code} 的原因？`, '规则依据不足')
  if (reason === null) return
  const result = await callJson(`${ENDPOINT}/${code}/reject`, { values: { reason } })
  if (!result.ok) errorMessage.value = result.message
  await reload()
}

async function publishVersion() {
  publishing.value = true
  errorMessage.value = ''
  try {
    const result = await callJson(`${ENDPOINT}/publish`, {})
    if (result.ok) {
      window.alert(result.message)
    } else {
      errorMessage.value = result.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布失败'
  } finally {
    publishing.value = false
  }
}

function openCreate() {
  editing.value = {
    isCreate: true,
    code: '',
    note: '',
    rules: [{ 所属钻孔: '*', 深度起: 0, 深度止: 200, 最低采取率: 90 }],
  }
}

function editRules(row: Version) {
  editing.value = {
    isCreate: false,
    code: row.版本号,
    note: row.说明 ?? '',
    rules: row.规则.map((rule) => ({ ...rule })),
  }
}

function addRule() {
  editing.value?.rules.push({ 所属钻孔: '*', 深度起: 0, 深度止: 200, 最低采取率: 90 })
}

function closeEditor() {
  editing.value = null
}

async function saveEditor() {
  if (!editing.value) return
  const { isCreate, code, note, rules } = editing.value
  errorMessage.value = ''
  try {
    const url = isCreate ? ENDPOINT : `${ENDPOINT}/${encodeURIComponent(code)}`
    const method = isCreate ? 'POST' : 'PUT'
    const response = await request(url, {
      method,
      body: JSON.stringify({ values: { 版本号: code, 说明: note }, rules }),
    })
    const result = await response.json()
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    editing.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本保存失败'
  }
}

onMounted(reload)
</script>
