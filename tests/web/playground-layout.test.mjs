import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../../src/euboulia/playground/web/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../../src/euboulia/playground/web/index.html', import.meta.url), 'utf8');
new vm.Script(script);
const layout = script.slice(script.indexOf('function initPanelLayout('), script.indexOf('async function init('));
const action = script.slice(script.indexOf('async function action('), script.indexOf('function options('));
const active = script.slice(script.indexOf('function activeRun('), script.indexOf('function controls('));
const selectSession = script.slice(script.indexOf('function selectSession('), script.indexOf('function shellQuote('));
const history = script.slice(script.indexOf('function renderHistory('), script.indexOf('async function readEvents('));
const run = script.slice(script.indexOf('async function run('), script.indexOf('$("error").onclick ='));
const shareKey = 'molou-playground-editor-share';

class ClassList {
  constructor(initial = '') { this.values = new Set(initial.split(/\s+/).filter(Boolean)); }
  contains(name) { return this.values.has(name); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  toggle(name, force = !this.contains(name)) {
    if (force) this.add(name); else this.remove(name);
    return force;
  }
}

class Element {
  constructor(classes = '') {
    this.classList = new ClassList(classes);
    this.attributes = new Map();
    this.properties = new Map();
    this.style = {
      setProperty: (name, value) => this.properties.set(name, value),
      getPropertyValue: name => this.properties.get(name) || '',
    };
    this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false;
    this.children = []; this.dataset = {};
    this.clientHeight = 0; this.offsetHeight = 0;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}

function record(id = 'active-run', status = 'running', extra = {}) {
  return {
    id, status, session: 'workspace', profile: 'gpu', mode: 'run', gpu_index: 0,
    gpu_uuid: 'GPU-test', created_at: 1_700_000_000, cluster: 'alpha', node: 'worker',
    args: [], ...extra,
  };
}

function setup(savedShare = null) {
  const elements = Object.fromEntries([
    'workbench', 'panel-resizer', 'expand-editor', 'toggle-output', 'editor-panel',
    'output-panel', 'run', 'error', 'code', 'arguments', 'gpu', 'profiler-arguments',
    'history', 'run-count', 'cluster', 'node',
  ].map(id => [id, new Element()]));
  const markupClasses = html.match(/id="workbench" class="([^"]+)"/)?.[1] || '';
  elements.workbench.classList = new ClassList(markupClasses);
  elements.workbench.clientHeight = 1012;
  elements['panel-resizer'].offsetHeight = 12;
  elements['editor-panel'].offsetHeight = 620;
  elements['output-panel'].clientHeight = 380;
  elements.code.value = 'keep my unsaved source';
  elements.arguments.value = '--keep-my-arguments';
  elements.gpu.value = '0'; elements.cluster.value = 'alpha'; elements.node.value = 'worker';
  const values = new Map(savedShare === null ? [] : [[shareKey, String(savedShare)]]);
  const requests = [], calls = [], listeners = new Map();
  const workspace = {id: 'workspace', cluster: 'alpha', node: 'worker', status: 'ready'};
  const context = vm.createContext({
    $: id => {
      assert.ok(elements[id], `Unexpected DOM lookup: ${id}`);
      return elements[id];
    },
    document: {
      body: new Element(),
      addEventListener: (name, callback) => listeners.set(name, callback),
      querySelector: () => null,
      createElement: () => new Element(),
    },
    localStorage: {
      getItem: name => values.get(name) ?? null,
      setItem: (name, value) => values.set(name, String(value)),
    },
    ResizeObserver: class { observe() {} },
    choices: {isOpen: () => false},
    api: (path, body) => new Promise((resolve, reject) => requests.push({path, body, resolve, reject})),
    controls: () => calls.push('controls'),
    error: message => calls.push(`error: ${message}`),
    saveDraft: () => calls.push('saveDraft'),
    clearConsole: () => calls.push('clearConsole'),
    resetResults: () => calls.push('resetResults'),
    badge: () => calls.push('badge'),
    renderSession: () => calls.push('renderSession'),
    historyDate: () => 'Saved date', historyAge: () => 'Just now',
    historyCommand: () => ({preview: 'python solution.py', detail: 'Saved command'}),
    nodeLabel: (_cluster, node) => node,
    loadRunDraft: (_run, code) => { calls.push('loadRunDraft'); elements.code.value = code; },
    readEvents: async () => calls.push('readEvents'),
  });
  vm.runInContext(`
    var busy = false, selectedRun = null, cursor = 75, historyKey = '';
    var currentProfile = 'gpu', currentMode = 'run';
    var session = ${JSON.stringify(workspace)};
    var state = {sessions: [session], runs: []};
    const activeStates = new Set(['queued', 'verifying_gpu', 'preparing_environment', 'running', 'cancelling', 'profiling', 'exporting', 'finalizing']);
    ${action}
    ${active}
    ${selectSession}
    ${history}
    ${run}
    ${layout}
  `, context);
  const panel = vm.runInContext('panelLayout', context);
  const collapsed = () => elements.workbench.classList.contains('output-collapsed');
  return {context, elements, values, requests, calls, listeners, panel, collapsed};
}

test('console starts collapsed and manual toggles keep its accessible label in sync', () => {
  const h = setup(), button = h.elements['toggle-output'];
  assert.equal(h.collapsed(), true);
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  assert.equal(button.textContent, 'Expand');
  assert.equal(button.getAttribute('aria-label'), 'Expand console output');
  button.onclick();
  assert.equal(h.collapsed(), false);
  assert.equal(button.getAttribute('aria-expanded'), 'true');
  assert.equal(button.textContent, 'Collapse');
  assert.equal(button.getAttribute('aria-label'), 'Collapse console output');
  button.onclick();
  assert.equal(h.collapsed(), true);
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  assert.equal(button.title, 'Expand console output');
});

test('Run reveals output immediately while its POST remains pending', async () => {
  const h = setup();
  h.elements['expand-editor'].onclick();
  assert.equal(h.elements.workbench.classList.contains('editor-expanded'), true);
  const pending = h.context.run();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].path, '/api/runs');
  assert.equal(h.requests[0].body.code, 'keep my unsaved source');
  assert.equal(h.collapsed(), false, 'output must be visible before the API responds');
  assert.equal(h.elements.workbench.classList.contains('editor-expanded'), false);
  assert.equal(h.context.selectedRun, null);
  assert.equal(h.context.busy, true);
  h.requests[0].resolve(record('submitted-run', 'queued'));
  await pending;
  assert.equal(h.context.selectedRun, 'submitted-run');
  assert.equal(h.context.cursor, 0);
  assert.equal(h.context.busy, false);
});

test('disabled Run leaves output, editor expansion, drafts, and requests untouched', async () => {
  const h = setup();
  h.elements['expand-editor'].onclick();
  h.elements.run.disabled = true;
  await h.context.run();
  assert.equal(h.collapsed(), true);
  assert.equal(h.elements.workbench.classList.contains('editor-expanded'), true);
  assert.deepEqual(h.requests, []);
  assert.deepEqual(h.calls, []);
});

test('restoring an active run selects its log once without changing the editor draft', () => {
  const h = setup(), source = h.elements.code.value, argumentsText = h.elements.arguments.value;
  h.context.state.runs = [record()];
  h.context.selectSession();
  assert.equal(h.context.selectedRun, 'active-run');
  assert.equal(h.context.cursor, 0);
  assert.equal(h.collapsed(), false);
  assert.equal(h.elements.code.value, source);
  assert.equal(h.elements.arguments.value, argumentsText);
  assert.equal(h.calls.filter(call => call === 'clearConsole').length, 1);
  assert.equal(h.calls.filter(call => call === 'resetResults').length, 1);
  assert.equal(h.calls.includes('loadRunDraft'), false);

  h.elements['toggle-output'].onclick();
  h.context.cursor = 123;
  for (const status of ['running', 'profiling', 'exporting', 'succeeded']) {
    h.context.state.runs[0].status = status;
    h.context.selectSession();
    h.context.restoreActiveRun();
  }
  assert.equal(h.collapsed(), true, 'polling must respect manual collapse for the selected run');
  assert.equal(h.context.cursor, 123);
  assert.equal(h.calls.filter(call => call === 'clearConsole').length, 1);
});

test('restoration ignores missing, completed, and other-session runs and preserves an existing selection', () => {
  for (const runs of [[], [record('finished', 'succeeded')], [record('other', 'running', {session: 'another-workspace'})]]) {
    const h = setup();
    h.context.state.runs = runs;
    h.context.restoreActiveRun();
    assert.equal(h.context.selectedRun, null);
    assert.equal(h.context.cursor, 75);
    assert.equal(h.collapsed(), true);
    assert.deepEqual(h.calls, []);
  }
  const h = setup();
  h.context.state.runs = [record()];
  h.context.selectedRun = 'chosen-history';
  h.context.restoreActiveRun();
  assert.equal(h.context.selectedRun, 'chosen-history');
  assert.equal(h.collapsed(), true);
});

test('polling cannot reopen manually collapsed output while a Run request is pending', async () => {
  const h = setup();
  const pending = h.context.run();
  h.elements['toggle-output'].onclick();
  h.context.state.runs = [record('already-visible-on-server', 'queued')];
  h.context.restoreActiveRun();
  assert.equal(h.context.selectedRun, null, 'the pending submission owns run selection');
  assert.equal(h.collapsed(), true);
  h.requests[0].resolve(record('already-visible-on-server', 'queued'));
  await pending;
  assert.equal(h.context.selectedRun, 'already-visible-on-server');
  assert.equal(h.collapsed(), true, 'submission completion must preserve manual collapse');
});

test('showOutput exits editor expansion while preserving the saved split ratio', () => {
  const h = setup(0.62), style = h.elements.workbench.style;
  const editorWeight = style.getPropertyValue('--editor-share');
  const outputWeight = style.getPropertyValue('--output-share');
  assert.equal(editorWeight, '62fr');
  assert.equal(outputWeight, '38fr');
  h.elements['expand-editor'].onclick();
  assert.equal(h.elements.workbench.classList.contains('editor-expanded'), true);
  h.panel.showOutput();
  assert.equal(h.collapsed(), false);
  assert.equal(h.elements.workbench.classList.contains('editor-expanded'), false);
  assert.equal(h.elements['expand-editor'].getAttribute('aria-pressed'), 'false');
  assert.equal(h.elements['toggle-output'].getAttribute('aria-expanded'), 'true');
  assert.equal(style.getPropertyValue('--editor-share'), editorWeight);
  assert.equal(style.getPropertyValue('--output-share'), outputWeight);
  assert.equal(h.values.get(shareKey), '0.62');
});

test('choosing history reveals output before loading its source and then reads its events', async () => {
  const h = setup();
  h.context.state.runs = [record('history-run', 'succeeded')];
  h.context.renderHistory();
  h.elements['expand-editor'].onclick();
  const pending = h.elements.history.children[0].onclick();
  assert.equal(h.context.selectedRun, 'history-run');
  assert.equal(h.collapsed(), false);
  assert.equal(h.elements.workbench.classList.contains('editor-expanded'), false);
  assert.equal(h.requests[0].path, '/api/runs/history-run/code');
  h.requests[0].resolve({code: 'saved history source'});
  await pending;
  assert.equal(h.elements.code.value, 'saved history source');
  assert.equal(h.calls.includes('readEvents'), true);
});
