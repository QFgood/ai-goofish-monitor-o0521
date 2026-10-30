<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Task, TaskGenerateRequest } from '@/types/task.d.ts'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { toast } from '@/components/ui/toast'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import TaskRegionSelector from '@/components/tasks/TaskRegionSelector.vue'
import RevisitSchedulePicker from '@/components/tasks/RevisitSchedulePicker.vue'

type FormMode = 'create' | 'edit'
type EmittedData = TaskGenerateRequest | Partial<Task>
const AUTO_ACCOUNT_VALUE = '__auto__'

const props = defineProps<{
  mode: FormMode
  initialData?: Task | null
  accountOptions?: { name: string; path: string }[]
  defaultAccount?: string
  defaultValues?: Partial<TaskGenerateRequest & Partial<Task>>
}>()

const emit = defineEmits<{
  (e: 'submit', data: EmittedData): void
}>()
const { t } = useI18n()

const form = ref<any>({})
const accountStrategy = ref<'auto' | 'fixed' | 'rotate'>('auto')
const selectedAccountStateFile = ref(AUTO_ACCOUNT_VALUE)
const keywordRulesInput = ref('')
const accountStrategyOptions = computed(() => [
  { value: 'auto', label: t('tasks.form.accountStrategy.auto'), description: t('tasks.form.accountStrategy.autoDescription') },
  { value: 'fixed', label: t('tasks.form.accountStrategy.fixed'), description: t('tasks.form.accountStrategy.fixedDescription') },
  { value: 'rotate', label: t('tasks.form.accountStrategy.rotate'), description: t('tasks.form.accountStrategy.rotateDescription') },
])

function parseKeywordText(text: string): string[] {
  const values = String(text || '')
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0)

  const seen = new Set<string>()
  const deduped: string[] = []
  for (const value of values) {
    const key = value.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    deduped.push(value)
  }
  return deduped
}

watch(() => [props.mode, props.initialData, props.defaultValues, props.defaultAccount], () => {
  const defaultValues = props.defaultValues || {}
  if (props.mode === 'edit' && props.initialData) {
    form.value = {
      ...props.initialData,
      ...defaultValues,
      account_strategy:
        defaultValues.account_strategy ||
        props.initialData.account_strategy ||
        (props.initialData.account_state_file ? 'fixed' : 'auto'),
      account_state_file:
        defaultValues.account_state_file ||
        props.initialData.account_state_file ||
        AUTO_ACCOUNT_VALUE,
      analyze_images: defaultValues.analyze_images ?? props.initialData.analyze_images ?? true,
      free_shipping: defaultValues.free_shipping ?? props.initialData.free_shipping ?? true,
      new_publish_option:
        defaultValues.new_publish_option || props.initialData.new_publish_option || '__none__',
      region: defaultValues.region || props.initialData.region || '',
      decision_mode: defaultValues.decision_mode || props.initialData.decision_mode || 'ai',
    }
    keywordRulesInput.value = (defaultValues.keyword_rules || props.initialData.keyword_rules || []).join('\n')
  } else {
    form.value = {
      task_name: '',
      keyword: '',
      description: '',
      analyze_images: true,
      max_pages: 3,
      personal_only: true,
      min_price: undefined,
      max_price: undefined,
      cron: '',
      revisit_enabled: false, revisit_cron: '30 9 * * *', revisit_limit: 20, revisit_days: 30,
      account_strategy: props.defaultAccount ? 'fixed' : 'auto',
      account_state_file: props.defaultAccount || AUTO_ACCOUNT_VALUE,
      free_shipping: true,
      new_publish_option: '__none__',
      region: '',
      decision_mode: 'ai',
      ...defaultValues,
    }
    if (!form.value.account_strategy) {
      form.value.account_strategy = props.defaultAccount ? 'fixed' : 'auto'
    }
    if (!form.value.account_state_file) {
      form.value.account_state_file = props.defaultAccount || AUTO_ACCOUNT_VALUE
    }
    if (!form.value.new_publish_option) {
      form.value.new_publish_option = '__none__'
    }
    keywordRulesInput.value = ''
    if (defaultValues.keyword_rules && defaultValues.keyword_rules.length > 0) {
      keywordRulesInput.value = defaultValues.keyword_rules.join('\n')
    }
  }

  form.value.revisit_enabled ??= false
  form.value.revisit_cron ??= '30 9 * * *'
  form.value.revisit_limit ??= 20
  form.value.revisit_days ??= 30
  accountStrategy.value = form.value.account_strategy || (props.defaultAccount ? 'fixed' : 'auto')
  selectedAccountStateFile.value =
    form.value.account_state_file || props.defaultAccount || AUTO_ACCOUNT_VALUE
}, { immediate: true, deep: true })

watch(accountStrategy, (value) => {
  form.value.account_strategy = value
  if (value === 'fixed') {
    form.value.account_state_file = selectedAccountStateFile.value || props.defaultAccount || AUTO_ACCOUNT_VALUE
    return
  }
  form.value.account_state_file = null
})

watch(selectedAccountStateFile, (value) => {
  if (accountStrategy.value !== 'fixed') return
  form.value.account_state_file = value || props.defaultAccount || AUTO_ACCOUNT_VALUE
})

function handleAccountStrategyChange(event: Event) {
  const value = (event.target as HTMLSelectElement).value as 'auto' | 'fixed' | 'rotate'
  accountStrategy.value = value
}

function handleAccountStateFileChange(event: Event) {
  selectedAccountStateFile.value = (event.target as HTMLSelectElement).value || AUTO_ACCOUNT_VALUE
}

function handleSubmit() {
  if (!form.value.task_name || !form.value.keyword) {
    toast({
      title: t('tasks.form.validation.incomplete'),
      description: t('tasks.form.validation.nameAndKeywordRequired'),
      variant: 'destructive',
    })
    return
  }

  const decisionMode = form.value.decision_mode || 'ai'
  if (decisionMode === 'ai' && !String(form.value.description || '').trim()) {
    toast({
      title: t('tasks.form.validation.incomplete'),
      description: t('tasks.form.validation.aiDescriptionRequired'),
      variant: 'destructive',
    })
    return
  }

  const keywordRules = parseKeywordText(keywordRulesInput.value)
  if (decisionMode === 'keyword' && keywordRules.length === 0) {
    toast({
      title: t('tasks.form.validation.keywordRuleIncomplete'),
      description: t('tasks.form.validation.keywordRuleRequired'),
      variant: 'destructive',
    })
    return
  }

  // Filter out fields that shouldn't be sent in update requests
  const { id, is_running, next_run_at, ...submitData } = form.value as any
  const currentAccountStrategy = accountStrategy.value || 'auto'
  if (currentAccountStrategy === 'fixed') {
    const currentAccountStateFile = selectedAccountStateFile.value || AUTO_ACCOUNT_VALUE
    if (currentAccountStateFile === AUTO_ACCOUNT_VALUE) {
      toast({
        title: t('tasks.form.validation.accountStrategyIncomplete'),
        description: t('tasks.form.validation.fixedAccountRequired'),
        variant: 'destructive',
      })
      return
    }
    submitData.account_state_file = currentAccountStateFile
  } else {
    submitData.account_state_file = null
  }

  if (typeof submitData.region === 'string') {
    const normalized = submitData.region
      .trim()
      .split('/')
      .map((part: string) => part.trim().replace(/(省|市)$/u, ''))
      .filter((part: string) => part.length > 0)
      .join('/')
    submitData.region = normalized
  }

  if (submitData.new_publish_option === '__none__') {
    submitData.new_publish_option = ''
  }

  submitData.decision_mode = decisionMode
  submitData.account_strategy = currentAccountStrategy
  submitData.analyze_images = submitData.analyze_images !== false
  submitData.keyword_rules = decisionMode === 'keyword' ? keywordRules : []
  if (decisionMode === 'keyword' && !submitData.description) {
    submitData.description = ''
  }

  emit('submit', submitData)
}
</script>

<template>
  <form id="task-form" @submit.prevent="handleSubmit">
    <div class="grid gap-6 py-4">
      <fieldset class="rounded-xl border border-slate-200 p-4 space-y-3">
        <legend class="px-2 font-medium">已登记商品回访</legend>
        <label class="flex items-center gap-2"><input v-model="form.revisit_enabled" type="checkbox" />启用定时回访</label>
        <p class="text-xs text-slate-500">只检查该任务已登记的商品链接，不重新搜索。新登记商品首次回访最早从次日开始，按所选日期和时间执行；已售停止回访。时间为上海时区。</p>
        <div v-if="form.revisit_enabled" class="grid gap-3 sm:grid-cols-2">
          <RevisitSchedulePicker v-model="form.revisit_cron" class="sm:col-span-2" />
          <div><Label for="revisit-limit">每次最多检查商品数</Label><Input id="revisit-limit" v-model.number="form.revisit_limit" type="number" min="1" max="100" required /></div>
          <div><Label for="revisit-days">追踪天数</Label><Input id="revisit-days" v-model.number="form.revisit_days" type="number" min="1" max="365" required /></div>
        </div>
      </fieldset>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label for="task-name" class="sm:text-right">{{ t('tasks.form.taskName') }}</Label>
        <Input id="task-name" v-model="form.task_name" class="sm:col-span-3" :placeholder="t('tasks.form.taskNamePlaceholder')" required />
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label for="keyword" class="sm:text-right">{{ t('tasks.form.keyword') }}</Label>
        <Input id="keyword" v-model="form.keyword" class="sm:col-span-3" :placeholder="t('tasks.form.keywordPlaceholder')" required />
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label class="sm:text-right">{{ t('tasks.form.decisionMode') }}</Label>
        <div class="sm:col-span-3">
          <Select v-model="form.decision_mode">
            <SelectTrigger>
              <SelectValue :placeholder="t('tasks.form.decisionModePlaceholder')" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="ai">{{ t('tasks.form.aiMode') }}</SelectItem>
              <SelectItem value="keyword">{{ t('tasks.form.keywordMode') }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label for="description" class="sm:text-right">{{ t('tasks.form.description') }}</Label>
        <div class="space-y-1 sm:col-span-3">
          <Textarea
            id="description"
            v-model="form.description"
            :placeholder="t('tasks.form.descriptionPlaceholder')"
          />
          <p v-if="form.decision_mode === 'keyword'" class="text-xs text-gray-500">
            {{ t('tasks.form.keywordDescriptionHint') }}
          </p>
        </div>
      </div>
      <div v-if="form.decision_mode === 'ai'" class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label for="analyze-images" class="sm:text-right">{{ t('tasks.form.analyzeImages') }}</Label>
        <div class="space-y-1 sm:col-span-3">
          <Switch id="analyze-images" v-model="form.analyze_images" />
          <p class="text-xs text-gray-500">
            {{ t('tasks.form.analyzeImagesHint') }}
          </p>
        </div>
      </div>

      <div v-if="form.decision_mode === 'keyword'" class="grid gap-2 sm:grid-cols-4 sm:gap-4">
        <Label class="pt-1 sm:pt-2 sm:text-right">{{ t('tasks.form.keywordRules') }}</Label>
        <div class="space-y-2 sm:col-span-3">
          <p class="text-xs text-gray-500">
            {{ t('tasks.form.keywordRulesHint') }}
          </p>
          <Textarea
            v-model="keywordRulesInput"
            class="min-h-[120px]"
            :placeholder="t('tasks.form.keywordRulesPlaceholder')"
          />
        </div>
      </div>

      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label class="sm:text-right">{{ t('tasks.form.priceRange') }}</Label>
        <div class="grid grid-cols-[1fr_auto_1fr] items-center gap-2 sm:col-span-3">
          <Input type="number" v-model="form.min_price as any" :aria-label="t('tasks.form.minPrice')" :placeholder="t('tasks.form.minPrice')" />
          <span>-</span>
          <Input type="number" v-model="form.max_price as any" :aria-label="t('tasks.form.maxPrice')" :placeholder="t('tasks.form.maxPrice')" />
        </div>
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label for="max-pages" class="sm:text-right">{{ t('tasks.form.maxPages') }}</Label>
        <Input id="max-pages" v-model.number="form.max_pages" type="number" class="sm:col-span-3" />
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label class="sm:text-right">采集定时设置</Label>
        <RevisitSchedulePicker v-model="form.cron" id-prefix="collection" label-prefix="采集" allow-manual class="sm:col-span-3" />
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label class="sm:text-right">{{ t('tasks.form.accountStrategyLabel') }}</Label>
        <div class="space-y-2 sm:col-span-3">
          <select
            :value="accountStrategy"
            class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
            @change="handleAccountStrategyChange"
          >
            <option v-for="option in accountStrategyOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
          <p class="text-xs text-gray-500">
            {{ accountStrategyOptions.find((option) => option.value === accountStrategy)?.description }}
          </p>
        </div>
      </div>
      <div v-if="accountStrategy === 'fixed'" class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label class="sm:text-right">{{ t('tasks.form.fixedAccount') }}</Label>
        <div class="sm:col-span-3">
          <select
            :value="selectedAccountStateFile"
            class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
            @change="handleAccountStateFileChange"
          >
            <option :value="AUTO_ACCOUNT_VALUE">{{ t('tasks.form.selectAccount') }}</option>
            <option v-for="account in accountOptions || []" :key="account.path" :value="account.path">
              {{ account.name }}
            </option>
          </select>
        </div>
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label for="personal-only" class="sm:text-right">{{ t('tasks.form.personalOnly') }}</Label>
        <div class="sm:col-span-3">
          <Switch id="personal-only" v-model="form.personal_only" />
        </div>
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label for="free-shipping" class="sm:text-right">{{ t('tasks.form.freeShipping') }}</Label>
        <div class="sm:col-span-3">
          <Switch id="free-shipping" v-model="form.free_shipping" />
        </div>
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label class="sm:text-right">{{ t('tasks.form.newPublish') }}</Label>
        <div class="sm:col-span-3">
          <Select v-model="form.new_publish_option as any">
            <SelectTrigger>
              <SelectValue :placeholder="t('tasks.form.publishOptions.none')" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">{{ t('tasks.form.publishOptions.none') }}</SelectItem>
              <SelectItem value="最新">{{ t('tasks.form.publishOptions.latest') }}</SelectItem>
              <SelectItem value="1天内">{{ t('tasks.form.publishOptions.oneDay') }}</SelectItem>
              <SelectItem value="3天内">{{ t('tasks.form.publishOptions.threeDays') }}</SelectItem>
              <SelectItem value="7天内">{{ t('tasks.form.publishOptions.sevenDays') }}</SelectItem>
              <SelectItem value="14天内">{{ t('tasks.form.publishOptions.fourteenDays') }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <div class="grid gap-2 sm:grid-cols-4 sm:items-center sm:gap-4">
        <Label class="sm:text-right">{{ t('tasks.form.region') }}</Label>
        <div class="space-y-1 sm:col-span-3">
          <TaskRegionSelector v-model="form.region as any" />
          <p class="text-xs text-gray-500">{{ t('tasks.form.regionHint') }}</p>
        </div>
      </div>
    </div>
  </form>
</template>
