<script setup lang="ts">
import type { ResultInsights } from '@/types/result.d.ts'
import PriceDistributionChart from './PriceDistributionChart.vue'
const props=defineProps<{insights:ResultInsights|null;selectedTaskLabel?:string|null;listingState?:'active'|'sold'}>()
const emit=defineEmits<{(e:'select-range',min:number,max:number):void;(e:'change-state',state:'active'|'sold'):void}>()
const runStatus=(status:string)=>({completed:'已完成',access_paused:'登录或访问受限，回访已暂停，请更新登录态后重新启用',failed:'失败，队列保留等待重试',interrupted:'中断，队列保留',running:'运行中'}[status] || status)
const price=(value:number|null|undefined)=>value==null?'—':`¥${value.toLocaleString('zh-CN',{maximumFractionDigits:2})}`
</script>
<template>
  <section class="app-surface mb-6 p-5 sm:p-7 space-y-5 text-slate-800 dark:text-slate-100" aria-label="结果商品价格分析">
    <div class="flex gap-2" role="group" aria-label="在售与已售统计切换">
      <button type="button" class="rounded-lg border px-4 py-2" :aria-pressed="listingState!=='sold'" :class="{'bg-primary text-white':listingState!=='sold'}" @click="emit('change-state','active')">在售挂牌</button>
      <button type="button" class="rounded-lg border px-4 py-2" :aria-pressed="listingState==='sold'" :class="{'bg-primary text-white':listingState==='sold'}" @click="emit('change-state','sold')">已售展示价</button>
    </div>
    <p v-if="listingState!=='sold'" class="text-xs text-slate-500 dark:text-slate-300">在售视图也包含尚未回访确认的搜索挂牌；未找到商品或检查失败不会被记作已售。</p>
    <p v-if="listingState==='sold'" class="text-sm text-slate-500 dark:text-slate-300">仅统计明确确认已售商品的商品页展示价格，不代表实际成交价；缺少展示价格的商品不计入均价与柱状图。首次确认已售日期不等于成交日期。</p>
    <div><p class="text-xs tracking-widest text-primary">RESULT PRICE DISTRIBUTION</p><h2 class="text-2xl font-semibold mt-2">{{selectedTaskLabel || '商品价格分析'}}</h2><p class="mt-2 text-sm text-slate-500 dark:text-slate-300">统计下方筛选后的全部商品，不受分页影响；展示挂牌价，不代表成交价。</p></div>
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <article class="app-surface-subtle p-4"><p class="text-sm text-slate-500 dark:text-slate-300">{{listingState==='sold'?'已售均价':'当前均价'}}</p><p class="text-2xl font-semibold my-2">{{price(props.insights?.market_summary.avg_price)}}</p><p class="text-xs text-slate-500 dark:text-slate-300">有效价格 {{insights?.market_summary.sample_count || 0}} 件</p></article>
      <article class="app-surface-subtle p-4"><p class="text-sm text-slate-500 dark:text-slate-300">{{listingState==='sold'?'已售历史均价':'历史均价'}}</p><p class="text-2xl font-semibold my-2">{{price(props.insights?.history_summary.avg_price)}}</p><p class="text-xs text-slate-500 dark:text-slate-300">近30天 · {{insights?.history_summary.sample_count || 0}} 个商品日样本</p></article>
      <article class="app-surface-subtle p-4"><p class="text-sm text-slate-500 dark:text-slate-300">{{listingState==='sold'?'已售价格范围':'当前价格范围'}}</p><p class="text-xl font-semibold my-2">{{price(props.insights?.market_summary.min_price)}} ～ {{price(props.insights?.market_summary.max_price)}}</p><p class="text-xs text-slate-500 dark:text-slate-300">中位数 {{price(insights?.market_summary.median_price)}}</p></article>
    </div>
    <details v-if="insights?.revisit_runs?.length" class="text-sm text-slate-500 dark:text-slate-300"><summary class="cursor-pointer">最近回访结果</summary><ul class="mt-2 space-y-2"><li v-for="run in insights.revisit_runs" :key="run.run_id">{{run.task_name || '已删除任务'}} · {{run.started_at}} · {{runStatus(run.status)}} · 检查 {{run.reviewed}} 件 / 新确认已售 {{run.sold}} 件</li></ul></details>
    <PriceDistributionChart :bins="insights?.price_distribution || []" @select-range="(min,max)=>emit('select-range',min,max)"/>
    <p v-if="insights?.price_filter?.excluded_count" class="text-sm text-amber-700 dark:text-amber-300">当前已排除 {{insights.price_filter.excluded_count}} 件不符价格范围或疑似低价引流商品；数据未删除，可调整筛选恢复查看。</p>
    <p v-if="insights?.missing_price_count" class="text-sm text-amber-700">{{insights.missing_price_count}} 件商品缺少有效展示价，均价及柱状图未计入。</p>
    <p v-if="listingState==='sold'" class="text-xs text-slate-500 dark:text-slate-300">已售历史均价按最近30天每商品每天最后一次明确已售页面价格统计，不混入在售挂牌价。</p>
    <p v-else class="text-xs text-slate-500 dark:text-slate-300">历史均价按当前筛选商品在最近快照起30天内、每天每商品最后观察价计算。低价判断是统计规则，不是确认商品真伪。</p>
  </section>
</template>
