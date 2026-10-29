<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { TrendingUp, RefreshCw, Download, Plus } from 'lucide-vue-next'
import { http } from '@/lib/http'
import { Button } from '@/components/ui/button'
import PriceTrendChart from '@/components/results/PriceTrendChart.vue'
import { fenPrice } from '@/utils/marketChart'

type Row = Record<string, any>
const tasks=ref<Row[]>([]), items=ref<Row[]>([]), jobs=ref<Row[]>([]), runs=ref<Row[]>([])
const stats=ref<Row|null>(null), trends=ref<Row[]>([]), variants=ref<string[]>([]), comparison=ref<Row[]>([])
const loading=ref(false), busy=ref(false), error=ref(''), message=ref(''), total=ref(0), page=ref(1)
const variant=ref(''), region=ref(''), minPrice=ref(''), maxPrice=ref(''), includeExcluded=ref(false)
const days=ref('30'), metric=ref<'median_price'|'avg_price'>('median_price'), curve=ref('both'), rolling=ref(false)
const dateLabel=(date:Date)=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(date)
const end=ref(dateLabel(new Date())), start=ref(''), showCreate=ref(false), detail=ref<Row|null>(null)
const correction=ref({product_category:'',product_variant:'',excluded:false})
const form=ref({task_name:'',keyword:'',tracking_days:30,max_pages:1,account_state_file:'',discovery_time:'08:30',revisit_time:'09:30',notifications:true})
const labels:Record<string,string>={active:'在售',sold:'已售',offline:'下架',unknown:'无法确认',stale_unconfirmed:'长期未确认',pending:'等待回访',running:'执行中',done:'已完成',expired:'追踪期结束',success:'成功',failed:'失败',paused:'已暂停',interrupted:'执行中断，等待恢复'}
const status=(v:string)=>labels[v]||v||'—'
const field='w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-transparent px-3 py-2 text-sm'
let timer:ReturnType<typeof setInterval>|undefined
let generation=0
let variantInitialized=false
function range(){if(days.value!=='custom'){const d=new Date(`${end.value}T00:00:00+08:00`);d.setUTCDate(d.getUTCDate()-Number(days.value)+1);start.value=dateLabel(d)}}
range()
const filters=computed(()=>({product_variant:variant.value||undefined,region:region.value||undefined,min_price:minPrice.value?Math.round(Number(minPrice.value)*100):undefined,max_price:maxPrice.value?Math.round(Number(maxPrice.value)*100):undefined,include_excluded:includeExcluded.value}))
async function load(silent=false){
  const current=++generation;if(!silent)loading.value=true
  try{
    const global=await http('/api/market/stats',{params:{...filters.value,product_variant:undefined}});if(current!==generation)return
    variants.value=global.variants.map((v:Row)=>v.product_variant);comparison.value=global.variants
    if(!variantInitialized){variant.value=variants.value.find(v=>v!=='unknown')||'';variantInitialized=true}
    const [a,b,c,d,e,f]=await Promise.all([http('/api/market/tasks'),http('/api/market/items',{params:{...filters.value,page:page.value,limit:20}}),http('/api/market/stats',{params:filters.value}),http('/api/market/trends',{params:{...filters.value,start:start.value,end:end.value}}),http('/api/market/revisits'),http('/api/market/runs')])
    if(current!==generation)return
    tasks.value=a.items;items.value=b.items;total.value=b.total;stats.value=c;trends.value=d.items;jobs.value=e.items;runs.value=f.items;error.value=''
  }catch(e){if(current===generation)error.value=e instanceof Error?e.message:'读取行情失败'}finally{if(current===generation)loading.value=false}
}
async function action(work:()=>Promise<unknown>,note='操作已完成'){
  busy.value=true;error.value='';message.value=''
  try{await work();message.value=note;await load()}catch(e){error.value=e instanceof Error?e.message:'操作失败'}finally{busy.value=false}
}
const json=(body:unknown)=>({headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
async function create(){await action(async()=>{await http('/api/market/tasks',{method:'POST',...json({...form.value,account_state_file:form.value.account_state_file||null})});showCreate.value=false},'已创建独立市场任务，将按上海时区执行，也可手动触发首次搜索。')}
function toggleTask(task:Row){void action(()=>http(`/api/market/tasks/${task.id}`,{method:'PATCH',...json({enabled:!task.enabled})}))}
function removeTask(task:Row){if(window.confirm(`删除“${task.task_name}”？商品、价格及已售档案等历史将保留。`))void action(()=>http(`/api/market/tasks/${task.id}`,{method:'DELETE'}))}
async function showItem(entry:Row){try{detail.value=await http(`/api/market/items/${encodeURIComponent(entry.item_id)}/history`);const v=detail.value!.item;correction.value={product_category:v.product_category,product_variant:v.product_variant,excluded:!!v.excluded}}catch(e){error.value=e instanceof Error?e.message:'读取历史失败'}}
async function saveCorrection(){if(!detail.value)return;const id=detail.value.item.item_id;await action(async()=>{await http(`/api/market/items/${id}/classification`,{method:'PATCH',...json(correction.value)});detail.value=null},'人工更正已保存，操作历史已记录。')}
async function exportCSV(){await action(async()=>{const params=new URLSearchParams(Object.entries(filters.value).filter(([,v])=>v!==undefined).map(([k,v])=>[k,String(v)]));const response=await fetch('/api/market/export?'+params);if(!response.ok)throw new Error('导出失败，请检查登录状态');const url=URL.createObjectURL(await response.blob());const anchor=document.createElement('a');anchor.href=url;anchor.download='goofish-market.csv';anchor.click();URL.revokeObjectURL(url)},'已导出 CSV，金额单位为分。')}
const cards=computed(()=>!stats.value?[]:[['当前在售样本',String(stats.value.active.total_count)],['累计已售商品',String(stats.value.sold_total)],['近7日新增已售',String(stats.value.sold_7d.total_count)],['在售挂牌价中位数',fenPrice(stats.value.active.median_price)],['近7日已售参考价中位数',fenPrice(stats.value.sold_7d.median_price)],['近30日已售参考价中位数',fenPrice(stats.value.sold_30d.median_price)]])
const points=computed(()=>trends.value.map(p=>({day:p.day,avg_price:null,median_price:null})))
const chartSeries=computed(()=>{
  const lines=[]
  const line=(key:string,label:string,color:string)=>({key,label,color,values:trends.value.map(p=>p[key][metric.value]===null?null:p[key][metric.value]/100),counts:trends.value.map(p=>p[key].sample_count)})
  if(curve.value!=='sold')lines.push(line('active','在售挂牌价','#0284c7'))
  if(curve.value!=='active')lines.push(line('sold','已售参考价（最后挂牌价）','#f59e0b'))
  if(rolling.value)lines.push({key:'rolling',label:'已售参考价 · 7日滚动中位数',color:'#8b5cf6',values:trends.value.map(p=>p.sold_rolling_7d.median_price===null?null:p.sold_rolling_7d.median_price/100),counts:trends.value.map(p=>p.sold_rolling_7d.sample_count)})
  return lines
})
const lowSamples=computed(()=>trends.value.some(p=>p.active.low_sample||p.sold.low_sample))
onMounted(()=>{void load();timer=setInterval(()=>{if(!busy.value&&!showCreate.value)void load(true)},15000)})
onUnmounted(()=>{clearInterval(timer);generation++})
</script>

<template>
  <div class="space-y-6 text-slate-800 dark:text-slate-100">
    <header class="flex flex-wrap gap-4 justify-between items-start">
      <div><p class="text-xs tracking-widest text-primary mb-2">GOOFISH SENTINEL · 市场价格追踪</p><h1 class="text-3xl font-semibold flex gap-3 items-center"><TrendingUp class="text-primary"/>市场行情</h1><p class="text-sm text-slate-500 mt-2">发现商品 → 次日回访 → 明确已售归档 → 历史价格分析</p></div>
      <div class="flex flex-wrap gap-2"><Button variant="outline" :disabled="loading" @click="load()"><RefreshCw class="w-4 h-4 mr-2"/>刷新</Button><Button @click="showCreate=!showCreate"><Plus class="w-4 h-4 mr-2"/>新建市场任务</Button></div>
    </header>
    <div class="app-surface-subtle p-4 text-sm leading-6 border-amber-300/60" role="note">已售参考价取最后确认在售时的挂牌价，<strong>不等于真实成交价</strong>。首次确认已售日期也不一定是实际成交日。商品消失、页面失败或未知状态均不算已售。</div>
    <p v-if="error" role="alert" class="rounded-xl p-4 bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-200">{{error}}</p><p v-if="message" role="status" class="text-sm text-emerald-700 dark:text-emerald-300">{{message}}</p>
    <form v-if="showCreate" class="app-surface p-5 space-y-4" @submit.prevent="create">
      <h2 class="font-semibold text-lg">新建独立市场追踪任务（无需 LLM）</h2>
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label>任务名称<input v-model="form.task_name" :class="field" required maxlength="100" placeholder="Mac mini 16GB 市场追踪"/></label><label>搜索关键词<input v-model="form.keyword" :class="field" required maxlength="200" placeholder="Mac mini M6 16GB"/></label>
        <label>追踪天数<input v-model.number="form.tracking_days" type="number" min="1" max="365" :class="field" required/></label><label>搜索页数（低频）<input v-model.number="form.max_pages" type="number" min="1" max="5" :class="field" required/></label>
        <label>每日发现（上海时间）<input v-model="form.discovery_time" type="time" :class="field" required/></label><label>每日回访（上海时间）<input v-model="form.revisit_time" type="time" :class="field" required/></label>
        <label>账号文件名（可留空）<input v-model="form.account_state_file" :class="field" placeholder="state 目录中的 account.json"/></label><label class="flex items-center gap-2"><input v-model="form.notifications" type="checkbox"/>每日摘要（复用现有通知渠道）</label>
      </div><div class="flex gap-2"><Button type="submit" :disabled="busy">创建追踪任务</Button><Button type="button" variant="outline" @click="showCreate=false">取消</Button></div>
    </form>
    <section class="app-surface p-5 space-y-4">
      <div class="flex flex-wrap gap-3 items-end">
        <label class="min-w-56 flex-1 text-sm">产品配置<select v-model="variant" :class="field" @change="page=1;load()"><option value="">请选择配置</option><option v-for="v in variants" :key="v" :value="v">{{v==='unknown'?'unknown · 未分类（请人工核对）':v}}</option></select></label><label class="text-sm">地区<input v-model="region" :class="field" placeholder="如 上海" @change="page=1;load()"/></label>
        <label class="text-sm w-28">最低价（元）<input v-model="minPrice" type="number" min="0" :class="field" @change="page=1;load()"/></label><label class="text-sm w-28">最高价（元）<input v-model="maxPrice" type="number" min="0" :class="field" @change="page=1;load()"/></label><label class="text-sm flex gap-2 items-center"><input v-model="includeExcluded" type="checkbox" @change="page=1;load()"/>含排除商品</label><Button variant="outline" :disabled="busy" @click="exportCSV"><Download class="h-4 w-4 mr-2"/>导出 CSV</Button>
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3"><article v-for="[label,value] in cards" :key="label" class="app-surface-subtle p-4"><p class="text-xs text-slate-500">{{label}}</p><p class="text-xl font-semibold mt-3">{{value}}</p></article></div><p v-if="!variant" class="text-sm text-slate-500">概览为全部配置；价格曲线需先选择配置，避免混合不同型号。</p>
    </section>
    <section class="app-surface p-5 space-y-4">
      <details><summary class="cursor-pointer font-semibold">不同配置价格比较</summary><div class="overflow-x-auto mt-3"><table class="w-full text-sm text-left"><thead><tr><th>产品配置</th><th>在售中位数 / 样本</th><th>已售参考价中位数 / 样本</th></tr></thead><tbody><tr v-for="entry in comparison" :key="entry.product_variant" class="border-t dark:border-slate-800"><td class="py-3"><button class="text-primary" @click="variant=entry.product_variant;page=1;load()">{{entry.product_variant}}</button></td><td>{{fenPrice(entry.active.median_price)}} / {{entry.active.sample_count}}</td><td>{{fenPrice(entry.sold.median_price)}} / {{entry.sold.sample_count}}</td></tr></tbody></table></div></details>
    </section>
    <section class="app-surface p-5 space-y-4">
      <p v-if="variant==='unknown'" class="text-sm text-amber-700 dark:text-amber-300">未分类样本可能包含多种配置，请先人工更正后再作行情判断。</p>
      <div class="flex flex-wrap gap-3 items-end"><h2 class="text-lg font-semibold mr-auto">挂牌价与已售参考价</h2><select v-model="days" :class="field+' !w-auto'" aria-label="价格日期范围" @change="range();load()"><option value="7">近7天</option><option value="30">近30天</option><option value="90">近90天</option><option value="custom">自定义</option></select><input v-model="start" type="date" :class="field+' !w-auto'" aria-label="开始日期" @change="days='custom';load()"/><input v-model="end" type="date" :class="field+' !w-auto'" aria-label="结束日期" @change="days='custom';load()"/><select v-model="metric" :class="field+' !w-auto'" aria-label="价格统计方法"><option value="median_price">中位数</option><option value="avg_price">平均数</option></select><select v-model="curve" :class="field+' !w-auto'" aria-label="曲线类型"><option value="both">双曲线比较</option><option value="active">在售挂牌价</option><option value="sold">已售参考价</option></select><label class="text-sm flex gap-2"><input v-model="rolling" type="checkbox"/>7日滚动中位数</label></div>
      <p v-if="loading" class="text-sm text-slate-500">正在读取实际历史样本…</p><PriceTrendChart v-if="variant" :points="points" :series="chartSeries"/><p v-else class="text-center py-12 text-slate-500">暂无可比较的配置，请先创建任务或补全商品分类。</p><p v-if="lowSamples" class="text-xs text-amber-700 dark:text-amber-300">部分日期少于5件有效样本，波动可能较大；悬停价格点查看实际样本数。</p>
      <div class="flex flex-wrap gap-5 text-sm text-slate-500"><p v-for="n in ['7','30']" :key="n">近{{n}}日参考价变化（对比前一等长窗口）：{{fenPrice(stats?.changes?.[n]?.amount)}} · 样本 {{stats?.changes?.[n]?.current_samples||0}} / {{stats?.changes?.[n]?.previous_samples||0}}</p></div>
      <details><summary class="text-sm cursor-pointer">每日统计与原始商品明细</summary><div class="overflow-x-auto mt-3"><table class="w-full text-xs"><thead><tr><th>日期</th><th>在售中位数 / 数量</th><th>已售参考价中位数 / 数量</th><th>累计已售</th><th>已售样本商品</th></tr></thead><tbody><tr v-for="p in trends" :key="p.day" class="border-t dark:border-slate-800"><td class="py-2">{{p.day}}</td><td>{{fenPrice(p.active.median_price)}} / {{p.active.sample_count}}</td><td>{{fenPrice(p.sold.median_price)}} / {{p.sold.sample_count}}</td><td>{{p.cumulative_sold_count}}</td><td><button v-for="s in p.sold.samples" :key="s.item_id" class="text-primary mr-2" @click="showItem({item_id:s.item_id})">{{s.item_id}} · {{fenPrice(s.price)}}</button></td></tr></tbody></table></div></details>
    </section>
    <section class="app-surface p-5 space-y-4"><h2 class="text-lg font-semibold">商品追踪明细 · {{total}} 件</h2><div class="overflow-x-auto"><table class="w-full text-sm min-w-[1080px] text-left"><thead class="text-xs text-slate-500"><tr><th>商品 / 卖家</th><th>首次 / 最后挂牌价</th><th>状态</th><th>首次发现 / 确认已售</th><th>上次 / 下次检查</th><th>回访结果</th></tr></thead><tbody>
      <tr v-for="entry in items" :key="entry.item_id" class="border-t dark:border-slate-800"><td class="py-4 max-w-72"><button class="font-medium text-left hover:text-primary" @click="showItem(entry)">{{entry.title}}</button><p class="text-xs mt-1 text-slate-500">{{entry.item_id}} · {{entry.product_variant}}</p><div class="flex gap-3 mt-2 text-xs"><a :href="entry.item_url" target="_blank" rel="noopener noreferrer" class="text-primary">原商品 ↗</a><a v-if="entry.seller_url" :href="entry.seller_url" target="_blank" rel="noopener noreferrer" class="text-primary">{{entry.seller_nickname||entry.seller_id}} ↗</a><span v-else>卖家ID待补全</span><span v-if="entry.excluded" class="text-amber-600">已排除统计</span></div></td><td>{{fenPrice(entry.first_listed_price)}}<br/>{{fenPrice(entry.last_listing_price)}}</td><td>{{status(entry.listing_status)}}</td><td class="text-xs">{{entry.first_seen_at}}<br/>{{entry.first_seen_sold_at||'—'}}</td><td class="text-xs">{{entry.last_review_at||'—'}}<br/>{{entry.next_review_at||'—'}}</td><td class="text-xs">{{status(entry.revisit_status)}}<br/>{{entry.revisit_error||status(entry.revisit_result)}}</td></tr><tr v-if="!items.length"><td colspan="6" class="text-center py-12 text-slate-500">还没有匹配商品。创建任务并执行搜索，或调整筛选条件。</td></tr>
    </tbody></table></div><div class="flex justify-between items-center text-sm"><span>第 {{page}} 页</span><div class="flex gap-2"><Button variant="outline" :disabled="page<=1" @click="page--;load()">上一页</Button><Button variant="outline" :disabled="page*20>=total" @click="page++;load()">下一页</Button></div></div></section>
    <section class="app-surface p-5 space-y-4"><div class="flex flex-wrap justify-between gap-3"><h2 class="text-lg font-semibold">市场任务 · 上海时区</h2><div class="flex gap-2"><Button variant="outline" :disabled="busy" @click="action(()=>http('/api/market/revisits/run',{method:'POST'}),'已请求到期回访，未到期商品不会提前访问。')">执行到期回访</Button><Button variant="outline" :disabled="busy" @click="action(()=>http('/api/market/tasks/pause-all',{method:'POST'}))">暂停全部</Button></div></div>
      <p v-if="!tasks.length" class="text-sm text-slate-500">暂无市场任务。原有 AI 推荐与关键词任务仍在原任务页管理。</p><article v-for="task in tasks" :key="task.id" class="app-surface-subtle p-4 flex flex-wrap gap-4 items-center"><div class="flex-1 min-w-52"><h3 class="font-medium">{{task.task_name}} · {{task.enabled?'启用':'暂停'}}</h3><p class="text-xs text-slate-500 mt-1">{{task.keyword}} · {{task.tracking_days}}天 · 发现 {{task.discovery_time}} / 回访 {{task.revisit_time}}</p><p class="text-xs mt-1">最近发现：{{status(task.last_run?.status)}} · 新商品 {{task.last_run?.discovered??'—'}} · {{task.last_error||''}}</p></div><Button size="sm" variant="outline" :disabled="busy||!task.enabled" @click="action(()=>http(`/api/market/tasks/${task.id}/run`,{method:'POST'}),'已加入搜索队列，请查看最近运行结果。')">搜索一次</Button><Button size="sm" variant="outline" :disabled="busy" @click="toggleTask(task)">{{task.enabled?'暂停':'恢复'}}</Button><Button size="sm" variant="outline" :disabled="busy" @click="removeTask(task)">删除 · 保留历史</Button></article>
      <details><summary class="cursor-pointer text-sm">回访队列与最近运行结果</summary><div class="grid md:grid-cols-2 gap-4 mt-4 text-xs"><div><p class="font-medium mb-2">最近20条回访计划</p><p v-for="job in jobs" :key="job.item_id" class="py-2 border-b dark:border-slate-800">{{job.item_id}} · {{status(job.status)}} · {{job.next_run_at}}<br/>尝试 {{job.attempts}} 次 · {{job.last_error||status(job.last_result)}}</p></div><div><p class="font-medium mb-2">最近20次运行</p><p v-for="run in runs" :key="run.run_id" class="py-2 border-b dark:border-slate-800">{{run.started_at}} · {{run.kind}} · {{status(run.status)}}<br/>新发现 {{run.discovered}} · 回访 {{run.reviewed}} · 新确认已售 {{run.sold}} · {{run.error||''}}</p></div></div></details>
    </section>
    <section v-if="detail" class="app-surface p-5 space-y-4" aria-label="商品生命周期历史"><div class="flex justify-between"><h2 class="font-semibold">{{detail.item.title}} · 观察历史</h2><Button variant="outline" @click="detail=null">关闭历史</Button></div><p class="text-sm">已售参考价：{{fenPrice(detail.item.sold_reference_price)}} · 已售页展示价：{{fenPrice(detail.item.sold_display_price)}} · 实际成交金额：未知</p>
      <form class="grid sm:grid-cols-4 gap-3 items-end" @submit.prevent="saveCorrection"><label class="text-xs">分类<input v-model="correction.product_category" :class="field" required/></label><label class="text-xs">标准配置<input v-model="correction.product_variant" :class="field" required/></label><label class="text-sm"><input v-model="correction.excluded" type="checkbox"/>排除行情统计</label><Button :disabled="busy" type="submit">保存人工更正</Button></form>
      <div class="overflow-x-auto"><table class="w-full text-xs text-left"><thead><tr><th>观察时间</th><th>观察价</th><th>原始状态 / 判断</th><th>来源及证据</th><th>请求 / 页面 / 列表完整</th></tr></thead><tbody><tr v-for="o in detail.items" :key="o.id" class="border-t dark:border-slate-800"><td class="py-3">{{o.observed_at}}</td><td>{{fenPrice(o.price)}}</td><td>{{o.raw_status}} / {{status(o.parsed_status)}}</td><td>{{o.source}} · {{o.evidence}}</td><td>{{o.request_ok?'是':'否'}} / {{o.page_complete?'是':'否'}} / {{o.list_complete?'是':'否'}}</td></tr></tbody></table></div><p class="text-xs text-slate-500">最近100条，共{{detail.total}}条；完整历史支持分页 API。分类与状态变化均保留审计记录。</p><p v-for="e in detail.events" :key="e.id" class="text-xs">{{e.observed_at}}：{{status(e.old_status)}} → {{status(e.new_status)}} · {{e.evidence}}</p>
    </section>
  </div>
</template>
