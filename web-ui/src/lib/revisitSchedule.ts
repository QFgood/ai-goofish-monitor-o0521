// APScheduler uses Monday=0. Emit named weekdays to avoid Unix-Cron ambiguity.
export const revisitFrequencies = [
  { value: 'daily', label: '每天' },
  { value: 'mon', label: '每周一' },
  { value: 'tue', label: '每周二' },
  { value: 'wed', label: '每周三' },
  { value: 'thu', label: '每周四' },
  { value: 'fri', label: '每周五' },
  { value: 'sat', label: '每周六' },
  { value: 'sun', label: '每周日' },
  { value: 'weekend', label: '周末（周六、周日）' },
] as const
export type RevisitFrequency = typeof revisitFrequencies[number]['value']
export interface RevisitSchedule { frequency: RevisitFrequency; hour: number; minute: number }
const weekdays = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const

export function parseRevisitSchedule(cron: string): RevisitSchedule | null {
  let normalized = cron.trim().toLowerCase()
  if (normalized === '@daily' || normalized === '@midnight') normalized = '0 0 * * *'
  let parts = normalized.split(/\s+/)
  if (parts.length === 6 && parts[0] === '0') parts = parts.slice(1)
  if (parts.length !== 5) return null
  const [minuteText, hourText, day, month, week] = parts
  if (!/^\d{1,2}$/.test(minuteText!) || !/^\d{1,2}$/.test(hourText!) || day !== '*' || month !== '*') return null
  const minute = Number(minuteText), hour = Number(hourText)
  if (minute > 59 || hour > 23) return null
  let frequency: RevisitFrequency
  const weekday = (value: string) => /^\d$/.test(value) ? weekdays[Number(value)] : weekdays.find(d => d === value)
  if (week === '*') frequency = 'daily'
  else {
    const days = week!.split(',').map(weekday)
    if (days.length === 2 && days.includes('sat') && days.includes('sun')) frequency = 'weekend'
    else if (days.length === 1 && days[0]) frequency = days[0]
    else return null
  }
  return { frequency, hour, minute }
}

export function buildRevisitCron({ frequency, hour, minute }: RevisitSchedule): string {
  if (!revisitFrequencies.some(f => f.value === frequency) || !Number.isInteger(hour) || hour < 0 || hour > 23 ||
      !Number.isInteger(minute) || minute < 0 || minute > 59) throw new Error('请选择有效的回访日期和时间')
  return `${minute} ${hour} * * ${frequency === 'daily' ? '*' : frequency === 'weekend' ? 'sat,sun' : frequency}`
}

export function describeRevisitSchedule(schedule: RevisitSchedule): string {
  return `${revisitFrequencies.find(f => f.value === schedule.frequency)!.label} ${String(schedule.hour).padStart(2,'0')}:${String(schedule.minute).padStart(2,'0')}（上海时间）`
}
