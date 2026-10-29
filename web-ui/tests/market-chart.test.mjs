import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/utils/marketChart.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } })
const { trendPath, fenPrice } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'))
test('missing daily observations break paths without zero filling', () => {
  assert.equal(trendPath([null,100,110,null,90], x => x, y => y), 'M 1 100 L 2 110 M 4 90')
  assert.equal(trendPath([null,null], x => x, y => y), '')
})
test('fen prices distinguish missing data from genuine numeric values', () => {
  assert.equal(fenPrice(null), '—')
  assert.equal(fenPrice(123456), '¥1,234.56')
  assert.equal(fenPrice(0), '¥0')
})
