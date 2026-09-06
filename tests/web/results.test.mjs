import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

// Exercise the shipped inline script, with no browser or third-party test dependency.
const html = readFileSync(new URL('../../src/euboulia/web/index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Also catches syntax errors in the complete console.
const start = script.indexOf('    function resultNumber(');
const end = script.indexOf('    const resultViews =');
const context = vm.createContext({});
vm.runInContext(script.slice(start, end), context);
const { resultPoints, resultSource, resultFormat, filteredResultPoints, resultChartSeries } = context;
const point = (metrics = {}, extra = {}) => ({ metrics, gate_passed: true, ...extra });
const source = (point_results, extra = {}) => ({ evaluation: { point_results, ...extra } });
const names = rows => Array.from(rows, p => p.name);

test('reads validation, synchronized summary result and legacy summaries', () => {
  const result = source({});
  assert.equal(resultSource({ validation: result, summary: {} }), result);
  assert.equal(resultSource({ summary: { result } }), result);
  assert.equal(resultSource({ summary: result }), result);
  assert.equal(resultSource({}), undefined);
});

test('workload dimensions sort numerically, with all planned and objective-only points retained', () => {
  const input = source({
    'isl1024-osl1024-c16-n16': point(),
    'isl1024-osl1024-c2-n2': point(),
    'isl1024-osl1024-c1-n1': point(),
    'isl32768-osl256-c1-n1': point(),
  }, { objective_values: { 'isl1024-osl1024-c4-n4': 42 } });
  input.identity = { aliases: { points: ['isl1024-osl1024-c8-n8'] } };
  const rows = resultPoints(input);
  assert.deepEqual(names(rows), [
    'isl32768-osl256-c1-n1', 'isl1024-osl1024-c1-n1', 'isl1024-osl1024-c2-n2',
    'isl1024-osl1024-c4-n4', 'isl1024-osl1024-c8-n8', 'isl1024-osl1024-c16-n16',
  ]);
  assert.equal(rows[3].status, 'Pending');
  assert.equal(rows[4].status, 'Pending');
  assert.equal(rows[5].requests, 16);
});

test('throughput comes from the named metric, never an arbitrary objective or failed zero sentinel', () => {
  const rows = resultPoints(source({
    'isl1024-osl1024-c1-n1': point({ output_throughput: 300, mean_ttft_ms: 50 }, { objective_value: 50 }),
    'isl1024-osl1024-c2-n2': point({}, { objective_value: 0, gate_passed: false }),
  }, { objective_values: { 'isl1024-osl1024-c4-n4': 90 } }));
  assert.equal(rows[0].throughput, 300);
  assert.equal(rows[0].ttft, 50);
  assert.equal(rows[0].objective, 50);
  assert.equal(rows[1].throughput, null);
  assert.equal(rows[2].throughput, null);
});

test('missing and non-finite values stay missing, while measured zero remains zero', () => {
  const row = resultPoints(source({ custom: point({ output_throughput: 0, mean_ttft_ms: Infinity, mean_tpot_ms: '7' }, { accuracy_value: 0, measurement_values: [null, 0, NaN] }) }))[0];
  assert.equal(row.throughput, 0);
  assert.equal(row.ttft, null);
  assert.equal(row.tpot, null);
  assert.equal(row.input, null);
  assert.equal(row.accuracy, 0);
  assert.deepEqual(Array.from(row.windows), [null, 0, null]);
  assert.equal(resultFormat(null), '—');
  assert.equal(resultFormat(NaN), '—');
  assert.equal(resultFormat(0), '0');
  assert.equal(resultFormat(1441.4345), '1,441.43');
});

test('failed, skipped, incomplete and legacy points are never counted as passed', () => {
  const rows = resultPoints(source({
    failed: point({}, { gate_passed: false, outcome: 'failed' }),
    conflict: point({}, { outcome: 'failed' }),
    skipped: { outcome: 'skipped' }, pending: {}, passed: point(),
  }));
  assert.equal(rows.filter(row => row.status === 'Passed').length, 1);
  assert.equal(rows.filter(row => row.status === 'Failed').length, 2);
  assert.equal(rows.find(row => row.name === 'skipped').status, 'Skipped');
  assert.equal(rows.find(row => row.name === 'pending').status, 'Pending');
});

test('numeric sorting keeps absent metrics last in both directions and combines filters', () => {
  const rows = resultPoints(source({
    'isl1024-osl1024-c1-n1': point({ output_throughput: 9 }),
    'isl1024-osl1024-c2-n2': point({ output_throughput: 100 }),
    'isl32768-osl1024-c1-n1': point({ output_throughput: 70 }),
    'isl1024-osl256-c1-n1': point({ output_throughput: 20 }),
    'isl1024-osl1024-c4-n4': point({}, { gate_passed: false }),
  }));
  const options = { output: '1024', input: '1024', sort: 'throughput', direction: -1 };
  assert.deepEqual(names(filteredResultPoints(rows, options)), ['isl1024-osl1024-c2-n2', 'isl1024-osl1024-c1-n1', 'isl1024-osl1024-c4-n4']);
  assert.equal(filteredResultPoints(rows, { ...options, direction: 1 }).at(-1).throughput, null);
  assert.equal(filteredResultPoints(rows, { ...options, status: 'Failed' }).length, 1);
  assert.equal(filteredResultPoints(rows, { ...options, status: 'Skipped' }).length, 0);
});

test('charts separate output lengths and request-count protocols, and retain gaps', () => {
  const rows = resultPoints(source({
    'isl1024-osl1024-c1-n1': point({ output_throughput: 9 }),
    'isl1024-osl1024-c2-n2': point({}, { gate_passed: false }),
    'isl1024-osl1024-c4-n4': point({ output_throughput: 40 }),
    'isl1024-osl1024-c1-n10': point({ output_throughput: 80 }),
    'isl1024-osl256-c1-n1': point({ output_throughput: 20 }),
  }));
  const lines = resultChartSeries(rows, 1024, 'throughput', 'concurrency');
  assert.equal(lines.length, 2);
  assert.deepEqual(Array.from(lines.find(line => line.name.endsWith('N = C')).values, v => v.y), [9, null, 40]);
  assert.equal(lines.flatMap(line => line.values).every(v => v.point.output === 1024), true);
});

test('custom point names retain their identity, dimensions from metrics and primary designation', () => {
  const rows = resultPoints(source({ 'long context': point({ random_input_len: 65536, random_output_len: 256, max_concurrency: 1 }) }, { primary_points: ['long context'] }));
  assert.equal(rows[0].name, 'long context');
  assert.equal(rows[0].input, 65536);
  assert.equal(rows[0].primary, true);
});
