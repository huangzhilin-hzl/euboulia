import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../../src/euboulia/playground/web/app.js', import.meta.url), 'utf8');
const choicesScript = readFileSync(new URL('../../src/euboulia/playground/web/choices.js', import.meta.url), 'utf8');
new vm.Script(script);
new vm.Script(choicesScript);
const helpers = script.slice(script.indexOf('function nodeGpuView('), script.indexOf('function draftKey('));
const model = vm.createContext({});
vm.runInContext(helpers, model);
const { nodeGpuView, sortNodeList, selectNodeValue, expireNodeOptions } = model;
const node = (name, state, idle = 0, extra = {}) => ({
  name, ip: name, ready: true,
  gpu: { state, total: 8, idle, measured: 8, checked_at: Date.now() / 1000 }, ...extra,
});

test('all-idle requires a Ready node and a complete physical GPU sample', () => {
  assert.equal(nodeGpuView(node('all', 'idle', 8)).label, 'All 8 idle');
  for (const changed of [{ total: null }, { measured: 7 }, { idle: 7 }, { state: 'unknown' }]) {
    const original = node('incomplete', 'idle', 8);
    assert.equal(nodeGpuView({ ...original, gpu: { ...original.gpu, ...changed } }).status, 'unknown');
  }
  assert.equal(nodeGpuView(node('unverified', 'idle', 8, { ready: null })).status, 'unknown');
  assert.equal(nodeGpuView(node('offline', 'idle', 8, { ready: false })).status, 'not_ready');
  assert.equal(nodeGpuView({ name: 'missing' }).label, 'Unknown');
});

test('partial and unknown samples never claim that every GPU is idle', () => {
  const partial = nodeGpuView(node('partial', 'busy', 3));
  assert.equal(partial.status, 'partial');
  assert.equal(partial.label, '3/8 idle');
  const unknown = nodeGpuView(node('unknown', 'unknown', 8));
  assert.equal(unknown.status, 'unknown');
  assert.match(unknown.label, /Unknown/);
  assert.equal(nodeGpuView(node('busy', 'busy')).label, 'Busy');
  const invalid = node('invalid', 'idle', 8);
  invalid.gpu.measured = 'N/A';
  assert.equal(nodeGpuView(invalid).status, 'unknown');
});

test('all-idle sorts first, followed by partial idle counts, unknown, and unavailable nodes', () => {
  const nodes = [
    node('offline', 'idle', 8, { ready: false }), node('unknown', 'unknown', 6),
    node('busy', 'busy'), node('three', 'busy', 3), node('seven', 'busy', 7),
    node('all', 'idle', 8), node('no-gpu', 'no_gpu'),
  ];
  assert.deepEqual(Array.from(sortNodeList(nodes), n => n.name),
    ['all', 'seven', 'three', 'busy', 'unknown', 'offline', 'no-gpu']);
  assert.equal(nodes[0].name, 'offline', 'sorting must not mutate the API snapshot');
  assert.deepEqual(Array.from(sortNodeList([node('10.0.0.10', 'unknown'), node('10.0.0.2', 'unknown')]), n => n.name),
    ['10.0.0.2', '10.0.0.10']);
});

test('initial selection favors all-idle, while explicit and connected selections remain stable', () => {
  const nodes = sortNodeList([node('partial', 'busy', 3), node('all', 'idle', 8)]);
  assert.equal(selectNodeValue(nodes, 'partial', true, false), 'all');
  assert.equal(selectNodeValue(nodes, 'partial', false, false), 'partial');
  assert.equal(selectNodeValue(nodes, 'partial', true, true), 'partial');
  assert.equal(selectNodeValue(nodes, 'removed-node', false, true), 'removed-node');
  assert.equal(selectNodeValue([node('offline', 'not_ready'), node('none', 'no_gpu')], '', true, false), '');
});

class OptionModel {
  constructor(text, value) { this.text = text; this.value = value; this.dataset = {}; this.disabled = false; this.title = ''; }
}
class SelectModel {
  constructor(value = '') { this.value = value; this.options = []; this.listeners = new Map(); this.events = []; this.replacements = 0; }
  replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; this.replacements++; }
  get selectedOptions() { return this.options.filter(option => option.value === this.value); }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  dispatchEvent(event) {
    this.events.push(event.type);
    for (const callback of this.listeners.get(event.type) || []) callback(event);
    if (event.type === 'change') this.onchange?.(event);
  }
}
class EventModel {
  constructor(type, options = {}) { this.type = type; Object.assign(this, options); }
}
function harness() {
  const elements = {
    cluster: new SelectModel('alpha'), node: new SelectModel(),
    'refresh-nodes': {}, 'node-refresh-status': {}, 'node-gpu-help': {},
  };
  const requests = [], timers = [], errors = [];
  let sessionRenders = 0;
  const context = vm.createContext({
    $: id => elements[id], Option: OptionModel, Event: EventModel, CustomEvent: EventModel, choices: { sync() {} },
    config: { clusters: [{ name: 'alpha', gpu_idle: { memory_mb: 64, utilization_percent: 1 } }, { name: 'beta' }] },
    api: path => new Promise((resolve, reject) => requests.push({ path, resolve, reject })),
    renderSession() { sessionRenders++; }, controls() {}, selectSession() {},
    setTimeout(fn, delay) { timers.push({ fn, delay }); return timers.length; }, clearTimeout() {},
    error: message => errors.push(message),
  });
  const optionHelper = script.slice(script.indexOf('function options('), script.indexOf('// Claim that every GPU'));
  const loader = script.slice(script.indexOf('async function loadNodes('), script.indexOf('function selectSession('));
  vm.runInContext('var nodeRequest = 0, nodeRefreshTimer = 0, nodeFreshnessTimer = 0, nodeAutoSelect = true, session = null, historyKey = ""; const nodeLabels = new Map();' + helpers + optionHelper + loader, context);
  vm.runInContext(script.slice(script.indexOf('$("node").onchange ='), script.indexOf('$("refresh-nodes").onclick =')), context);
  return { context, elements, requests, timers, errors, renders: () => sessionRenders };
}
const snapshot = (nodes, refreshing = false) => ({ nodes, refreshing, updated_at: 100 });

test('background refresh preserves manual node/session selection and uses the configured idle rule', async () => {
  const h = harness();
  const first = h.context.loadNodes({ reset: true });
  h.requests[0].resolve(snapshot([node('partial', 'busy', 3), node('all', 'idle', 8)], true));
  await first;
  assert.equal(h.elements.node.value, 'all');
  assert.equal(h.timers.at(-1).delay, 2000);
  assert.match(h.elements['node-gpu-help'].textContent, /≤1%.*≤64 MiB/);
  h.elements.node.value = 'partial'; h.context.nodeAutoSelect = false;
  h.context.session = { id: 'existing-workspace' };
  const refresh = h.context.loadNodes({ force: true });
  assert.equal(h.elements.node.value, 'partial', 'refresh must not clear the selected node while waiting');
  h.requests[1].resolve(snapshot([node('all', 'idle', 8), node('partial', 'busy', 3)]));
  await refresh;
  assert.match(h.requests[1].path, /\?refresh=1$/);
  assert.equal(h.elements.node.value, 'partial');
  assert.equal(h.context.session.id, 'existing-workspace');
  assert.equal(h.renders(), 1, 'only the initial cluster load should clear/render the workspace');
  assert.equal(h.timers.at(-1).delay, 15000);
  assert.equal(h.elements.node.selectedOptions[0].dataset.choiceStatus, 'partial');
});

test('pending initial telemetry can choose an all-idle node once, then later refreshes preserve selection', async () => {
  const h = harness();
  const first = h.context.loadNodes({ reset: true });
  h.requests[0].resolve(snapshot([node('initial', 'unknown'), node('candidate', 'unknown')], true));
  await first;
  const initial = h.elements.node.value;
  const chosen = initial === 'initial' ? 'candidate' : 'initial';
  const second = h.context.loadNodes();
  h.requests[1].resolve(snapshot([node(initial, 'busy'), node(chosen, 'idle', 8)]));
  await second;
  assert.equal(h.elements.node.value, chosen);
  assert.equal(h.context.nodeAutoSelect, false);
  const third = h.context.loadNodes();
  h.requests[2].resolve(snapshot([node(initial, 'idle', 8), node(chosen, 'busy')]));
  await third;
  assert.equal(h.elements.node.value, chosen);
});

test('stale responses from a previous cluster cannot replace the node list or polling timer', async () => {
  const h = harness();
  const oldRequest = h.context.loadNodes({ reset: true });
  h.elements.cluster.value = 'beta';
  const newRequest = h.context.loadNodes({ reset: true });
  h.requests[1].resolve(snapshot([node('beta-node', 'idle', 8)]));
  await newRequest;
  const timers = h.timers.length;
  h.requests[0].resolve(snapshot([node('alpha-node', 'idle', 8)], true));
  await oldRequest;
  assert.equal(h.elements.node.value, 'beta-node');
  assert.deepEqual(h.elements.node.options.map(option => option.value), ['beta-node']);
  assert.equal(h.timers.length, timers);
});

test('a selected workspace that disappears remains visible as unavailable, without switching nodes', async () => {
  const h = harness();
  h.elements.node.replaceChildren(new OptionModel('existing IP · Busy', 'existing'));
  h.context.nodeAutoSelect = false; h.context.session = { id: 'existing-workspace' };
  const refresh = h.context.loadNodes();
  h.requests[0].resolve(snapshot([node('new-node', 'idle', 8)]));
  await refresh;
  assert.equal(h.elements.node.value, 'existing');
  assert.equal(h.elements.node.selectedOptions[0].disabled, true);
  assert.equal(h.elements.node.selectedOptions[0].dataset.choiceSummary, 'No longer listed');
});

test('telemetry expires honestly after 45 seconds without changing the selected node', () => {
  const stale = node('stale', 'idle', 8);
  stale.gpu.checked_at = 100;
  assert.equal(nodeGpuView(stale, 144).status, 'idle');
  assert.equal(nodeGpuView(stale, 145).status, 'unknown');
  const select = new SelectModel();
  const option = new OptionModel('stale IP · All 8 idle', 'stale');
  Object.assign(option.dataset, { choiceStatus: 'idle', choiceLabel: 'stale IP', choiceCheckedAt: '100' });
  select.replaceChildren(option);
  assert.equal(expireNodeOptions(select, 144), false);
  assert.equal(expireNodeOptions(select, 145), true);
  assert.equal(select.value, 'stale');
  assert.equal(option.dataset.choiceStatus, 'unknown');
  assert.match(option.text, /Unknown/);
  assert.equal(option.disabled, false, 'stale telemetry does not make a configured target unusable');
});

test('failed refresh expires old green telemetry while retaining the selected workspace', async () => {
  const h = harness();
  const first = h.context.loadNodes({ reset: true });
  h.requests[0].resolve(snapshot([node('existing', 'idle', 8)]));
  await first;
  const selected = h.elements.node.selectedOptions[0];
  selected.dataset.choiceCheckedAt = String(Date.now() / 1000 - 46);
  h.context.session = { id: 'existing-workspace' };
  const refresh = h.context.loadNodes();
  h.requests[1].reject(new Error('Server unavailable'));
  await refresh;
  assert.equal(h.elements.node.value, 'existing');
  assert.equal(h.context.session.id, 'existing-workspace');
  assert.equal(selected.dataset.choiceStatus, 'unknown');
  assert.match(h.elements['node-refresh-status'].textContent, /Could not refresh/);
  assert.deepEqual(h.errors, ['Server unavailable']);
});

test('scheduled telemetry expiry also runs while a network refresh is still pending', async () => {
  const h = harness();
  const first = h.context.loadNodes({ reset: true });
  h.requests[0].resolve(snapshot([node('existing', 'idle', 8)]));
  await first;
  const expiry = h.timers.find(timer => timer.delay > 40000);
  assert.ok(expiry, 'fresh GPU telemetry must have an independent expiry timer');
  const refresh = h.context.loadNodes();
  h.elements.node.selectedOptions[0].dataset.choiceCheckedAt = String(Date.now() / 1000 - 46);
  expiry.fn();
  assert.equal(h.elements.node.value, 'existing');
  assert.equal(h.elements.node.selectedOptions[0].dataset.choiceStatus, 'unknown');
  h.requests[1].resolve(snapshot([node('existing', 'idle', 8)]));
  await refresh;
  assert.equal(h.elements.node.selectedOptions[0].dataset.choiceStatus, 'idle');
});

test('explicitly choosing the current node during pending telemetry prevents later automatic switching', async () => {
  const h = harness();
  const first = h.context.loadNodes({ reset: true });
  h.requests[0].resolve(snapshot([node('initial', 'unknown'), node('candidate', 'unknown')], true));
  await first;
  assert.equal(h.context.nodeAutoSelect, true);
  const selected = h.elements.node.value;
  const chooseMethod = choicesScript.slice(choicesScript.indexOf('    choose(index)'), choicesScript.indexOf('    position()'));
  const choice = vm.runInContext('({' + chooseMethod + '})', h.context);
  choice.select = h.elements.node; choice.close = () => {}; choice.sync = () => {};
  choice.choose(h.elements.node.options.findIndex(option => option.value === selected));
  assert.equal(h.context.nodeAutoSelect, false);
  assert.deepEqual(h.elements.node.events, ['choice-select'], 'same-value choice must not invent a change event');
  const other = selected === 'initial' ? 'candidate' : 'initial';
  const refresh = h.context.loadNodes();
  h.requests[1].resolve(snapshot([node(selected, 'busy'), node(other, 'idle', 8)]));
  await refresh;
  assert.equal(h.elements.node.value, selected);
  choice.choose(h.elements.node.options.findIndex(option => option.value === other));
  assert.deepEqual(h.elements.node.events, ['choice-select', 'choice-select', 'change']);
  assert.equal(h.elements.node.value, other);
});

test('timestamp-only background refresh retains options, status text, refresh button, and history cache', async () => {
  const h = harness();
  const current = node('existing', 'idle', 8);
  const first = h.context.loadNodes({ reset: true });
  h.requests[0].resolve(snapshot([current])); await first;
  const original = h.elements.node.options[0], replacements = h.elements.node.replacements;
  const summary = h.elements['node-refresh-status'].textContent;
  h.context.historyKey = 'unchanged-history';
  const refresh = h.context.loadNodes();
  assert.equal(h.elements['node-refresh-status'].textContent, summary);
  assert.equal(h.elements['refresh-nodes'].disabled, false, 'quiet background refresh must not flash the button');
  const newer = { ...current, gpu: { ...current.gpu, checked_at: current.gpu.checked_at + 1 } };
  h.requests[1].resolve(snapshot([newer])); await refresh;
  assert.equal(h.elements.node.options[0], original);
  assert.equal(h.elements.node.replacements, replacements);
  assert.equal(original.dataset.choiceCheckedAt, String(newer.gpu.checked_at));
  assert.equal(h.context.historyKey, 'unchanged-history');
});
