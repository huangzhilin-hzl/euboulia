import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/euboulia/playground/web/choices.js', import.meta.url), 'utf8');
new vm.Script(source);
const classSource = source.slice(source.indexOf('  class Choice {'), source.indexOf('  document.addEventListener('));

// A small DOM model records the identity and structural mutations that matter
// for polling, focus, and menu scroll; tests execute the shipped Choice methods.
class Element {
  constructor() {
    this.children = []; this.dataset = {}; this.attributes = new Map(); this.className = '';
    this.text = ''; this.scrollTop = 0; this.style = {}; this.structureWrites = 0;
    this.title = ''; this.disabled = false; this.tabIndex = -1;
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      toggle: (name, force) => {
        const names = new Set(this.className.split(' ').filter(Boolean));
        const enabled = force ?? !names.has(name);
        if (enabled) names.add(name); else names.delete(name);
        this.className = [...names].join(' '); return enabled;
      },
    };
  }
  get textContent() { return this.children.length ? this.children.map(child => child.textContent).join('') : this.text; }
  set textContent(value) { this.text = value; this.children.forEach(child => { child.parent = null; }); this.children = []; this.structureWrites++; }
  get firstElementChild() { return this.children[0] || null; }
  get nextElementSibling() { return this.parent?.children[this.parent.children.indexOf(this) + 1] || null; }
  get offsetTop() { return (this.parent?.children.indexOf(this) || 0) * 30; }
  get offsetHeight() { return 30; }
  get clientHeight() { return 90; }
  append(...children) { for (const child of children) this.insertBefore(child, null); }
  insertBefore(child, next) {
    if (child.parent) child.remove();
    const index = next ? this.children.indexOf(next) : this.children.length;
    this.children.splice(index, 0, child); child.parent = this; this.structureWrites++;
  }
  replaceChildren(...children) {
    this.children.forEach(child => { child.parent = null; }); this.children = [];
    this.structureWrites++; this.append(...children);
  }
  remove() {
    if (!this.parent) return;
    this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent.structureWrites++; this.parent = null;
  }
  querySelector(selector) {
    for (const child of this.children) {
      if (selector === ':focus' ? child.focused : child.classList.contains(selector.slice(1))) return child;
      const nested = child.querySelector(selector); if (nested) return nested;
    }
    return null;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  focus() { this.focused = true; }
  getBoundingClientRect() { return { width: 200, left: 10, top: 100, bottom: 150 }; }
}
function option(value, status = 'idle', summary = 'All 8 idle') {
  return { value, text: `${value} · ${summary}`, disabled: false, title: `${value} Checked 12:00:00`,
    dataset: { choiceLabel: value, choiceStatus: status, choiceSummary: summary, choiceBrief: summary } };
}
function setup(options = [option('a'), option('b'), option('c')]) {
  const context = vm.createContext({ document: { createElement: () => new Element() },
    window: { innerWidth: 900, innerHeight: 800 }, Event: class { constructor(type) { this.type = type; } },
    CustomEvent: class { constructor(type) { this.type = type; } },
  });
  vm.runInContext('let opened = null; const statuses = new Set(["idle","partial","busy","unknown","not_ready","no_gpu"]); var Choice = (' + classSource + ');', context);
  const choice = Object.create(context.Choice.prototype);
  const select = new Element(); select.options = options; select.value = options[0]?.value || ''; select.id = 'node';
  Object.defineProperty(select, 'selectedIndex', { get: () => select.options.findIndex(o => o.value === select.value) });
  select.events = []; select.dispatchEvent = event => select.events.push(event.type);
  Object.assign(choice, { select, root: new Element(), text: new Element(), popup: new Element(),
    trigger: new Element(), label: 'Node', segmented: false, key: '', active: 0, prefix: '' });
  choice.root.append(select, choice.trigger); choice.trigger.append(choice.text);
  choice.sync();
  return { choice, context, open() { context.current = choice; vm.runInContext('opened = current;', context); } };
}

test('unchanged polling and timestamp-only updates reuse the trigger and every menu row', () => {
  const { choice } = setup();
  const label = choice.text.children[0], status = choice.text.children[1], rows = [...choice.items];
  const writes = choice.popup.structureWrites;
  choice.popup.scrollTop = 42;
  for (let i = 0; i < 20; i++) choice.sync();
  choice.select.options.forEach(o => { o.title = o.title.replace('12:00:00', '12:00:15'); });
  choice.sync();
  assert.equal(choice.text.children[0], label);
  assert.equal(choice.text.children[1], status);
  rows.forEach((row, index) => assert.equal(choice.items[index], row));
  assert.equal(choice.popup.structureWrites, writes);
  assert.equal(choice.popup.scrollTop, 42);
  assert.match(choice.items[0].title, /12:00:15/);
});

test('a real GPU status update changes text/color in place and keeps keyboard focus', () => {
  const { choice } = setup();
  const row = choice.items[0], label = row.children[0], status = row.children[1];
  row.focus();
  Object.assign(choice.select.options[0], option('a', 'busy', 'Busy'));
  choice.sync();
  assert.equal(choice.items[0], row);
  assert.equal(row.children[0], label);
  assert.equal(row.children[1], status);
  assert.equal(status.textContent, 'Busy');
  assert.equal(status.dataset.status, 'busy');
  assert.equal(row.focused, true);
});

test('an open node menu freezes row order and selects by value after the model reorders', () => {
  const h = setup(), { choice } = h;
  const [a, b, c] = choice.items;
  h.open(); choice.active = 1; choice.popup.scrollTop = 42;
  const [oa, ob, oc] = choice.select.options;
  choice.select.options = [ob, oa, oc];
  ob.title = 'b Checked 12:00:15';
  choice.sync();
  assert.deepEqual(Array.from(choice.items), [a, b, c]);
  assert.equal(choice.items[choice.active], b);
  assert.equal(choice.popup.scrollTop, 42);
  b.onclick();
  assert.equal(choice.select.value, 'b', 'the retained second row maps to b, now first in the select model');
  assert.deepEqual(Array.from(choice.items), [b, a, c], 'closing applies the latest idle-first order');
  assert.deepEqual(choice.select.events, ['choice-select', 'change']);
});

test('keyboard Enter also chooses the highlighted value in a frozen menu', () => {
  const h = setup(), { choice } = h;
  h.open(); choice.active = 1;
  choice.select.options.reverse(); choice.sync();
  choice.keydown({ key: 'Enter', preventDefault() {} });
  assert.equal(choice.select.value, 'b');
  assert.deepEqual(Array.from(choice.items, item => item.dataset.value), ['c', 'b', 'a']);
});

test('a highlighted node that becomes disabled during model reordering moves to an available row', () => {
  const h = setup(), { choice } = h;
  h.open(); choice.highlight(1, false);
  const [a, b, c] = choice.items, [oa, ob, oc] = choice.select.options;
  assert.equal(choice.trigger.getAttribute('aria-activedescendant'), b.id);
  ob.disabled = true;
  choice.select.options = [ob, oa, oc];
  choice.sync();
  assert.deepEqual(Array.from(choice.items), [a, b, c]);
  assert.equal(choice.items[choice.active], c);
  assert.equal(choice.trigger.getAttribute('aria-activedescendant'), c.id);
  assert.equal(b.classList.contains('active'), false);
  assert.equal(c.classList.contains('active'), true);
  choice.keydown({ key: 'Enter', preventDefault() {} });
  assert.equal(choice.select.value, 'c');
});

test('when every refreshed node is disabled the menu clears active state and its ARIA reference', () => {
  const h = setup(), { choice } = h;
  h.open(); choice.highlight(1, false);
  choice.select.options.forEach(option => { option.disabled = true; });
  choice.select.options.reverse();
  choice.sync();
  assert.equal(choice.active, -1);
  assert.equal(choice.trigger.getAttribute('aria-activedescendant'), null);
  assert.equal(choice.items.some(item => item.classList.contains('active')), false);
  choice.keydown({ key: 'Enter', preventDefault() {} });
  assert.equal(choice.select.value, 'a');
  assert.deepEqual(choice.select.events, []);
});
