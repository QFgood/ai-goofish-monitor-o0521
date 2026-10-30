<script setup lang="ts">
import type { ResultInsights } from '@/types/result.d.ts'
import PriceDistributionChart from './PriceDistributionChart.vue'
const props=defineProps<{insights:ResultInsights|null;selectedTaskLabel?:string|null}>()
const emit=defineEmits<{(e:'select-range',min:number,max:number):void}>()
const price=(value:number|null|undefined)=>value==null?'—':`¥${value.toLocaleString('zh-CN',{maximumFractionDigits:2})}`
</script>
<template>
  <section class="app-surface mb-6 p-5 sm:p-7 space-y-5" aria-label="结果商品价格分析">
    <div><p class="text-xs tracking-widest text-primary">RESULT PRICE DISTRIBUTION</p><h2 class="text-2xl font-semibold mt-2">{{selectedTaskLabel || '商品价格分析'}}</h2><p class="mt-2 text-sm text-slate-500">统计下方筛选后的全部商品，不受分页影响；展示挂牌价，不代表成交价。</p></div>
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <article class="app-surface-subtle p-4"><p class="text-sm text-slate-500">当前均价</p><p class="text-2xl font-semibold my-2">{{price(props.insights?.market_summary.avg_price)}}</p><p class="text-xs text-slate-500">有效价格 {{insights?.market_summary.sample_count || 0}} 件</p></article>
      <article class="app-surface-subtle p-4"><p class="text-sm text-slate-500">历史均价</p><p class="text-2xl font-semibold my-2">{{price(props.insights?.history_summary.avg_price)}}</p><p class="text-xs text-slate-500">近30天 · {{insights?.history_summary.sample_count || 0}} 个商品日样本</p></article>
      <article class="app-surface-subtle p-4"><p class="text-sm text-slate-500">当前价格范围</p><p class="text-xl font-semibold my-2">{{price(props.insights?.market_summary.min_price)}} ～ {{price(props.insights?.market_summary.max_price)}}</p><p class="text-xs text-slate-500">中位数 {{price(insights?.market_summary.median_price)}}</p></article>
    </div>
    <PriceDistributionChart :bins="insights?.price_distribution || []" @select-range="(min,max)=>emit('select-range',min,max)"/>
    <p v-if="insights?.price_filter?.excluded_count" class="text-sm text-amber-700 dark:text-amber-300">当前已排除 {{insights.price_filter.excluded_count}} 件不符价格范围或疑似低价引流商品；数据未删除，可调整筛选恢复查看。</p>
    <p class="text-xs text-slate-500">历史均价按当前筛选商品在最近快照起30天内、每天每商品最后观察价计算。低价判断是统计规则，不是确认商品真伪。</p>
  </section>
</template>
