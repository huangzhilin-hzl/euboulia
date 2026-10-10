import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../../src/euboulia/playground/web/app.js', import.meta.url), 'utf8');
new vm.Script(script);
const draftHelpers = script.slice(script.indexOf('function draftKey('), script.indexOf('function argumentLines('));
const profileFunctions = script.slice(script.indexOf('function saveDraft('), script.indexOf('function resetResults('));
const runFunction = script.slice(script.indexOf('async function run('), script.indexOf('$("error").onclick ='));
const key = name => `molou-playground-draft-${name}`;
const marker = 'molou-playground-last-profile';
const defaults = {run: '', ncu: '--set full --launch-count 1', nsys: '--trace cuda,osrt'};
const gpu = {name: 'gpu', label: 'GPU environment', code: 'unified example'};
const legacy = ['cutedsl', 'tilelang', 'python'].map(name => ({name, label: name, code: `${name} example`}));
const bundle = (name, code = `${name} code`, argumentsText = `${name} args`, profiling = {}) => ({
  [key(name)]: code,
  [`${key(name)}-arguments`]: argumentsText,
  [`${key(name)}-profiling`]: JSON.stringify(profiling),
});

function setup(initial = {}, profiles = [gpu], storageOptions = {}) {
  const values = new Map(Object.entries(initial)), requests = [];
  const wrapper = {hidden: true};
  const elements = Object.fromEntries([
    'code', 'arguments', 'profile', 'mode', 'profiler-arguments', 'saved', 'profiling-settings',
    'ncu-reports', 'profiler-preview-text', 'run', 'profiling-hint', 'gpu',
  ].map(id => [id, {value: '', textContent: '', hidden: false}]));
  elements.profile.closest = selector => {
    assert.equal(selector, '.choice-control'); return wrapper;
  };
  elements.gpu.value = '0';
  const storage = {
    getItem(name) {
      if (storageOptions.blocked) throw new Error('Blocked storage');
      return values.has(name) ? values.get(name) : null;
    },
    setItem(name, value) {
      if (storageOptions.blocked || name === storageOptions.failKey) throw new Error('Quota exceeded');
      values.set(name, String(value));
    },
    removeItem(name) { values.delete(name); },
  };
  const context = vm.createContext({
    $: id => elements[id], localStorage: storage,
    config: {profiles, profiler_defaults: {...defaults}},
    choices: {sync() {}},
    options(select, entries) { select.options = entries; },
    updateArguments() {}, highlight() {}, updateCursor() {},
    action: async callback => callback(),
    api: async (path, body) => { requests.push({path, body}); return {id: 'new-run'}; },
    clearConsole() {}, resetResults() {}, badge() {}, renderHistory() {},
  });
  vm.runInContext('var currentProfile = "", editorTouched = false, currentMode = "run", profilerDraft = {}, session = {id: "workspace"}, state = {runs: []}, selectedRun = null, cursor = 0;' + draftHelpers + profileFunctions + runFunction, context);
  return {context, elements, values, wrapper, requests};
}

test('single GPU environment hides only its selector and migrates the latest complete draft', () => {
  const original = {
    ...bundle('cutedsl'),
    ...bundle('tilelang', 'import tilelang', '--size 32', {mode: 'nsys', ncu: '--kernel-name custom', nsys: '--trace cuda --sample none'}),
    [marker]: 'tilelang',
  };
  const h = setup(original);
  h.context.initializeProfiles();
  assert.equal(h.wrapper.hidden, true);
  assert.equal(h.context.currentProfile, 'gpu');
  assert.equal(h.elements.code.value, 'import tilelang');
  assert.equal(h.elements.arguments.value, '--size 32');
  assert.equal(h.context.currentMode, 'nsys');
  assert.equal(h.elements['profiler-arguments'].value, '--trace cuda --sample none');
  assert.equal(h.context.profilerDraft.ncu, '--kernel-name custom');
  assert.equal(h.values.get(key('gpu')), original[key('tilelang')]);
  assert.equal(h.values.get(`${key('gpu')}-arguments`), original[`${key('tilelang')}-arguments`]);
  assert.equal(h.values.get(`${key('gpu')}-profiling`), original[`${key('tilelang')}-profiling`]);
  for (const [name, value] of Object.entries(original)) {
    if (name !== marker) assert.equal(h.values.get(name), value, 'legacy keys must stay intact');
  }
  assert.equal(h.values.get(marker), 'gpu');
});

test('missing or stale recent-profile markers fall back in legacy order using one source bundle', () => {
  for (const recent of [undefined, 'missing', 'gpu']) {
    const h = setup({...bundle('python'), ...bundle('tilelang'), ...(recent ? {[marker]: recent} : {})});
    h.context.initializeProfiles();
    assert.equal(h.elements.code.value, 'tilelang code');
    assert.equal(h.elements.arguments.value, 'tilelang args');
    assert.equal(h.values.get(key('gpu')), 'tilelang code');
  }
  const h = setup({...bundle('python'), ...bundle('tilelang'), [key('cutedsl')]: ''});
  h.context.initializeProfiles();
  assert.equal(h.elements.code.value, '', 'an empty saved code is a draft');
  assert.equal(h.elements.arguments.value, '', 'missing sibling data must not come from another profile');
  assert.equal(h.context.currentMode, 'run');
});

test('a recent custom profile draft can migrate to GPU without replacing its source', () => {
  const h = setup({...bundle('custom-cuda'), [marker]: 'custom-cuda'});
  h.context.initializeProfiles();
  assert.equal(h.elements.code.value, 'custom-cuda code');
  assert.equal(h.values.get(key('custom-cuda')), 'custom-cuda code');
});

test('existing GPU drafts including empty code are never replaced by migration', () => {
  for (const code of ['existing gpu code', '']) {
    const original = {...bundle('gpu', code, '--existing', {mode: 'ncu', ncu: '--kernel-name existing'}), ...bundle('tilelang'), [marker]: 'tilelang'};
    const h = setup(original);
    h.context.initializeProfiles();
    assert.equal(h.elements.code.value, code);
    assert.equal(h.elements.arguments.value, '--existing');
    assert.equal(h.context.currentMode, 'ncu');
    for (const name of [key('gpu'), `${key('gpu')}-arguments`, `${key('gpu')}-profiling`]) {
      assert.equal(h.values.get(name), original[name]);
    }
  }
});

test('partially existing GPU settings are preserved rather than mixed with a legacy bundle', () => {
  const h = setup({...bundle('cutedsl'), [`${key('gpu')}-arguments`]: '--gpu-only'});
  h.context.initializeProfiles();
  assert.equal(h.elements.code.value, 'unified example');
  assert.equal(h.elements.arguments.value, '--gpu-only');
  assert.equal(h.values.has(key('gpu')), false);
});

test('migration quota errors roll back new keys and still load the complete source in memory', () => {
  const h = setup(bundle('cutedsl', 'keep my work', '--count 7', {mode: 'nsys', nsys: '--sample none'}), [gpu], {failKey: `${key('gpu')}-profiling`});
  h.context.initializeProfiles();
  assert.equal(h.elements.code.value, 'keep my work');
  assert.equal(h.elements.arguments.value, '--count 7');
  assert.equal(h.context.currentMode, 'nsys');
  assert.equal(h.elements['profiler-arguments'].value, '--sample none');
  assert.equal(h.values.has(`${key('gpu')}-arguments`), false);
  assert.equal(h.values.has(key('gpu')), false);
  assert.equal(h.values.get(key('cutedsl')), 'keep my work');
});

test('a failed migration retains the recent source across reloads until the GPU draft is saved', () => {
  const storageOptions = {failKey: `${key('gpu')}-profiling`};
  const h = setup({...bundle('cutedsl', 'older draft'), ...bundle('tilelang', 'latest draft', '--latest', {mode: 'ncu', ncu: '--latest-kernel'}), [marker]: 'tilelang'}, [gpu], storageOptions);
  h.context.initializeProfiles();
  assert.equal(h.elements.code.value, 'latest draft');
  assert.equal(h.values.get(marker), 'tilelang');
  assert.equal(h.values.has(key('gpu')), false);

  const reloaded = setup(Object.fromEntries(h.values));
  reloaded.context.initializeProfiles();
  assert.equal(reloaded.elements.code.value, 'latest draft');
  assert.equal(reloaded.elements.arguments.value, '--latest');
  assert.equal(reloaded.context.currentMode, 'ncu');
  assert.equal(reloaded.elements['profiler-arguments'].value, '--latest-kernel');
  assert.equal(reloaded.values.get(marker), 'gpu');

  h.context.loadRunDraft({profile: 'python', mode: 'run'}, 'loaded history');
  assert.equal(h.values.get(marker), 'tilelang', 'failed saves and history loading must not discard migration priority');
  delete storageOptions.failKey;
  h.context.saveDraft();
  assert.equal(h.values.get(marker), 'gpu');
  assert.equal(h.values.get(key('gpu')), 'loaded history');
});

test('a failed draft save after migration failure cannot leave a partial GPU bundle that masks the source', () => {
  const original = {
    ...bundle('cutedsl', 'older draft'),
    ...bundle('tilelang', 'latest source', '--source-args', {mode: 'ncu', ncu: '--kernel-name source'}),
    [marker]: 'tilelang',
  };
  const h = setup(original, [gpu], {failKey: `${key('gpu')}-profiling`});
  h.context.initializeProfiles();
  h.elements.code.value = 'edited but not saved';
  h.context.saveDraft();
  assert.match(h.elements.saved.textContent, /Draft not saved/);
  for (const name of [key('gpu'), `${key('gpu')}-arguments`, `${key('gpu')}-profiling`]) {
    assert.equal(h.values.has(name), false, 'failed migration retries must roll back newly written GPU keys');
  }
  for (const [name, value] of Object.entries(original)) assert.equal(h.values.get(name), value);

  const reloaded = setup(Object.fromEntries(h.values));
  reloaded.context.initializeProfiles();
  assert.equal(reloaded.elements.code.value, 'latest source');
  assert.equal(reloaded.elements.arguments.value, '--source-args');
  assert.equal(reloaded.context.currentMode, 'ncu');
  assert.equal(reloaded.elements['profiler-arguments'].value, '--kernel-name source');
  assert.equal(reloaded.values.get(marker), 'gpu');
});

test('blocked storage and malformed profiling settings do not prevent initialization', () => {
  const blocked = setup({}, [gpu], {blocked: true});
  assert.doesNotThrow(() => blocked.context.initializeProfiles());
  assert.equal(blocked.elements.code.value, 'unified example');
  for (const raw of ['bad json', 'null', '{"ncu":42,"mode":"toString"}']) {
    const h = setup({[key('gpu')]: 'keep my code', [`${key('gpu')}-profiling`]: raw});
    assert.doesNotThrow(() => h.context.initializeProfiles());
    assert.equal(h.elements.code.value, 'keep my code');
    assert.equal(h.context.currentMode, 'run');
    assert.equal(h.context.profilerDraft.ncu, defaults.ncu);
  }
});

test('edits made while configuration is loading survive initialization', () => {
  const h = setup();
  h.elements.code.value = 'typed before config returned';
  h.elements.arguments.value = '--pending';
  h.context.saveDraft();
  assert.equal(h.values.has(key('')), false, 'uninitialized edits must not save under an empty profile');
  h.context.initializeProfiles();
  assert.equal(h.elements.code.value, 'typed before config returned');
  assert.equal(h.elements.arguments.value, '--pending');
  assert.equal(h.values.get(key('gpu')), 'typed before config returned');
  assert.equal(h.values.get(`${key('gpu')}-arguments`), '--pending');
});

test('multiple legacy environments keep their selector and restore the recently used environment', () => {
  const h = setup({...bundle('python'), [marker]: 'python'}, legacy);
  h.context.initializeProfiles();
  assert.equal(h.wrapper.hidden, false);
  assert.equal(h.context.currentProfile, 'python');
  assert.equal(h.elements.code.value, 'python code');
  assert.equal(h.values.has(key('gpu')), false);
  h.context.saveDraft();
  h.context.setProfile('tilelang');
  assert.equal(h.values.get(marker), 'tilelang');
  h.elements.code.value = 'edited tilelang';
  h.elements.arguments.value = '--tile';
  h.context.setMode('ncu'); h.elements['profiler-arguments'].value = '--kernel-name tile';
  h.context.saveDraft();
  assert.equal(h.values.get(key('tilelang')), 'edited tilelang');
  assert.equal(JSON.parse(h.values.get(`${key('tilelang')}-profiling`)).ncu, '--kernel-name tile');
  assert.equal(h.values.get(marker), 'tilelang');
});

test('loading old history preserves current GPU work then restores old code, arguments, and profiler for GPU execution', async () => {
  const h = setup(bundle('gpu', 'current code', '--current', {mode: 'nsys', nsys: '--current-profiler'}));
  h.context.initializeProfiles();
  h.elements.code.value = 'unsaved current edit';
  h.elements.arguments.value = '--unsaved';
  h.elements['profiler-arguments'].value = '--unsaved-profiler';
  h.context.loadRunDraft({profile: 'cutedsl', arguments: '--history', mode: 'ncu', profiler_arguments: '--kernel-name old'}, 'old cute kernel');
  assert.equal(h.values.get(key('gpu')), 'unsaved current edit');
  assert.equal(h.values.get(`${key('gpu')}-arguments`), '--unsaved');
  assert.equal(JSON.parse(h.values.get(`${key('gpu')}-profiling`)).nsys, '--unsaved-profiler');
  assert.equal(h.context.currentProfile, 'gpu');
  assert.equal(h.elements.profile.value, 'gpu');
  assert.equal(h.elements.code.value, 'old cute kernel');
  assert.equal(h.elements.arguments.value, '--history');
  assert.equal(h.context.currentMode, 'ncu');
  assert.equal(h.elements['profiler-arguments'].value, '--kernel-name old');
  await h.context.run();
  assert.equal(h.requests[0].path, '/api/runs');
  assert.equal(h.requests[0].body.profile, 'gpu');
  assert.equal(h.requests[0].body.code, 'old cute kernel');
  assert.equal(h.requests[0].body.arguments, '--history');
  assert.equal(h.requests[0].body.mode, 'ncu');
  assert.equal(h.requests[0].body.profiler_arguments, '--kernel-name old');
});

test('history loading keeps legacy configured environments and missing-profile runs use the current environment', () => {
  const h = setup(bundle('python'), legacy);
  h.context.initializeProfiles();
  h.context.loadRunDraft({profile: 'tilelang', arguments: '', mode: 'run'}, 'tile history');
  assert.equal(h.context.currentProfile, 'tilelang');
  assert.equal(h.elements.code.value, 'tile history');
  h.context.loadRunDraft({profile: 'removed-profile', arguments: '--removed', mode: 'nsys', profiler_arguments: '--trace cuda'}, 'other history');
  assert.equal(h.values.get(key('tilelang')), 'tile history');
  assert.equal(h.context.currentProfile, 'tilelang');
  assert.equal(h.elements.code.value, 'other history');
  assert.equal(h.elements.arguments.value, '--removed');
  assert.equal(h.context.currentMode, 'nsys');
});

test('the environment selector stays hidden during configuration loading while GPU and execution mode controls remain', () => {
  const html = readFileSync(new URL('../../src/euboulia/playground/web/index.html', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../src/euboulia/playground/web/style.css', import.meta.url), 'utf8');
  const choices = readFileSync(new URL('../../src/euboulia/playground/web/choices.js', import.meta.url), 'utf8');
  assert.match(html, /id="profile" data-hide-until-config="true"/);
  assert.match(choices, /this\.root\.hidden = select\.dataset\.hideUntilConfig === "true"/);
  assert.match(css, /\[hidden\]\{display:none!important\}/, 'author display rules must not reveal hidden wrappers');
  assert.match(html, /id="gpu"/);
  assert.match(html, /id="mode"[\s\S]*value="run"[\s\S]*value="ncu"[\s\S]*value="nsys"/);
});
