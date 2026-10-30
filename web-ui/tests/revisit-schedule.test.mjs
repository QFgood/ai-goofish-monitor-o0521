import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/lib/revisitSchedule.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } })
const { parseRevisitSchedule: parse, buildRevisitCron: build, describeRevisitSchedule: describe, revisitFrequencies } =
  await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'))
test('all daily, named weekday and weekend choices roundtrip without time drift', () => {
  for (const {value:frequency} of revisitFrequencies) {
    for (const [hour,minute] of [[0,0],[9,30],[12,0],[23,59]]) {
      const schedule={frequency,hour,minute}
      assert.deepEqual(parse(build(schedule)),schedule)
    }
  }
  assert.equal(build({frequency:'weekend',hour:12,minute:5}),'5 12 * * sat,sun')
})
test('existing daily noon rule is displayed as noon rather than the default 09:30', () => {
  assert.deepEqual(parse('0 12 * * *'),{frequency:'daily',hour:12,minute:0})
  assert.equal(describe(parse('0 12 * * *')),'每天 12:00（上海时间）')
})
test('legacy numeric days follow APScheduler Monday=0 and Sunday=6', () => {
  assert.equal(parse('0 8 * * 0').frequency,'mon')
  assert.equal(parse('0 8 * * 6').frequency,'sun')
  assert.equal(parse('0 8 * * 5,6').frequency,'weekend')
  assert.equal(parse('0 8 * * sun,sat').frequency,'weekend')
  assert.equal(parse('0 8 * * 6,0'),null)
})
test('daily aliases and zero-second six-field legacy rules are recognized', () => {
  assert.deepEqual(parse('@daily'),{frequency:'daily',hour:0,minute:0})
  assert.deepEqual(parse('0 30 9 * * *'),{frequency:'daily',hour:9,minute:30})
})
test('complex or invalid legacy rules return custom, without silently replacing the value', () => {
  for (const rule of ['*/5 * * * *','0 8 1 * *','10 0 8 * * *','0 25 * * *','60 9 * * *','0 8 * * mon-fri','']) {
    assert.equal(parse(rule),null)
  }
})
test('invalid selections are rejected and valid preview pads hours and minutes', () => {
  for (const change of [{hour:24},{hour:-1},{minute:60},{minute:0.5},{frequency:'invalid'}]) {
    assert.throws(()=>build({frequency:'daily',hour:9,minute:30,...change}))
  }
  assert.equal(describe({frequency:'sun',hour:0,minute:5}),'每周日 00:05（上海时间）')
})
