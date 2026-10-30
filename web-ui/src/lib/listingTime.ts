/** Listing timestamps are displayed in Shanghai, independent of device timezone. */
export function formatListingTime(value: string | null | undefined): string {
  if (!value) return '—'
  let normalized = value
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) normalized += 'T00:00:00+08:00'
  else if (/^\d{4}-\d{2}-\d{2}T/.test(value) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(value)) normalized += '+08:00'
  const date = new Date(normalized)
  if (Number.isNaN(date.getTime())) return '—'
  const parts = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(date)
  const get = (type:string) => parts.find(part=>part.type===type)!.value
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`
}
