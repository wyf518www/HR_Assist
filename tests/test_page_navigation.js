'use strict';
// Run with: node --test tests/test_page_navigation.js
// These tests execute the shipped navigation code with stubbed DOM/network APIs.
// They do not launch a browser or contact any service.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'hr_assist', 'web', 'app.js'), 'utf8')
  .replace(/\ninit\(\);\s*$/, '\n');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(handler = async () => ({})) {
  const elements = new Map(['#page', '#app', '#toast'].map(key => [key, { innerHTML: '', textContent: '', hidden: true }]));
  const calls = [], intervals = [], listeners = new Map();
  const location = { hash: '#home' };
  const context = vm.createContext({
    console, URLSearchParams, FormData, structuredClone,
    document: {
      hidden: false, title: '',
      querySelector: selector => elements.get(selector) || null,
      addEventListener: (name, callback) => listeners.set(name, callback),
    },
    location,
    history: { pushState: (_state, _title, url) => { location.hash = url; } },
    window: { addEventListener() {}, scrollTo() {} },
    setInterval: (callback, milliseconds) => { intervals.push({ callback, milliseconds }); return intervals.length; },
    setTimeout: () => 1, clearTimeout() {},
    fetch: () => { throw new Error('Unexpected real fetch attempt'); },
    diagnosticsPage: async () => '<diagnostics/>',
    jdImportsPage: async () => '<jd-imports/>',
    jdReviewPage: () => '<jd-review/>',
    identitiesPage: async () => '<identity-review/>',
    __api: async (url, options) => { calls.push({ url, options }); return handler(url, options); },
    __shell: () => {},
  });
  vm.runInContext(source, context, { filename: 'app.js' });
  vm.runInContext(`
    api=__api;
    shell=__shell;
    state.session={username:'synthetic',csrf:'synthetic',version:'test'};
    homePage=()=>'<home/>';
    overviewPage=()=>'<overview/>';
    candidatePage=()=>'<candidates/>';
    jobsPage=()=>'<jobs/>';
    detailPage=()=>'<detail/>';
    roleEditor=()=>'<role-editor/>';
    reportsPage=async()=>'<reports/>';
    runsPage=async()=>'<runs/>';
    settingsPage=async()=>'<settings-form/>';
  `, context);
  return {
    context, calls, intervals, listeners,
    run: expression => vm.runInContext(expression, context),
    page: () => elements.get('#page').innerHTML,
  };
}

const dependencies = {
  home: ['/dashboard'], overview: ['/dashboard'],
  candidates: ['/roles?summary=true', '/applications?summary=true'],
  jobs: ['/roles'], 'jd-review': ['/roles'],
  'identity-review': ['/roles?summary=true'], detail: ['/roles?summary=true'],
  runs: [], settings: [], reports: [], 'jd-imports': [], diagnostics: [], 'role-editor': [],
};

for (const [view, expected] of Object.entries(dependencies)) {
  test(`${view} loads only its declared base data`, async () => {
    const h = harness(async url => ({ marker: url }));
    const result = await h.run(`refreshBase(${JSON.stringify(view)})`);
    assert.deepEqual(h.calls.map(call => call.url).sort(), [...expected].sort());
    assert.equal(h.run('state.dashboard'), null, 'loader must not mutate shared state before navigation wins');
    assert.equal(h.run('state.roles.length'), 0);
    assert.equal(h.run('state.applications.length'), 0);
    assert.equal(typeof result, 'object');
  });
}

test('a slow previous navigation cannot overwrite a newer page or its data', async () => {
  const slow = deferred();
  const h = harness(url => {
    assert.equal(url, '/dashboard');
    return slow.promise;
  });
  const previous = h.run("navigate('overview')");
  await h.run("navigate('settings')");
  assert.equal(h.page(), '<settings-form/>');
  slow.resolve({ latest_run: { id: 'stale', status: 'RUNNING' } });
  await previous;
  assert.equal(h.run('state.view'), 'settings');
  assert.equal(h.run('state.dashboard'), null);
  assert.equal(h.page(), '<settings-form/>');
});

test('a slow renderer cannot overwrite a newly opened form', async () => {
  const slow = deferred();
  const h = harness();
  h.context.__slow = slow.promise;
  h.run('runsPage=()=>__slow');
  const previous = h.run("navigate('runs')");
  await new Promise(resolve => setImmediate(resolve));
  await h.run("navigate('settings')");
  slow.resolve('<stale-runs/>');
  await previous;
  assert.equal(h.run('state.view'), 'settings');
  assert.equal(h.page(), '<settings-form/>');
});

test('an obsolete request failure cannot replace a newer editing page', async () => {
  const slow = deferred();
  const h = harness(() => slow.promise);
  const previous = h.run("navigate('overview')");
  await h.run("navigate('settings')");
  slow.reject(new Error('synthetic old request failure'));
  await previous;
  assert.equal(h.run('state.view'), 'settings');
  assert.equal(h.page(), '<settings-form/>');
});

test('editing a job fetches its complete JD without relying on a warmed role list', async () => {
  const full = { id: 'synthetic-role', name: 'Synthetic role', jd_text: 'Full synthetic JD', fields: { skills: 'Synthetic skill' } };
  const h = harness(async url => {
    assert.equal(url, '/roles/synthetic-role');
    return full;
  });
  const button = { dataset: { action: 'edit-role', id: 'synthetic-role' }, disabled: false };
  await h.listeners.get('click')({ target: { closest: () => button } });
  assert.deepEqual(h.calls.map(call => call.url), ['/roles/synthetic-role']);
  assert.equal(h.run('state.roles.length'), 0);
  assert.equal(h.run('state.draft.jd_text'), full.jd_text);
  assert.equal(h.run('state.view'), 'role-editor');
  assert.equal(h.page(), '<role-editor/>');
  assert.equal(button.disabled, false);
});

test('polling is single flight and an old poll cannot rebuild a different page', async () => {
  const slow = deferred();
  const h = harness(url => {
    assert.equal(url, '/dashboard');
    return slow.promise;
  });
  h.run("state.view='overview';state.dashboard={latest_run:{id:'before',status:'RUNNING'}}");
  const first = h.run('pollRunStatus()');
  await h.run('pollRunStatus()');
  assert.equal(h.calls.length, 1);
  await h.run("navigate('settings')");
  slow.resolve({ latest_run: { id: 'late', status: 'RUNNING' } });
  await first;
  assert.equal(h.page(), '<settings-form/>');
  assert.equal(h.run('state.dashboard.latest_run.id'), 'before');
});

test('a completed run refreshes the visible overview without reloading unrelated collections', async () => {
  const h = harness(async url => {
    assert.equal(url, '/dashboard');
    return { latest_run: { id: 'batch', status: 'SUCCESS' } };
  });
  h.run("state.view='overview';state.dashboard={latest_run:{id:'batch',status:'RUNNING'}}");
  const revision = h.run('navigationRevision');
  await h.run('pollRunStatus()');
  assert.deepEqual(h.calls.map(call => call.url), ['/dashboard']);
  assert.equal(h.run('navigationRevision'), revision, 'poll must not call navigate');
  assert.equal(h.run('state.dashboard.latest_run.status'), 'SUCCESS');
  assert.equal(h.page(), '<overview/>');
});

test('polling does no work on editing pages, when hidden, or when logged out', async () => {
  const h = harness();
  for (const view of ['settings', 'role-editor', 'candidates', 'detail', 'jd-review', 'reports']) {
    h.run(`state.view=${JSON.stringify(view)}`);
    await h.run('pollRunStatus()');
  }
  h.run("state.view='overview';document.hidden=true");
  await h.run('pollRunStatus()');
  h.run('document.hidden=false;state.session=null');
  await h.run('pollRunStatus()');
  assert.equal(h.calls.length, 0);
});
