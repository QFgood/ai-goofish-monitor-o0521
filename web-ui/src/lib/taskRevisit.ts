import type { TaskRevisitRun } from '@/types/task.d.ts'
export function isRevisitRunning(run:TaskRevisitRun|null|undefined,now=Date.now()):boolean {
  return !!run && run.status==='running' && (!run.lease_until || new Date(run.lease_until).getTime()>now)
}
export function describeTaskRevisit(run:TaskRevisitRun|null|undefined,now=Date.now()):string {
  if (!run) return ''
  if (isRevisitRunning(run,now)) return `回访中 · 已检查 ${run.reviewed} 件`
  if (run.status==='completed') return `回访完成：检查 ${run.reviewed} 件，新确认已售 ${run.sold} 件`
  if (run.status==='empty') return '暂无可回访商品：请先采集，或检查商品是否已售、超期'
  if (run.status==='access_paused') return '登录失效或访问受限，请更新登录态后再试'
  if (run.status==='interrupted' || run.status==='running') return '回访已中断，可重新回访'
  return '回访失败，记录已保留，可稍后重试'
}
