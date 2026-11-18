const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function load(filename, name, ids, globals = {}) {
  const nodes = Object.fromEntries(ids.map(id => [id, {
    dataset: {}, value: '', textContent: '', disabled: false, focus() {},
    removeAttribute(key) { delete this[key]; },
    classList: { remove() {} },
  }]));
  const document = { getElementById: id => nodes[id] || null };
  const source = fs.readFileSync(`src/maillard/web/static/js/${filename}.js`, 'utf8');
  const module = vm.runInNewContext(`${source}\n${name}`, { document, ...globals });
  return [module, nodes];
}

test('print readiness and override require valid weight, saved bags, and a printer', async () => {
  const requests = [];
  const ready = { weights_ok: true, printer: 'tcp', sync_ok: false };
  const [history, nodes] = load('history', 'History', [
    'btn-print-label', 'btn-print-anyway', 'btn-reprint-label', 'btn-preview-last',
    'label-print-readiness', 'label-print-result', 'label-bag-progress',
    'label-net-weight', 'label-drop-override', 'label-preview', 'label-preview-img',
    'label-qr-url', 'label-preview-error',
  ], {
    apiGet: async () => ready,
    apiPost: async (url, body) => {
      requests.push([url, body]);
      if (url.endsWith('/preview')) return { status: 'ok', image: 'sample' };
      if (requests.length === 2) return { status: 'error', error: 'Printer submission failed' };
      return { status: 'ok', message: 'Label data sent over TCP', bag_weights_grams: [365] };
    },
    confirm: () => true,
  });
  history._bagWeights = [];
  history._bagsPacked = 1;
  await history._updatePrintButton('roast');
  assert.equal(nodes['btn-print-label'].disabled, true);
  assert.equal(nodes['btn-print-anyway'].disabled, true);
  assert.match(nodes['label-print-readiness'].textContent, /Enter bag net weight/);

  nodes['label-net-weight'].value = '365.9';
  nodes['label-drop-override'].value = 'XC014';
  ready.printer = null;
  history._applyPrintEnabled();
  assert.match(nodes['label-print-readiness'].textContent, /Printer not configured/);
  assert.equal(nodes['btn-print-anyway'].disabled, true);
  ready.printer = 'tcp';
  ready.weights_ok = false;
  history._applyPrintEnabled();
  assert.equal(nodes['btn-print-anyway'].disabled, true);
  ready.weights_ok = true;
  ready.sync_ok = true;
  history._applyPrintEnabled();
  assert.equal(nodes['btn-print-label'].disabled, false);
  assert.equal(nodes['btn-print-anyway'].disabled, true);
  ready.sync_ok = false;
  history._applyPrintEnabled();
  assert.equal(nodes['btn-print-label'].disabled, true);
  assert.equal(nodes['btn-print-anyway'].disabled, false);

  await history.previewLabel('roast');
  await history.printAnyway('roast');
  assert.equal(requests[0][1].overrides.drop_label, 'XC014');
  assert.deepEqual(requests[1][1].overrides, requests[0][1].overrides);
  assert.equal(requests[1][1].net_weight_grams, 365);
  assert.equal(requests[1][1].override, true);
  assert.equal(nodes['label-print-result'].textContent, 'Printer submission failed');
  assert.equal(nodes['label-net-weight'].value, '365.9');
  assert.equal(nodes['btn-print-anyway'].disabled, false);

  await history.printAnyway('roast');
  assert.equal(nodes['label-print-result'].textContent, 'Label data sent over TCP');
  assert.equal(nodes['btn-print-anyway'].disabled, true);
  assert.equal(nodes['btn-reprint-label'].disabled, false);
  await history.previewLabel('roast', true);
  assert.equal(requests.at(-1)[1].reprint, true);
  assert.equal('net_weight_grams' in requests.at(-1)[1], false);
});

test('preheat points rebase to charge and telemetry leaves saved duration frozen', () => {
  const [roast, nodes] = load('roast', 'Roast', ['val-time', 'val-bt', 'val-et'], {
    App: { formatTime: value => String(Math.floor(value)) },
  });
  roast.chart = {
    update() {},
    options: { plugins: { annotation: { annotations: {} } }, scales: { x: { title: {} } } },
  };
  const sample = (time) => roast.handleWSMessage({
    type: 'StateChangeEvent', timestamp: time, state: { BT: 110, ET: 120 },
  });
  sample('2026-01-01T00:00:00Z');
  sample('2026-01-01T00:00:08Z');
  assert.equal(roast.elapsed, 0);
  assert.deepEqual(Array.from(roast.chartData.labels), [0, 8]);
  const charge = { roast_id: 'roast', charged_at: '2026-01-01T00:00:10Z', elapsed_seconds: 0 };
  roast.syncCharge(charge);
  sample('2026-01-01T00:00:12Z');
  roast.syncCharge({ ...charge, elapsed_seconds: 1 });
  assert.equal(nodes['val-time'].textContent, '2');
  assert.deepEqual(Array.from(roast.chartData.labels), [-10, -2, 2]);
  const marks = Object.values(roast.chart.options.plugins.annotation.annotations);
  assert.equal(marks.length, 1);
  assert.equal(marks[0].xMin, 0);
  roast.finishChart(2);
  sample('2026-01-01T00:00:22Z');
  assert.equal(nodes['val-time'].textContent, '2');
  assert.deepEqual(Array.from(roast.chartData.labels), [-10, -2, 2]);
});
