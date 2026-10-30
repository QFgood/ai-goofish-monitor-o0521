<script setup lang="ts">
import { computed } from 'vue'
import { parseRevisitSchedule, buildRevisitCron, describeRevisitSchedule, revisitFrequencies } from '@/lib/revisitSchedule'
import type { RevisitFrequency } from '@/lib/revisitSchedule'
const props = defineProps<{ modelValue: string }>()
const emit = defineEmits<{ (e:'update:modelValue', value:string):void }>()
const parsed = computed(() => parseRevisitSchedule(props.modelValue))
function update(part: {frequency?:RevisitFrequency;hour?:number;minute?:number}) {
  emit('update:modelValue',buildRevisitCron({...{frequency:'daily' as const,hour:9,minute:30},...parsed.value,...part}))
}
const frequency = computed({ get:()=>parsed.value?.frequency || 'custom', set:(value:string)=>update({frequency:value as RevisitFrequency}) })
const hour = computed({ get:()=>parsed.value?.hour ?? 9, set:(value:number)=>update({hour:value}) })
const minute = computed({ get:()=>parsed.value?.minute ?? 30, set:(value:number)=>update({minute:value}) })
const selectClass = 'w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-slate-800 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100'
</script>
<template>
  <div class="space-y-3">
    <div><label for="revisit-frequency" class="text-sm font-medium">回访频率</label>
      <select id="revisit-frequency" v-model="frequency" :class="selectClass">
        <option v-if="!parsed" value="custom" disabled>保留现有时间规则</option>
        <option v-for="option in revisitFrequencies" :key="option.value" :value="option.value">{{option.label}}</option>
      </select>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div><label for="revisit-hour" class="text-sm font-medium">小时</label><select id="revisit-hour" v-model.number="hour" :disabled="!parsed" :class="selectClass"><option v-for="h in 24" :key="h-1" :value="h-1">{{String(h-1).padStart(2,'0')}} 时</option></select></div>
      <div><label for="revisit-minute" class="text-sm font-medium">分钟</label><select id="revisit-minute" v-model.number="minute" :disabled="!parsed" :class="selectClass"><option v-for="m in 60" :key="m-1" :value="m-1">{{String(m-1).padStart(2,'0')}} 分</option></select></div>
    </div>
    <p class="text-xs text-slate-500 dark:text-slate-300" aria-live="polite">{{parsed ? describeRevisitSchedule(parsed) : '现有规则会原样保留；如需调整，请先选择回访频率。'}}</p>
  </div>
</template>
