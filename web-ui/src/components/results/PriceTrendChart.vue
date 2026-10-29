<script setup lang="ts">
import { computed } from 'vue'
import { trendPath } from '@/utils/marketChart'
import { useI18n } from 'vue-i18n'

interface TrendPoint {
  day: string
  avg_price: number | null
  median_price: number | null
}

const props = defineProps<{
  points: TrendPoint[]
  series?: Array<{ key: string; label: string; color: string; values: Array<number | null>; counts: number[] }>
}>()
const { t } = useI18n()

const chartWidth = 720
const chartHeight = 220
const padding = 24

const validPoints = computed(() =>
  props.points.filter((point) => point.avg_price !== null && point.avg_price !== undefined)
)

const valueRange = computed(() => {
  const values = validPoints.value
    .flatMap((point) => [point.avg_price, point.median_price])
    .filter((value): value is number => typeof value === 'number')
  if (values.length === 0) {
    return { min: 0, max: 1 }
  }
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (min === max) {
    return { min: min - 1, max: max + 1 }
  }
  return { min, max }
})

function resolveX(index: number) {
  if (validPoints.value.length <= 1) return chartWidth / 2
  const usableWidth = chartWidth - padding * 2
  return padding + (usableWidth / (validPoints.value.length - 1)) * index
}

function resolveY(value: number) {
  const usableHeight = chartHeight - padding * 2
  const ratio = (value - valueRange.value.min) / (valueRange.value.max - valueRange.value.min)
  return chartHeight - padding - ratio * usableHeight
}

function buildPath(values: Array<number | null>) {
  const commands = values
    .map((value, index) => {
      if (value === null || value === undefined) return null
      const prefix = index === 0 ? 'M' : 'L'
      return `${prefix} ${resolveX(index)} ${resolveY(value)}`
    })
    .filter(Boolean)
  return commands.join(' ')
}

const avgPath = computed(() => buildPath(validPoints.value.map((point) => point.avg_price)))
const medianPath = computed(() => buildPath(validPoints.value.map((point) => point.median_price)))
const areaPath = computed(() => {
  if (!avgPath.value || validPoints.value.length === 0) return ''
  const firstX = resolveX(0)
  const lastX = resolveX(validPoints.value.length - 1)
  const baseline = chartHeight - padding
  return `${avgPath.value} L ${lastX} ${baseline} L ${firstX} ${baseline} Z`
})
const marketValues = computed(() => props.series?.flatMap(s => s.values).filter((v): v is number => v !== null && Number.isFinite(v)) || [])
const marketRange = computed(() => {
  const values = marketValues.value
  const min = values.length ? Math.min(...values) : 0
  const max = values.length ? Math.max(...values) : 1
  return { min: min === max ? min - 1 : min, max: min === max ? max + 1 : max }
})
const marketX = (index: number) => padding + index * (chartWidth - padding * 2) / Math.max(1, props.points.length - 1)
const marketY = (value: number) => chartHeight - padding - (value-marketRange.value.min) / (marketRange.value.max-marketRange.value.min) * (chartHeight-padding*2)
</script>

<template>
  <div class="app-surface-subtle p-4">
    <template v-if="series">
      <div class="flex flex-wrap gap-4 text-sm mb-3">
        <span v-for="line in series" :key="line.key" :style="{color: line.color}">{{ line.label }}</span>
      </div>
      <p v-if="marketValues.length === 0" class="py-12 text-center text-slate-500">暂无有效样本，价格保留为空；请等待采集或调整日期。</p>
      <svg v-else :viewBox="`0 0 ${chartWidth} ${chartHeight}`" class="w-full h-[240px]" role="img" aria-label="市场价格曲线（元），空值处断开">
        <text :x="padding" y="14" font-size="11" fill="currentColor">{{ marketRange.max.toFixed(0) }} 元</text>
        <text :x="padding" :y="chartHeight-25" font-size="11" fill="currentColor">{{ marketRange.min.toFixed(0) }} 元</text>
        <g v-for="line in series" :key="line.key">
          <path :d="trendPath(line.values, marketX, marketY)" fill="none" :stroke="line.color" stroke-width="2.5" />
          <template v-for="(value, index) in line.values" :key="index">
            <circle v-if="value !== null" :cx="marketX(index)" :cy="marketY(value)" r="4" :fill="line.color" tabindex="0">
              <title>{{ points[index]?.day }} · {{ line.label }} · ¥{{ value }} · 样本 {{ line.counts[index] || 0 }} 件</title>
            </circle>
          </template>
        </g>
        <template v-for="(point,index) in points" :key="point.day">
          <text v-if="index % Math.max(1,Math.ceil(points.length/7)) === 0 || index === points.length-1" :x="marketX(index)" :y="chartHeight-3" text-anchor="middle" font-size="11" fill="currentColor">{{ point.day.slice(5) }}</text>
        </template>
      </svg>
    </template>
    <template v-else>
    <div class="mb-3 flex flex-col gap-3 text-xs uppercase tracking-[0.22em] text-slate-500 sm:flex-row sm:items-center sm:justify-between">
      <span>Daily Price Curve</span>
      <div class="flex items-center gap-3">
        <span class="inline-flex items-center gap-1">
          <span class="h-2.5 w-2.5 rounded-full bg-sky-600" />
          {{ t('results.chart.avgPrice') }}
        </span>
        <span class="inline-flex items-center gap-1">
          <span class="h-2.5 w-2.5 rounded-full bg-amber-500" />
          {{ t('results.chart.medianPrice') }}
        </span>
      </div>
    </div>

    <div v-if="validPoints.length === 0" class="rounded-2xl border border-dashed border-slate-200 bg-white/70 px-4 py-10 text-center text-sm text-slate-500">
      {{ t('results.chart.noTrend') }}
    </div>

    <div v-else>
      <svg :viewBox="`0 0 ${chartWidth} ${chartHeight}`" class="h-[220px] w-full" role="img" :aria-label="t('results.chart.noTrend')">
        <defs>
          <linearGradient id="avg-area-fill" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stop-color="#0284c7" stop-opacity="0.24" />
            <stop offset="100%" stop-color="#0284c7" stop-opacity="0" />
          </linearGradient>
        </defs>

        <g>
          <line
            v-for="index in 4"
            :key="index"
            :x1="padding"
            :x2="chartWidth - padding"
            :y1="padding + ((chartHeight - padding * 2) / 4) * (index - 1)"
            :y2="padding + ((chartHeight - padding * 2) / 4) * (index - 1)"
            stroke="#cbd5e1"
            stroke-dasharray="4 6"
          />
        </g>

        <path :d="areaPath" fill="url(#avg-area-fill)" />
        <path :d="avgPath" fill="none" stroke="#0284c7" stroke-width="4" stroke-linecap="round" />
        <path :d="medianPath" fill="none" stroke="#f59e0b" stroke-width="3" stroke-dasharray="8 6" stroke-linecap="round" />

        <g v-for="(point, index) in validPoints" :key="point.day">
          <circle :cx="resolveX(index)" :cy="resolveY(point.avg_price as number)" r="5" fill="#0284c7" />
          <circle :cx="resolveX(index)" :cy="resolveY(point.median_price as number)" r="4" fill="#f59e0b" />
          <text
            :x="resolveX(index)"
            :y="chartHeight - 6"
            text-anchor="middle"
            fill="#64748b"
            font-size="12"
          >
            {{ point.day.slice(5) }}
          </text>
        </g>
      </svg>
    </div>
    </template>
  </div>
</template>
