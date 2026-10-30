<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ bins: Array<{min_price:number;max_price:number;count:number}> }>()
const emit=defineEmits<{(e:'select-range', min:number, max:number):void}>()
const maxCount=computed(()=>Math.max(1,...props.bins.map(b=>b.count)))
const step=computed(()=>640/Math.max(1,props.bins.length))
const height=(count:number)=>count/maxCount.value*180
const label=(bin:typeof props.bins[number])=>`¥${bin.min_price}～＜¥${bin.max_price}`
</script>
<template>
  <div>
    <p v-if="!bins.length" class="py-14 text-center text-sm text-slate-500">暂无有效价格，调整筛选条件或等待商品采集。</p>
    <svg v-else viewBox="0 0 720 260" class="w-full h-[280px] text-slate-500" role="img" aria-label="商品价格区间分布柱状图">
      <text x="10" y="15" font-size="12" fill="currentColor">商品数量</text>
      <line x1="50" y1="210" x2="700" y2="210" stroke="currentColor" opacity=".3"/>
      <g v-for="(bin,index) in bins" :key="bin.min_price" role="button" tabindex="0" :aria-label="`${label(bin)}，${bin.count}件`" class="cursor-pointer" @click="emit('select-range',bin.min_price,bin.max_price-.01)" @keydown.enter="emit('select-range',bin.min_price,bin.max_price-.01)" @keydown.space.prevent="emit('select-range',bin.min_price,bin.max_price-.01)">
        <rect :x="55+index*step" :y="210-height(bin.count)" :width="Math.max(2,step-8)" :height="Math.max(1,height(bin.count))" rx="4" fill="#0284c7"><title>{{label(bin)}}：{{bin.count}}件，点击筛选此区间</title></rect>
        <text :x="55+index*step+(step-8)/2" :y="200-height(bin.count)" text-anchor="middle" fill="currentColor" font-size="12">{{bin.count}}</text>
        <text :x="55+index*step+(step-8)/2" y="230" text-anchor="middle" fill="currentColor" font-size="10">{{bin.min_price}}</text>
        <text :x="55+index*step+(step-8)/2" y="245" text-anchor="middle" fill="currentColor" font-size="9">～{{bin.max_price}}</text>
      </g>
    </svg>
    <p class="text-xs text-slate-500">横轴为挂牌价格区间（元），纵轴为商品数量；点击柱子筛选该价位。</p>
  </div>
</template>
