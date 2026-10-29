/** Null values break a curve instead of inventing a zero or connecting across missing days. */
export function trendPath(values: Array<number | null>, x: (index: number) => number, y: (value: number) => number): string {
  let drawing = false
  return values.map((value, index) => {
    if (value === null || !Number.isFinite(value)) { drawing = false; return '' }
    const command = `${drawing ? 'L' : 'M'} ${x(index)} ${y(value)}`
    drawing = true
    return command
  }).filter(Boolean).join(' ')
}

export function fenPrice(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `¥${(value / 100).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}
