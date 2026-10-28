'use strict';
// Execute the shipped query, rendering, event handlers and batch-selection code
// against a small DOM surface. No browser, model or production service is used.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const decode = value => String(value).replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const attribute = (html, name) => decode(html.match(new RegExp(`(?:^|\\s)${name}="([^"]*)"`))?.[1] || '');

function candidate(id, score, overrides = {}) {
  return {
    id, name: `Synthetic ${id}`, role_id: 'eng', role_name: 'Engineer',
    original_name: `${id}.pdf`, status: score === null ? 'QUEUED' : 'ASSESSED',
    created_at: '2026-10-28T10:00:00+00:00', recommended_at: score === null ? null : '2026-10-28',
    assessment: score === null ? null : {score, grade: score >= 85 ? '强烈推荐' : '推荐', provisional: false},
    revision: 4, manual: {'推荐给用人部门日期': null, '用人部门复筛结果': null, '约面时间': null, '面试状态': null, '面试结果': null, '面试评价': 'Preserve feedback', '当前状态': 'Preserve status'},
    ...overrides,
  };
}

function harness(candidates) {
  const elements = new Map(), controls = new Map(), listeners = new Map(), headers = new Map();
  let boxes = [];
  const calls = [], roles = [{id: 'eng', name: 'Engineer', active: true}, {id: 'sales', name: 'Sales', active: true}];
  const location = {hash: '#candidates'};

  class Element {
    constructor(tag = 'div', dataset = {}) {
      this.tag = tag; this.dataset = dataset; this.hidden = false; this.disabled = false;
      this.textContent = ''; this.value = ''; this.checked = false; this.attributes = {};
      this._html = ''; this.name = '';
    }
    set innerHTML(value) {
      this._html = value;
      if (this === elements.get('#page')) mount(value);
      if (this === elements.get('#candidate-rows')) mountRows(value);
    }
    get innerHTML() { return this._html; }
    set outerHTML(value) { mountControls(value); }
    setAttribute(name, value) { this.attributes[name] = value; }
    querySelector(selector) { return selector === 'span' ? this.arrow : null; }
    closest(selector) { return selector === '[data-action]' && this.dataset.action ? this : null; }
    matches(selector) {
      return (selector === 'input[data-candidate-filter]' && this.tag === 'input' && !!this.dataset.candidateFilter)
        || (selector === 'select[data-candidate-filter]' && this.tag === 'select' && !!this.dataset.candidateFilter);
    }
  }
  function mountRows(html) {
    boxes = [...html.matchAll(/<input\b[^>]*name="batch-candidate"[^>]*>/g)].map(([tag]) => {
      const box = new Element('input'); box.value = attribute(tag, 'value'); box.name = 'batch-candidate'; return box;
    });
  }
  function mountControls(html) {
    for (const match of html.matchAll(/<(input|select)\b([^>]*data-candidate-filter="([^"]+)"[^>]*)>([\s\S]*?)(?=<\/select>|<\/label>)/g)) {
      const [, tag, attrs, key, body] = match, element = new Element(tag, {candidateFilter: key});
      for (const name of ['type', 'inputmode', 'maxlength']) element.attributes[name] = attribute(attrs, name);
      if (tag === 'input') element.value = attribute(attrs, 'value');
      else {
        const options = [...body.matchAll(/<option\b([^>]*)>/g)];
        const selected = options.find(option => /\sselected(?:\s|$)/.test(option[1])) || options[0];
        element.value = selected ? attribute(selected[1], 'value') : '';
      }
      controls.set(key, element);
    }
  }
  function mount(html) {
    if (!html.includes('id="candidate-rows"')) return;
    for (const id of ['candidate-rows', 'candidate-result-count', 'candidate-pagination', 'candidate-filter-error', 'batch-selection-count']) {
      const element = new Element(), match = html.match(new RegExp(`<([a-z]+)\\b([^>]*id="${id}"[^>]*)>([\\s\\S]*?)<\\/\\1>`));
      elements.set('#' + id, element);
      if (match) { element._html = match[3]; element.textContent = decode(match[3]); element.hidden = /\shidden(?:\s|$)/.test(match[2]); }
    }
    elements.set('.candidate-filters', new Element());
    const batch = new Element('button', {action: 'batch-open'}); batch.disabled = true;
    elements.set('[data-action=batch-open]', batch);
    mountControls(html);
    mountRows(elements.get('#candidate-rows').innerHTML);
    headers.clear();
    for (const [, key, order, contents] of html.matchAll(/<th data-candidate-sort-header="([^"]+)" aria-sort="([^"]+)">([\s\S]*?)<\/th>/g)) {
      const header = new Element('th', {candidateSortHeader: key}); header.attributes['aria-sort'] = order;
      header.arrow = new Element('span'); header.arrow.textContent = contents.match(/<span[^>]*>([^<]*)/)[1]; headers.set(key, header);
    }
  }
  for (const key of ['#page', '#app', '#toast']) elements.set(key, new Element());
  const context = vm.createContext({
    console, URLSearchParams, FormData, structuredClone,
    document: {
      hidden: false, title: '',
      querySelector: selector => {
        const key = selector.match(/^\[data-candidate-filter="([^"]+)"\]$/)?.[1];
        return key ? controls.get(key) : elements.get(selector) || null;
      },
      querySelectorAll: selector => {
        if (selector === '[name=batch-candidate]') return boxes;
        if (selector === '[name=batch-candidate]:checked') return boxes.filter(box => box.checked);
        if (selector === '[data-candidate-sort-header]') return [...headers.values()];
        throw new Error('Unexpected selector: ' + selector);
      },
      addEventListener: (name, callback) => listeners.set(name, callback),
      createElement: () => { throw new Error('Unexpected dialog creation'); },
    },
    location, history: {pushState: (_state, _title, url) => {location.hash = url;}},
    window: {addEventListener() {}, scrollTo() {}},
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    fetch: () => {throw new Error('Unexpected real network request');},
    diagnosticsPage: () => '', jdImportsPage: () => '', jdReviewPage: () => '', identitiesPage: () => '',
    __rows: structuredClone(candidates), __roles: roles,
    __api: async (url, options) => {
      calls.push({url, options});
      if (url === '/applications?summary=true') return context.__rows;
      if (url === '/roles?summary=true') return roles;
      throw new Error('Unexpected API: ' + url);
    },
  });
  for (const filename of ['candidate-query.js', 'candidate-list.js', 'batch-followup.js', 'app.js']) {
    const source = fs.readFileSync(path.join(__dirname, '..', 'hr_assist', 'web', filename), 'utf8').replace(/\ninit\(\);\s*$/, '\n');
    vm.runInContext(source, context, {filename});
  }
  const run = expression => vm.runInContext(expression, context);
  run("api=__api;shell=()=>{};detailPage=()=>'<detail/>';state.session={username:'synthetic',csrf:'synthetic'};");
  return {
    run, calls, controls, headers,
    load: () => run("navigate('candidates')"),
    ids: () => boxes.map(box => box.value), selected: () => boxes.filter(box => box.checked).map(box => box.value),
    node: selector => elements.get(selector),
    change: (key, value) => {
      const target = controls.get(key); assert.ok(target, 'Expected rendered control ' + key); target.value = String(value);
      listeners.get(target.tag === 'input' ? 'input' : 'change')({target});
    },
    click: (action, extra = {}) => {
      const target = new Element('button', {action, ...extra});
      return listeners.get('click')({target});
    },
  };
}

test('rendered input and select events combine filters and update rows and result count', async () => {
  const h = harness([candidate('a', 95), candidate('b', 71), candidate('c', null, {role_id:'sales',role_name:'Sales'}), candidate('d', 89, {manual:{'面试状态':'已面','面试结果':'通过'}})]);
  await h.load();
  assert.equal(h.ids().length, 4);
  h.change('search', 'synthetic');
  h.change('role', 'eng');
  h.change('grade', '强烈推荐');
  assert.deepEqual(h.ids(), ['a', 'd']);
  assert.match(h.node('#candidate-result-count').textContent, /筛选出 2 人 \/ 共 4 人/);
  h.change('interviewStatus', '已面');
  assert.deepEqual(h.ids(), ['d']);
  h.change('status', 'QUEUED');
  assert.deepEqual(h.ids(), []);
  assert.match(h.node('#candidate-result-count').textContent, /筛选出 0 人/);
  assert.deepEqual(h.calls.map(call => call.url).sort(), ['/applications?summary=true', '/roles?summary=true']);
});

test('sort menu and score/status table headers stay synchronized with visible order', async () => {
  const h = harness([candidate('a', 20), candidate('b', 90), candidate('c', null), candidate('d', 0)]);
  await h.load();
  h.change('sort', 'score_desc');
  assert.deepEqual(h.ids(), ['b', 'a', 'd', 'c']);
  assert.equal(h.headers.get('score').attributes['aria-sort'], 'descending');
  assert.equal(h.headers.get('score').arrow.textContent, '↓');
  await h.click('candidate-sort', {sort:'score'});
  assert.deepEqual(h.ids(), ['d', 'a', 'b', 'c']);
  assert.equal(h.controls.get('sort').value, 'score_asc');
  assert.equal(h.headers.get('score').attributes['aria-sort'], 'ascending');
  await h.click('candidate-sort', {sort:'status'});
  assert.equal(h.ids()[0], 'c');
  assert.equal(h.controls.get('sort').value, 'status_asc');
  assert.equal(h.headers.get('score').attributes['aria-sort'], 'none');
  assert.equal(h.headers.get('status').attributes['aria-sort'], 'ascending');
});

test('reset restores all controls, sorting and pagination defaults', async () => {
  const h = harness([candidate('a', 0), candidate('b', 90), candidate('c', null)]);
  await h.load();
  h.change('scoreMin', '80'); h.change('sort', 'score_desc'); h.change('pageSize', '100');
  assert.deepEqual(h.ids(), ['b']);
  await h.click('candidate-reset');
  assert.equal(h.ids().length, 3);
  assert.equal(h.controls.get('scoreMin').value, '');
  assert.equal(h.controls.get('sort').value, 'recommended_desc');
  assert.equal(h.controls.get('pageSize').value, '50');
  assert.equal(h.run('candidateView.page'), 1);
});

test('select all is page-scoped and page, filter and sort changes clear selections', async () => {
  const h = harness(Array.from({length: 52}, (_, i) => candidate('row' + String(i).padStart(3, '0'), i)));
  await h.load();
  const firstPage = h.ids(); assert.equal(firstPage.length, 50);
  await h.click('batch-select-all');
  assert.deepEqual(h.selected(), firstPage);
  assert.equal(h.node('[data-action=batch-open]').disabled, false);
  await h.click('candidate-page', {page:'2'});
  assert.equal(h.ids().length, 2); assert.equal(h.selected().length, 0);
  assert.equal(h.node('[data-action=batch-open]').disabled, true);
  await h.click('batch-select-all'); assert.equal(h.selected().length, 2);
  h.change('sort', 'score_desc'); assert.equal(h.selected().length, 0); assert.equal(h.run('candidateView.page'), 1);
  await h.click('batch-select-all');
  h.change('search', 'row000');
  assert.deepEqual(h.ids(), ['row000']); assert.equal(h.selected().length, 0);
  assert.equal(h.node('#batch-selection-count').textContent, '已选 0 人');
});

test('invalid score bounds show error, remove selectable rows and cannot open bulk editor', async () => {
  const h = harness([candidate('a', 0), candidate('b', 90), candidate('c', null)]);
  await h.load(); await h.click('batch-select-all');
  h.change('scoreMin', '80'); h.change('scoreMax', '30');
  assert.equal(h.node('#candidate-filter-error').hidden, false);
  assert.match(h.node('#candidate-filter-error').textContent, /最低分不能高于最高分/);
  assert.deepEqual(h.ids(), []); assert.deepEqual(h.selected(), []);
  assert.equal(h.node('[data-action=batch-open]').disabled, true);
  assert.throws(() => h.run('openBatchFollowup()'), /请先勾选候选人/);
  await h.run("navigate('detail')"); await h.load();
  assert.equal(h.node('#candidate-filter-error').hidden, false);
  assert.match(h.node('#candidate-filter-error').textContent, /最低分不能高于最高分/);
  assert.deepEqual(h.ids(), []);
  h.change('scoreMax', '100');
  assert.deepEqual(h.ids(), ['b']); assert.equal(h.node('#candidate-filter-error').hidden, true);
});

test('partially typed score signs or exponent letters are rejected instead of widening the filter', async () => {
  const h = harness([candidate('a', 0), candidate('b', 90), candidate('c', null)]);
  await h.load();
  for (const key of ['scoreMin', 'scoreMax']) {
    assert.equal(h.controls.get(key).attributes.type, 'text');
    assert.equal(h.controls.get(key).attributes.inputmode, 'numeric');
    assert.equal(h.controls.get(key).attributes.maxlength, '3');
    for (const badValue of ['-', 'e']) {
      await h.click('batch-select-all');
      h.change(key, badValue);
      assert.equal(h.node('#candidate-filter-error').hidden, false);
      assert.match(h.node('#candidate-filter-error').textContent, /0—100的整数/);
      assert.deepEqual(h.ids(), []); assert.deepEqual(h.selected(), []);
      assert.equal(h.node('[data-action=batch-open]').disabled, true);
      assert.throws(() => h.run('openBatchFollowup()'), /请先勾选候选人/);
      h.change(key, '');
      assert.equal(h.ids().length, 3);
      assert.equal(h.node('#candidate-filter-error').hidden, true);
    }
  }
});

test('navigation preserves filters while filtering, sorting and selection never mutate candidates or follow-up', async () => {
  const h = harness([candidate('a', 90), candidate('b', 60), candidate('c', null)]);
  await h.load();
  const before = h.run('JSON.stringify(state.applications)');
  h.change('scoreMin', '80'); h.change('sort', 'score_asc');
  await h.click('batch-select-all');
  await h.run("navigate('detail')");
  await h.load();
  assert.deepEqual(h.ids(), ['a']);
  assert.equal(h.controls.get('scoreMin').value, '80');
  assert.equal(h.controls.get('sort').value, 'score_asc');
  assert.equal(h.selected().length, 0);
  assert.equal(h.run('JSON.stringify(state.applications)'), before);
  assert.ok(h.calls.every(call => call.options === undefined), 'Filtering and ordering never write to API');
});
