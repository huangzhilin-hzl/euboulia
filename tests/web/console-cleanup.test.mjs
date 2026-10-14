import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import { setImmediate } from 'node:timers/promises';

const html = readFileSync(new URL('../../src/euboulia/web/index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const loaders = script.slice(script.indexOf('    async function loadRuns('), script.indexOf('    async function loadMemory('));
const deletion = script.slice(script.indexOf('    async function deleteResource('), script.indexOf('    function switchView('));

function setup() {
  const run = { run_uid: 'failed-run', status: 'failed', infrastructure_state: 'pod_retained' };
  const state = {
    runs: [run], selected: run.run_uid, detail: { run }, view: 'runs', tab: 'activity',
    revision: 0, busy: false, action: null, logOffsets: { controller: 4 }, logContent: { controller: 'logs' },
  };
  const requests = [], notifications = [];
  const context = vm.createContext({
    state,
    api: (path, options) => new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })),
    renderDetail() {}, renderSummary() {}, renderRuns() {}, renderArtifactLedger() {}, loadLog() {},
    toast: (...args) => notifications.push(args),
  });
  vm.runInContext(loaders + deletion, context);
  return { context, state, run, requests, notifications };
}

test('late list and detail responses cannot restore a deleted experiment', async () => {
  const { context, state, run, requests } = setup();
  const oldList = context.loadRuns();
  const oldDetail = context.loadDetail(run.run_uid);
  const deleting = context.deleteResource(run.run_uid, 'run');
  assert.equal(requests[2].options.method, 'DELETE');
  assert.equal(requests[2].options.headers['X-Euboulia-Control'], '1');
  requests[2].resolve({ deleted: run.run_uid });
  await setImmediate();
  requests[3].resolve({ runs: [] });
  await deleting;
  requests[0].resolve({ runs: [run] });
  requests[1].resolve({ run });
  await Promise.all([oldList, oldDetail]);
  assert.equal(state.runs.length, 0);
  assert.equal(state.selected, null);
  assert.equal(state.detail, null);
  assert.deepEqual(Object.keys(state.logContent), []);
  assert.equal(state.busy, false);
});

test('late detail cannot undo successful Pod cleanup on the selected experiment', async () => {
  const { context, state, run, requests } = setup();
  const oldDetail = context.loadDetail(run.run_uid);
  const deleting = context.deleteResource(run.run_uid, 'pod');
  assert.equal(requests[1].path, '/api/runs/failed-run/pod');
  const cleaned = { ...run, infrastructure_state: 'pod_deleted' };
  requests[1].resolve({ run: cleaned });
  await setImmediate();
  requests[2].resolve({ runs: [cleaned] });
  await setImmediate();
  requests[3].resolve({ run: cleaned });
  await deleting;
  requests[0].resolve({ run });
  await oldDetail;
  assert.equal(state.selected, run.run_uid);
  assert.equal(state.detail.run.infrastructure_state, 'pod_deleted');
});

test('failed cleanup keeps the selected experiment and releases the action state', async () => {
  const { context, state, run, requests, notifications } = setup();
  const originalDetail = state.detail;
  const deleting = context.deleteResource(run.run_uid, 'run');
  assert.equal(state.busy, true);
  requests[0].reject(new Error('Pod ownership could not be verified'));
  await deleting;
  assert.equal(state.selected, run.run_uid);
  assert.equal(state.detail, originalDetail);
  assert.equal(state.logContent.controller, 'logs');
  assert.equal(state.revision, 0);
  assert.equal(state.busy, false);
  assert.equal(state.action, null);
  assert.deepEqual(notifications, [['Pod ownership could not be verified', true]]);
});
