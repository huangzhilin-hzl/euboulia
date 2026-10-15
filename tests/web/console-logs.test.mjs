import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../../src/euboulia/web/index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const detailScript = script.slice(script.indexOf('    function stateCard('), script.indexOf('    function renderPod('));
const logScript = script.slice(script.indexOf('    function renderConsole('), script.indexOf('    function kv('));

class Element {
  constructor(tag, classes = '', text = '') {
    this.tag = tag;
    this.classes = new Set(classes.split(/\s+/).filter(Boolean));
    this.children = []; this.parent = null; this.dataset = {}; this.attributes = {};
    this.textContent = text; this.top = 0; this.left = 0;
    this.classList = {
      contains: name => this.classes.has(name),
      add: name => this.classes.add(name),
      toggle: (name, force = !this.classes.has(name)) => {
        if (force) this.classes.add(name); else this.classes.delete(name);
      },
    };
  }
  get isConnected() { return this.root || !!this.parent?.isConnected; }
  get clientHeight() { return 250; }
  get clientWidth() { return 500; }
  get scrollHeight() { return Math.max(this.clientHeight, this.textContent.split('\n').length * 20); }
  get scrollWidth() { return Math.max(this.clientWidth, ...this.textContent.split('\n').map(line => line.length * 8)); }
  get scrollTop() { return this.top; }
  // Browsers clamp assignments while the empty placeholder is displayed.
  set scrollTop(value) { this.top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); }
  get scrollLeft() { return this.left; }
  set scrollLeft(value) { this.left = Math.max(0, Math.min(value, this.scrollWidth - this.clientWidth)); }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) {
    this.children.forEach(child => { child.parent = null; });
    this.children = []; this.append(...children);
  }
  replaceWith(replacement) {
    const parent = this.parent;
    parent.children[parent.children.indexOf(this)] = replacement;
    replacement.parent = parent; this.parent = null;
  }
  matches(selector) {
    if (selector.startsWith('.')) return this.classes.has(selector.slice(1));
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    return this.tag === selector;
  }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
  closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector) || null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener() {}
}

function detail(uid) {
  return {
    run: {run_uid: uid, status: 'completed', recipe_name: uid, executor: 'test', phase: 'completed'},
    log_sources: [{source: 'controller', label: 'Controller', available: true}],
  };
}

function setup() {
  const root = new Element('section'); root.id = 'runDetail'; root.root = true;
  const state = {
    selected: 'run-a', tab: 'console', detail: detail('run-a'), revision: 0,
    logSource: 'controller', logOffsets: {}, logContent: {}, logViewports: {},
  };
  const requests = [];
  const context = vm.createContext({
    state,
    $: id => id === root.id ? root : root.querySelector(`#${id}`),
    element: (tag, classes, text) => new Element(tag, classes, text),
    clear: node => node.replaceChildren(),
    updateResultLayout() {},
    displayName: run => run.recipe_name,
    elapsedTime: () => '1s', relativeTime: () => 'Just now',
    renderPod: () => new Element('div', 'pod-panel'),
    terminal: new Set(['completed', 'failed', 'cancelled']), phaseGroups: [], phaseLabels: [],
    api: path => new Promise(resolve => requests.push({path, resolve})),
  });
  vm.runInContext(detailScript + logScript, context);
  context.renderDetail();
  function select(uid) {
    state.selected = uid; state.detail = detail(uid);
    state.logOffsets = {}; state.logContent = {};
    context.renderDetail();
  }
  return {state, root, context, requests, select, output: () => root.querySelector('#consoleOutput')};
}

const longLog = `${'a'.repeat(100)}\n`.repeat(60);
const response = (content, next_offset = content.length) => ({content, next_offset, reset: false});

test('a response for a previous run cannot replace the selected run or its log cache', async () => {
  const h = setup();
  const pending = h.context.loadLog();
  h.select('run-b');
  const output = h.output();
  h.requests[0].resolve(response('old run output'));
  await pending;
  assert.equal(h.state.selected, 'run-b');
  assert.equal(h.state.logContent.controller, undefined);
  assert.equal(h.state.logOffsets.controller, undefined);
  assert.equal(h.output(), output);
  assert.equal(output.textContent, 'Waiting for output from this source.');
});

test('reselecting the same run rejects responses belonging to the discarded log cache', async () => {
  const h = setup();
  const pending = h.context.loadLog();
  h.select('run-b'); h.select('run-a');
  h.requests[0].resolve(response('stale first visit'));
  await pending;
  assert.equal(h.state.logContent.controller, undefined);
  assert.equal(h.output().textContent, 'Waiting for output from this source.');
});

test('an in-flight log response cannot repopulate state after a successful deletion', async () => {
  const h = setup();
  const pending = h.context.loadLog();
  h.state.revision++;
  h.requests[0].resolve(response('deleted run output'));
  await pending;
  assert.equal(h.state.logContent.controller, undefined);
  assert.equal(h.state.logOffsets.controller, undefined);
});

test('concurrent reads at the same offset append the response only once', async () => {
  const h = setup();
  const first = h.context.loadLog(), second = h.context.loadLog();
  assert.equal(h.requests[0].path, h.requests[1].path);
  h.requests[1].resolve(response('one chunk\n')); await second;
  h.requests[0].resolve(response('one chunk\n')); await first;
  assert.equal(h.state.logContent.controller, 'one chunk\n');
  assert.equal(h.state.logOffsets.controller, 10);
  assert.equal(h.output().textContent, 'one chunk\n');
});

test('returning to a run restores its viewport after the loading placeholder disappears', async () => {
  const h = setup();
  let pending = h.context.loadLog();
  h.requests[0].resolve(response(longLog)); await pending;
  h.output().scrollTop = 300; h.output().scrollLeft = 120;
  h.select('run-b'); h.select('run-a');
  assert.equal(h.output().scrollTop, 0, 'the placeholder cannot scroll');
  assert.equal(h.output().scrollLeft, 0);
  h.context.renderDetail(); // Polling while the first log request is pending.
  pending = h.context.loadLog();
  h.requests[1].resolve(response(longLog)); await pending;
  assert.equal(h.output().scrollTop, 300);
  assert.equal(h.output().scrollLeft, 120);
  assert.equal(h.state.logViewports['run-a:controller'].followTail, false);
});

test('detail polling retains the log DOM, selection surface, and scrolled position', async () => {
  const h = setup();
  let pending = h.context.loadLog();
  h.requests[0].resolve(response(longLog)); await pending;
  const output = h.output(), content = output.closest('.tab-content');
  const hero = h.root.querySelector('.detail-hero');
  output.scrollTop = 300; output.scrollLeft = 120;
  h.state.detail = detail('run-a');
  h.context.renderDetail();
  assert.equal(h.output(), output);
  assert.equal(output.closest('.tab-content'), content);
  assert.notEqual(h.root.querySelector('.detail-hero'), hero);
  assert.equal(h.root.children.length, 4, 'refresh must replace rather than duplicate sections');
  pending = h.context.loadLog();
  h.requests[1].resolve(response('new line\n', longLog.length + 9)); await pending;
  assert.equal(output.scrollTop, 300);
  assert.equal(output.scrollLeft, 120);
  assert.equal(output.textContent, longLog + 'new line\n');
});

test('a viewport following the tail continues following newly appended output', async () => {
  const h = setup();
  let pending = h.context.loadLog();
  h.requests[0].resolve(response(longLog)); await pending;
  assert.equal(h.output().scrollTop, h.output().scrollHeight - h.output().clientHeight);
  const previousTop = h.output().scrollTop;
  pending = h.context.loadLog();
  h.requests[1].resolve(response('new line\n', longLog.length + 9)); await pending;
  assert.ok(h.output().scrollTop > previousTop);
  assert.equal(h.output().scrollTop, h.output().scrollHeight - h.output().clientHeight);
});
