<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const tree = ref<any[]>([])
const data = ref<any>(null)
const shortages = ref<any[]>([])
const orders = ref<any[]>([])
const error = ref('')
const inlineQty = ref<Record<number, string>>({})
const modalLine = ref<any>(null)
const modalQty = ref('')
const isVoided = () => data.value?.order?.status === 'voided'

// 同一口径:备料台行内改与行上弹层保存共用这一条校验
function validateQty(line: any, raw: string): { ok: boolean; qty?: number; msg?: string } {
  const qty = Number(raw)
  if (String(raw).trim() === '' || !Number.isFinite(qty)) return { ok: false, msg: '请输入有效数字' }
  if (qty < 0) return { ok: false, msg: '数量不能为负' }
  if (qty > line.need_qty) return { ok: false, msg: `超过该行当前需求 ${line.need_qty}` }
  return { ok: true, qty }
}

function errMsg(e: any): string {
  try {
    const d = JSON.parse(e?.message || '')
    if (d?.detail) return typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail)
  } catch { /* 非 JSON 错误体 */ }
  return e?.message || '保存失败'
}

function applyData(res: any) {
  data.value = res
  shortages.value = res.shortages || []
  const m: Record<number, string> = {}
  for (const l of res.prep_lines || []) m[l.ingredient_id] = String(l.prep_qty ?? l.need_qty)
  inlineQty.value = m
}

async function load() {
  error.value = ''
  applyData(await api('/prep/latest?order_id=1'))
}

async function regen() {
  error.value = ''
  applyData(await api('/prep/run?order_id=1', { method: 'POST' }))
}

async function saveQty(line: any, raw: string): Promise<boolean> {
  const v = validateQty(line, raw)
  if (!v.ok) { error.value = v.msg as string; return false }
  try {
    applyData(await api(`/prep/runs/${data.value.id}/lines/${line.ingredient_id}`, {
      method: 'PATCH',
      body: JSON.stringify({ qty: v.qty }),
    }))
    error.value = ''
    return true
  } catch (e: any) {
    error.value = errMsg(e)
    return false
  }
}

async function saveInline(line: any) {
  await saveQty(line, inlineQty.value[line.ingredient_id])
}

function openModal(line: any) {
  modalLine.value = line
  modalQty.value = String(line.prep_qty ?? line.need_qty)
}

async function saveModal() {
  if (await saveQty(modalLine.value, modalQty.value)) modalLine.value = null
}

onMounted(async () => {
  tree.value = await api('/bom/tree')
  orders.value = await api('/orders')
  await load()
})
</script>
<template>
  <h1>备料工作台</h1>
  <p class="sub">左 BOM 树 · 中备料表 · 右缺料便利贴 · 顶栏订单芯片</p>
  <div class="kp-chips" style="margin-bottom:0.75rem" v-if="orders.length">
    <span v-for="o in orders" :key="o.id" class="kp-chip" style="cursor:default">
      {{ o.code }} · {{ o.outlet }}<template v-if="o.status !== 'open'"> · {{ o.status }}</template>
    </span>
  </div>
  <button class="btn" :disabled="isVoided()" @click="regen">生成备料单</button>
  <p v-if="error" class="kp-error">⚠ {{ error }}</p>
  <div class="kp-workbench" style="margin-top:0.85rem">
    <aside class="kp-bom-tree">
      <h2>菜品 / BOM</h2>
      <div v-for="d in tree" :key="d.code" class="kp-dish-node">
        <strong>{{ d.dish }}</strong>
        <span style="font-size:0.7rem;color:#8a8078">{{ d.code }}</span>
        <ul>
          <li v-for="(c,i) in d.children" :key="i">{{ c.ingredient }} · {{ c.qty }} {{ c.unit }}</li>
        </ul>
      </div>
    </aside>
    <section class="kp-worksheet" v-if="data">
      <h2>
        备料单 #{{ data.id }} · {{ data.order?.code }} · {{ data.order?.outlet }}
        <span v-if="isVoided()" class="badge badge-bad">已作废</span>
      </h2>
      <p v-if="isVoided()" class="muted" style="font-size:0.8rem;margin:0.3rem 0 0">订单已作废，不能再手改。</p>
      <table>
        <thead>
          <tr><th>原料</th><th>需求</th><th>实备</th><th>占用</th><th>库存</th><th>缺料</th><th>单位</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="l in data.prep_lines" :key="l.ingredient_id">
            <td>{{ l.ingredient_name }}</td>
            <td>{{ l.need_qty }}</td>
            <td>
              <input
                class="kp-qty-input"
                v-model="inlineQty[l.ingredient_id]"
                :disabled="isVoided()"
                @keyup.enter="saveInline(l)"
              />
            </td>
            <td>{{ l.reserved_qty }}</td>
            <td>{{ l.stock_qty }}</td>
            <td><span v-if="l.shortage > 0" class="badge badge-bad">{{ l.shortage }}</span><span v-else>0</span></td>
            <td>{{ l.unit }}</td>
            <td style="white-space:nowrap">
              <button class="btn kp-btn-sm" :disabled="isVoided()" @click="saveInline(l)">保存</button>
              <button class="btn kp-btn-sm kp-btn-ghost" :disabled="isVoided()" @click="openModal(l)">弹层改</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
    <aside class="kp-shortage-sticky">
      <h2>⚠ 缺料便利贴</h2>
      <div v-for="r in shortages" :key="r.ingredient_id" class="kp-shortage-item">
        <span>{{ r.ingredient_name }}</span>
        <span class="kp-qty">−{{ r.shortage }} {{ r.unit }}</span>
      </div>
      <p v-if="!shortages.length" style="font-size:0.8rem;margin:0.5rem 0 0">暂无缺料</p>
    </aside>
  </div>
  <div v-if="modalLine" class="kp-modal-mask" @click.self="modalLine = null">
    <div class="kp-modal">
      <h3>手改 · {{ modalLine.ingredient_name }}</h3>
      <p class="muted" style="font-size:0.8rem;margin:0 0 0.5rem">
        需求 {{ modalLine.need_qty }} {{ modalLine.unit }} · 库存 {{ modalLine.stock_qty }} · 改后不得超过该行当前需求
      </p>
      <input class="kp-qty-input" v-model="modalQty" @keyup.enter="saveModal" />
      <div class="kp-modal-actions">
        <button class="btn" @click="saveModal">保存</button>
        <button class="btn kp-btn-ghost" @click="modalLine = null">取消</button>
      </div>
    </div>
  </div>
</template>
