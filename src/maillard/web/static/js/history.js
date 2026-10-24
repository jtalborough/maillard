// ---------------------------------------------------------------------------
// History — Roast history list + detail view with curve chart + cuppings
// ---------------------------------------------------------------------------
const History = {
  data: [],
  detailChart: null,
  labelStyles: [],

  async render(container) {
    container.innerHTML = '<div class="text-slate-400 text-sm">Loading...</div>';
    this.data = await apiGet('/api/roasts') || [];
    this._renderList(container);
  },

  _renderList(container) {
    let html = `
      <div class="flex justify-between items-center mb-4">
        <span class="text-sm text-slate-400">${this.data.length} roasts</span>
      </div>`;

    if (this.data.length === 0) {
      html += '<div class="text-slate-500 text-sm italic">No roasts recorded yet.</div>';
    } else {
      html += '<div class="space-y-2">';
      this.data.forEach(r => {
        const date = r.start_time ? new Date(r.start_time).toLocaleDateString() : '—';
        const time = r.total_time_seconds ? App.formatTime(r.total_time_seconds) : '—';
        const statusColor = r.status === 'completed' ? 'text-green-400' : ['aborted', 'abandoned'].includes(r.status) ? 'text-red-400' : 'text-yellow-400';
        html += `
          <div class="bg-slate-800 rounded-lg p-3 cursor-pointer hover:bg-slate-750"
               onclick="History.showDetail('${r.id}')">
            <div class="flex justify-between items-center">
              <div>
                <div class="text-sm font-medium text-slate-200">${this._esc(r.coffee_name || 'Unknown coffee')}</div>
                <div class="text-xs text-slate-400">
                  ${date} · ${this._esc(r.profile_name || 'No profile')} · ${time}
                </div>
              </div>
              <div class="text-right">
                <span class="text-xs ${statusColor} font-medium">${r.status}</span>
                ${r.excluded ? '<span class="text-xs text-slate-400 rounded bg-slate-700 px-1">Discarded</span>' : ''}
                ${r.drop_temp ? `<div class="text-xs text-slate-500">${r.drop_temp.toFixed(1)}°C</div>` : ''}
                ${r.dtr ? `<div class="text-xs text-slate-500">DTR ${r.dtr.toFixed(1)}%</div>` : ''}
                ${r.cupping_avg_total ? `<div class="text-xs text-amber-400">Cup ${r.cupping_avg_total.toFixed(1)}/60${r.cupping_count > 1 ? ` (${r.cupping_count})` : ''}</div>` : ''}
              </div>
            </div>
          </div>`;
      });
      html += '</div>';
    }

    container.innerHTML = html;
  },

  async showDetail(roastId) {
    const container = document.getElementById('drawer-body');
    container.innerHTML = '<div class="text-slate-400 text-sm">Loading...</div>';

    const r = await apiGet(`/api/roasts/${roastId}`);
    if (!r || r.status === 'error') {
      container.innerHTML = '<div class="text-red-400 text-sm">Failed to load roast.</div>';
      return;
    }

    const site = await apiGet(`/api/roasts/${roastId}/sync`);
    const siteStatus = !site?.configured ? 'not configured' : (site.sync_status || 'pending');
    const siteColors = { synced: 'text-green-400', pending: 'text-amber-400', failed: 'text-red-400' };
    let siteBadge = `<span class="rounded bg-slate-800 px-2 py-1 ${siteColors[siteStatus] || 'text-slate-400'}">${this._esc(siteStatus)}</span>`;
    if (siteStatus === 'synced' && /^https?:\/\//.test(site.url || '')) {
      siteBadge = `<a href="${this._esc(site.url)}" target="_blank" rel="noopener noreferrer">${siteBadge}</a>`;
    }
    const date = r.start_time ? new Date(r.start_time).toLocaleString() : '—';
    const statusColor = r.status === 'completed' ? 'text-green-400' : ['aborted', 'abandoned'].includes(r.status) ? 'text-red-400' : 'text-yellow-400';

    let html = `
      <div class="space-y-4">
        <div class="flex justify-between items-center">
          <button onclick="History.render(document.getElementById('drawer-body'))" class="text-xs text-slate-400 hover:text-slate-300">&larr; Back</button>
          <div class="flex items-center gap-2">
            <span class="text-xs ${statusColor} font-medium">${r.status}</span>
                ${r.excluded ? '<span class="text-xs text-slate-400 rounded bg-slate-700 px-1">Discarded</span>' : ''}
            <button onclick="History.setDiscarded('${r.id}', ${!r.excluded})" class="text-xs text-slate-400 hover:text-slate-300">${r.excluded ? 'Restore to AI history' : 'Discard from AI history'}</button>
            <button onclick="History.deleteRoast('${r.id}')" class="text-xs text-red-400 hover:text-red-300 hover:bg-red-900/30 px-1.5 py-0.5 rounded" title="Delete roast">Delete</button>
          </div>
        </div>

        <div>
          <h3 class="text-sm font-semibold text-slate-200">${this._esc(r.coffee_name || 'Unknown coffee')}</h3>
          ${r.roast_name ? `<div class="text-xs text-slate-500 italic">${this._esc(r.roast_name)}</div>` : ''}
          <div class="text-xs mt-0.5">
            ${r.code
              ? `<span class="font-mono font-bold tracking-widest text-amber-300" title="Roast code (QR / roastery.example.com/r/${this._esc(r.code)})">${this._esc(r.code)}</span>`
              : `<button onclick="History.ensureCode('${r.id}')" class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 px-1.5 py-0.5 rounded">Assign roast code</button>`}
            ${r.control_mode ? `<span class="ml-2 text-slate-500">${this._esc(r.control_mode)}</span>` : ''}
          </div>
          <div class="text-xs text-slate-400">${this._esc(r.profile_name || 'No profile')} · ${date}</div>
        </div>

        <!-- Metrics -->
        <div class="grid grid-cols-3 gap-2 text-center">
          ${this._metric('Charge', r.charge_temp, '°C')}
          ${this._metric('TP', r.turning_point_temp, '°C', r.turning_point_time)}
          ${this._metric('Dry End', r.dry_end_temp, '°C', r.dry_end_time)}
          ${this._metric('FC', r.fc_start_temp, '°C', r.fc_start_time)}
          ${this._metric('Drop', r.drop_temp, '°C', r.drop_time)}
          ${this._metric('DTR', r.dtr, '%')}
          ${this._metric('Total', null, '', r.total_time_seconds)}
          ${this._metric('Batch', r.batch_size_grams, 'g')}
          ${this._metric('W Loss', r.weight_loss, '%')}
        </div>

        <div class="flex items-center gap-2 text-xs"><span class="text-slate-400">SITE</span>
          ${siteBadge}
          ${siteStatus === 'failed' ? this._esc(site.outbox?.last_error || '') : ''}
          <button onclick="History.syncNow('${r.id}')" class="bg-slate-700 rounded px-2 py-1">Sync now</button>
        </div>

        <!-- Weights -->
        <div class="grid grid-cols-2 gap-2 items-end">
          <div>
            <label class="text-xs text-slate-400">Green (g)</label>
            <input id="roast-green-weight" type="number" min="0" max="5000" step="0.1"
              value="${r.green_weight_grams ?? r.batch_size_grams ?? ''}"
              class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm mt-0.5" />
          </div>
          <div>
            <label class="text-xs text-slate-400">Roasted (g)</label>
            <input id="roast-roasted-weight" oninput="History.suggestBags()" type="number" min="0" max="5000" step="0.1"
              value="${r.roasted_weight_grams ?? ''}"
              class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm mt-0.5" />
          </div>
          <div>
            <label class="text-xs text-slate-400">Bag size (g)</label>
            <input id="roast-bag-size" type="number" min="1" step="0.1" value="${r.bag_size_grams ?? 340}" oninput="History.suggestBags()" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm" />
          </div>
          <div>
            <label class="text-xs text-slate-400">Bags packed (editable)</label>
            <input id="roast-bags-packed" type="number" min="0" step="1" value="${r.bags_packed ?? (r.roasted_weight_grams != null ? Math.floor(r.roasted_weight_grams / (r.bag_size_grams || 340)) : '')}" oninput="this.dataset.edited='true'" data-edited="${r.bags_packed != null}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm" />
          </div>
          <div class="col-span-2">
            <label class="text-xs text-slate-400">Presale drop (optional; choose before saving bags)</label>
            <input id="roast-drop-id" value="${this._esc(r.drop_id || '')}" placeholder="Optional drop id; existing link cannot change" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm" />
          </div>
          <button onclick="History.saveWeights('${r.id}')" class="text-xs bg-amber-700 hover:bg-amber-600 text-white px-2 py-1.5 rounded">Save weights</button>
        </div>
        <div id="roast-weights-msg" class="text-xs text-slate-400 hidden"></div>

        <!-- Curve chart -->
        <div class="bg-slate-800 rounded-lg p-2" style="height:200px;">
          <canvas id="history-chart"></canvas>
        </div>

        <!-- Label -->
        <div class="border-t border-slate-700 pt-3">
          <div class="flex justify-between items-center mb-2">
            <div class="flex items-center gap-2">
              <h4 class="text-xs font-semibold text-slate-400">LABEL</h4>
              <select id="label-style" class="text-xs bg-slate-800 border border-slate-600 rounded px-1.5 py-0.5 text-slate-300">
                <option value="">Loading...</option>
              </select>
            </div>
            <div class="flex gap-1">
              <button onclick="History.previewLabel('${r.id}')" class="text-xs bg-blue-700 hover:bg-blue-600 text-white px-2 py-0.5 rounded">Preview</button>
              <button onclick="History.printAnyway('${r.id}')" class="text-xs underline">Print anyway</button>
              <button id="btn-print-label" class="text-xs bg-amber-700 hover:bg-amber-600 text-white px-2 py-0.5 rounded disabled:opacity-40 disabled:cursor-not-allowed" disabled title="Sync to site first">Print</button>
              <button onclick="History.generateLabel('${r.id}')" class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 px-2 py-0.5 rounded">Text</button>
            </div>
          </div>
          <label class="text-xs"><input type="checkbox" ${r.published ? 'checked' : ''} onchange="History.publishOnSite('${r.id}', this.checked)"> Publish on site</label>
          <div id="label-preview" class="hidden mb-2">
            <img id="label-preview-img" class="w-full rounded border border-slate-600" alt="Label preview" />
            <div id="label-qr-url" class="text-xs break-all"></div>
          </div>
          <div id="label-output" class="hidden">
            <textarea id="label-text" rows="6" readonly class="w-full bg-slate-800 border border-amber-700/50 rounded px-2 py-1.5 text-sm text-slate-200 font-mono"></textarea>
            <button onclick="History.copyLabel()" class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 px-2 py-1 rounded mt-1">Copy to Clipboard</button>
          </div>
        </div>

        <!-- Label Settings -->
        <div class="border-t border-slate-700 pt-3">
          <h4 class="text-xs font-semibold text-slate-400 mb-2">LABEL SETTINGS</h4>
          <div class="grid grid-cols-2 gap-2">
            <div>
              <label for="label-title-override" class="text-xs text-slate-400">Label Title</label>
              <input id="label-title-override" type="text" maxlength="120"
                value="${this._esc(r.coffee?.name || r.coffee_name || '')}"
                class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm mt-0.5" />
            </div>
            <div>
              <label for="label-origin-override" class="text-xs text-slate-400">Label Origin</label>
              <input id="label-origin-override" type="text" maxlength="120"
                value="${this._esc(r.coffee?.origin || '')}"
                class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm mt-0.5" />
            </div>
            <p class="col-span-2 text-xs text-slate-400">Applies to preview/print only; edit the coffee in Coffees to change it permanently.</p>
            <div>
              <label class="text-xs text-slate-400">Drop #</label>
              <input id="label-bond-number" type="number" min="1" placeholder="1"
                value="${r.bond_number || ''}"
                class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm mt-0.5" />
            </div>
            <div>
              <label class="text-xs text-slate-400">Weight Display</label>
              <input id="label-weight-display" type="text" placeholder="12oz / 340g"
                value="${this._esc(r.weight_display || '')}"
                class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm mt-0.5" />
            </div>
            <div>
              <label class="text-xs text-slate-400">Flavor Notes</label>
              <input id="label-flavor-notes" type="text" placeholder="citrus, floral, honey"
                value="${this._esc((r.flavor_notes || []).join(', '))}"
                class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm mt-0.5" />
            </div>
            <div>
              <label class="text-xs text-slate-400">Roast Name</label>
              <div class="flex gap-1 mt-0.5">
                <input id="label-roast-name" type="text" placeholder="bold-ember-drift"
                  value="${this._esc(r.roast_name || '')}"
                  class="flex-1 bg-slate-800 border border-slate-600 rounded px-2 py-1 text-sm" />
                <button onclick="History.regenerateName('${r.id}')" class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 px-1.5 py-1 rounded" title="Generate new name">↻</button>
              </div>
            </div>
          </div>
          <div class="flex items-center gap-2 mt-2">
            <button onclick="History.saveLabelSettings('${r.id}')" class="text-xs bg-amber-700 hover:bg-amber-600 text-white px-2 py-1 rounded">Save Label Settings</button>
            <span id="label-settings-msg" class="text-xs text-green-400 hidden">Saved!</span>
          </div>
        </div>

        <!-- Notes -->
        <div>
          <label class="text-xs text-slate-400">Notes</label>
          <textarea id="roast-notes" rows="2" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">${this._esc(r.notes || '')}</textarea>
          <button onclick="History.saveNotes('${r.id}')" class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 px-2 py-1 rounded mt-1">Save Notes</button>
        </div>

        <!-- Cuppings -->
        <div class="border-t border-slate-700 pt-3">
          <div class="flex justify-between items-center mb-2">
            <h4 class="text-xs font-semibold text-slate-400">CUPPING NOTES</h4>
            <button onclick="Cuppings.showForm('${r.id}')" class="text-xs bg-green-700 hover:bg-green-600 text-white px-2 py-0.5 rounded">+ Add Cupping</button>
          </div>
          <div id="cupping-list"></div>
        </div>
      </div>`;

    container.innerHTML = html;

    const printBtn = document.getElementById('btn-print-label');
    if (printBtn) {
      printBtn.addEventListener('click', () => this.printLabel(r.id));
    }

    // Draw curve chart
    if (r.curve_data) this._drawCurve(r.curve_data, r);

    // Disable print button if printer not configured
    this._updatePrintButton(r.id);

    // Load label styles into dropdown, then auto-preview
    this._loadLabelStyles(r.id);

    // Auto-preview when label style changes
    const styleSel = document.getElementById('label-style');
    if (styleSel) {
      styleSel.addEventListener('change', () => this.previewLabel(r.id));
    }

    clearTimeout(this._labelPreviewTimer);
    for (const id of ['label-title-override', 'label-origin-override']) {
      document.getElementById(id)?.addEventListener('input', () => {
        clearTimeout(this._labelPreviewTimer);
        this._labelPreviewTimer = setTimeout(() => this.previewLabel(r.id), 400);
      });
    }

    // Render cuppings
    Cuppings.renderList(r.cuppings || [], r.id);
  },

  _getLabelOverrides() {
    return {
      coffee_name: document.getElementById('label-title-override')?.value || '',
      origin: document.getElementById('label-origin-override')?.value || '',
    };
  },

  _getSelectedStyle() {
    const sel = document.getElementById('label-style');
    return sel ? sel.value : '';
  },

  _brandName() {
    const selected = (this.labelStyles || []).find(style => style.id === this._getSelectedStyle());
    return (selected && selected.brand_name) || 'Example Roastery';
  },

  async _loadLabelStyles(roastId) {
    const sel = document.getElementById('label-style');
    if (!sel) return;
    const styles = await apiGet('/api/label-styles');
    this.labelStyles = Array.isArray(styles) ? styles : [];
    if (!this.labelStyles.length) {
      sel.innerHTML = '<option value="">Classic</option>';
    } else {
      sel.innerHTML = this.labelStyles.map(s =>
        `<option value="${s.id}" title="${this._esc(s.description)}"${s.id === 'drop' ? ' selected' : ''}>${this._esc(s.name)}</option>`
      ).join('');
    }
    // Trigger initial preview after styles are loaded
    if (roastId) this.previewLabel(roastId);
  },

  async _updatePrintButton(roastId) {
    const btn = document.getElementById('btn-print-label');
    if (!btn) return;
    const readiness = await apiGet(`/api/labels/print-readiness/${roastId}`);
    btn.disabled = !readiness?.weights_ok || !readiness?.sync_ok || !readiness?.printer;
    btn.title = !readiness?.weights_ok ? 'Save roasted weight and bags packed first' : !readiness?.sync_ok ? 'Sync to site first' : !readiness?.printer ? 'Printer not configured' : 'Send to Zebra printer';
  },

  async previewLabel(roastId) {
    const previewContainer = document.getElementById('label-preview');
    const previewImg = document.getElementById('label-preview-img');
    if (!previewContainer || !previewImg) return;

    previewContainer.classList.remove('hidden');
    previewImg.alt = 'Loading preview...';

    const style = this._getSelectedStyle();
    const result = await apiPost('/api/labels/preview', { roast_id: roastId, style, overrides: this._getLabelOverrides() });
    if (result && result.status === 'ok' && result.image) {
      previewImg.src = result.image;
      previewImg.alt = 'Label preview';
      document.getElementById('label-qr-url').textContent = result.qr_url;
      await this._updatePrintButton(roastId);
    } else {
      previewImg.alt = 'Failed to generate preview';
      App.logEvent(result?.error || 'Preview failed', 'text-red-400');
    }
  },

  async publishOnSite(roastId, published) {
    const result = await apiPut(`/api/roasts/${roastId}`, { published });
    if (result?.error) {
      App.logEvent(result.error, 'text-red-400');
      return;
    }
    await this.syncNow(roastId);
  },

  async printAnyway(roastId) {
    if (confirm('Print even if the QR page is not live on the site?')) {
      await this.printLabel(roastId, true);
    }
  },

  async printLabel(roastId, override = false) {
    const btn = document.getElementById('btn-print-label');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Printing...';
    }

    const style = this._getSelectedStyle();
    try {
      const result = await apiPost('/api/labels/print', { roast_id: roastId, style, override, overrides: this._getLabelOverrides() });
      if (result && result.status === 'ok') {
        App.logEvent(result.message || 'Label sent to printer', 'text-green-400');
      } else {
        App.logEvent(result?.error || 'Print failed', 'text-red-400');
      }
    } finally {
      if (btn) {
        btn.textContent = 'Print';
        await this._updatePrintButton(roastId);
      }
    }
  },

  _drawCurve(curve, roast) {
    const ctx = document.getElementById('history-chart')?.getContext('2d');
    if (!ctx) return;

    if (this.detailChart) this.detailChart.destroy();

    const labels = (curve.time_seconds || []).map(t => App.formatTime(t));
    const ror = (curve.bt_ror || []).map(v => v ?? null);

    // Event annotations
    const annotations = {};
    (curve.events || []).forEach(ev => {
      const colors = {
        charge: '#22c55e', turning_point: '#a78bfa', dry_end: '#eab308',
        fc_start: '#f97316', fc_end: '#fb923c', sc_start: '#ef4444', drop: '#dc2626',
      };
      annotations[ev.type] = {
        type: 'line',
        xMin: App.formatTime(ev.time),
        xMax: App.formatTime(ev.time),
        borderColor: colors[ev.type] || '#94a3b8',
        borderWidth: 1,
        borderDash: [4, 2],
        label: {
          content: ev.type.replace(/_/g, ' '),
          display: true,
          position: 'start',
          color: '#94a3b8',
          font: { size: 8 },
        },
      };
    });

    this.detailChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'BT', data: curve.bt || [], borderColor: '#f87171',
            borderWidth: 1.5, pointRadius: 0, tension: 0.3, yAxisID: 'y',
          },
          {
            label: 'ET', data: curve.et || [], borderColor: '#60a5fa',
            borderWidth: 1.5, pointRadius: 0, tension: 0.3, yAxisID: 'y',
          },
          {
            label: 'RoR', data: ror, borderColor: '#4ade80', borderDash: [4, 2],
            borderWidth: 1, pointRadius: 0, tension: 0.3, yAxisID: 'y1',
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 0 },
        plugins: {
          legend: { position: 'top', labels: { color: '#94a3b8', font: { size: 9 } } },
          annotation: { annotations },
        },
        scales: {
          x: { ticks: { color: '#64748b', maxTicksLimit: 10, font: { size: 9 } }, grid: { color: '#1e293b' } },
          y: { type: 'linear', position: 'left', ticks: { color: '#64748b', font: { size: 9 } }, grid: { color: '#1e293b' }, min: 0, max: 280 },
          y1: { type: 'linear', position: 'right', ticks: { color: '#64748b', font: { size: 9 } }, grid: { drawOnChartArea: false }, min: -5, max: 30 },
        },
      },
    });
  },

  async generateLabel(roastId) {
    const r = await apiGet(`/api/roasts/${roastId}`);
    if (!r) return;

    const coffee = r.coffee || {};
    const date = r.start_time ? new Date(r.start_time).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }) : 'Unknown date';
    const roastLevel = r.roast_level || r.profile_name || 'Custom roast';

    const parts = [];
    parts.push(`${coffee.name || r.coffee_name || 'Single Origin'}`);
    if (coffee.origin) parts.push(`Origin: ${coffee.origin}`);
    if (coffee.processing) parts.push(`Process: ${coffee.processing}`);
    if (coffee.varietal && coffee.varietal.length) parts.push(`Varietal: ${coffee.varietal.join(', ')}`);
    if (coffee.altitude) parts.push(`Altitude: ${coffee.altitude} MASL`);
    parts.push(`Roast: ${roastLevel}`);
    parts.push(`Roasted: ${date}`);
    if (r.drop_temp) parts.push(`Drop temp: ${r.drop_temp.toFixed(1)}\u00b0C`);
    if (r.total_time_seconds) parts.push(`Total time: ${App.formatTime(r.total_time_seconds)}`);
    if (r.dtr) parts.push(`DTR: ${r.dtr.toFixed(1)}%`);
    if (r.batch_size_grams) parts.push(`Batch: ${r.batch_size_grams}g`);
    parts.push('');
    parts.push(this._brandName());
    parts.push('Roasted with Maillard');

    const label = parts.join('\n');
    const el = document.getElementById('label-text');
    const container = document.getElementById('label-output');
    if (el && container) {
      el.value = label;
      container.classList.remove('hidden');
    }
  },

  copyLabel() {
    const el = document.getElementById('label-text');
    if (!el) return;
    navigator.clipboard.writeText(el.value).then(() => {
      App.logEvent('Label copied to clipboard', 'text-green-400');
    }).catch(() => {
      el.select();
      document.execCommand('copy');
      App.logEvent('Label copied', 'text-green-400');
    });
  },

  async setDiscarded(roastId, excluded) {
    const result = await apiPatch(`/api/roasts/${roastId}`, { excluded });
    if (result && result.excluded === excluded) this.showDetail(roastId);
  },

  async deleteRoast(roastId) {
    if (!confirm('Delete this roast and all its cuppings? This cannot be undone.')) return;
    const result = await apiDelete(`/api/roasts/${roastId}`);
    if (result && result.status === 'deleted') {
      App.logEvent('Roast deleted', 'text-green-400');
      await this.render(document.getElementById('drawer-body'));
    }
  },

  async saveLabelSettings(roastId) {
    const bondVal = document.getElementById('label-bond-number').value;
    const weightVal = document.getElementById('label-weight-display').value.trim();
    const flavorVal = document.getElementById('label-flavor-notes').value.trim();
    const roastNameVal = document.getElementById('label-roast-name').value.trim();

    const payload = {
      bond_number: bondVal ? parseInt(bondVal, 10) : null,
      weight_display: weightVal || null,
      flavor_notes: flavorVal ? flavorVal.split(',').map(s => s.trim()).filter(Boolean) : null,
      roast_name: roastNameVal || null,
    };

    const result = await apiPut(`/api/roasts/${roastId}`, payload);
    const msg = document.getElementById('label-settings-msg');
    if (result && !result.status?.startsWith('error')) {
      if (msg) { msg.classList.remove('hidden'); setTimeout(() => msg.classList.add('hidden'), 2000); }
      // Refresh preview if visible
      const previewContainer = document.getElementById('label-preview');
      if (previewContainer && !previewContainer.classList.contains('hidden')) {
        this.previewLabel(roastId);
      }
    }
  },

  suggestBags() {
    const bags = document.getElementById('roast-bags-packed');
    const weight = Number(document.getElementById('roast-roasted-weight').value);
    const size = Number(document.getElementById('roast-bag-size').value);
    if (bags && bags.dataset.edited !== 'true' && size > 0) bags.value = Math.floor(weight / size);
  },

  async saveWeights(roastId) {
    const g = document.getElementById('roast-green-weight').value;
    const r = document.getElementById('roast-roasted-weight').value;
    const result = await apiPost(`/api/roasts/${roastId}/weights`, {
      green_weight_grams: g ? parseFloat(g) : null,
      roasted_weight_grams: r ? parseFloat(r) : null,
      bags_packed: document.getElementById('roast-bags-packed').value === '' ? null : Number(document.getElementById('roast-bags-packed').value),
      bag_size_grams: Number(document.getElementById('roast-bag-size').value),
      drop_id: document.getElementById('roast-drop-id').value.trim() || null,
    });
    const msg = document.getElementById('roast-weights-msg');
    if (!msg) return;
    msg.classList.remove('hidden');
    if (result && result.status === 'ok') {
      msg.className = 'text-xs text-green-400';
      msg.textContent = `Saved ${result.bags_packed ?? 0} bags. Weight loss ${result.weight_loss ?? '?'}%.`;
      await this._updatePrintButton(roastId);
    } else {
      msg.className = 'text-xs text-red-400';
      msg.textContent = (result && result.error) || 'Save failed';
    }
  },

  async ensureCode(roastId) {
    const result = await apiPost(`/api/roasts/${roastId}/ensure-code`);
    if (result && result.status === 'ok') this.showDetail(roastId);
  },

  async regenerateName(roastId) {
    const result = await apiPost(`/api/roasts/${roastId}/regenerate-name`);
    if (result && result.status === 'ok' && result.roast_name) {
      const input = document.getElementById('label-roast-name');
      if (input) input.value = result.roast_name;
    }
  },

  async syncNow(roastId) {
    const result = await apiPost(`/api/roasts/${roastId}/sync`, {});
    App.logEvent(result.error || `Site sync: ${result.sync_status}`, result.error ? 'text-red-400' : 'text-green-400');
    await this.showDetail(roastId);
  },

  async saveNotes(roastId) {
    const notes = document.getElementById('roast-notes').value.trim() || null;
    const result = await apiPut(`/api/roasts/${roastId}`, { notes });
    if (result && !result.status?.startsWith('error')) {
      App.logEvent('Roast notes saved', 'text-green-400');
    }
  },

  _metric(label, value, unit, timeSec) {
    let display = '—';
    if (timeSec != null && value != null) {
      display = `${value.toFixed?.(1) ?? value}${unit} @ ${App.formatTime(timeSec)}`;
    } else if (timeSec != null) {
      display = App.formatTime(timeSec);
    } else if (value != null) {
      display = `${value.toFixed?.(1) ?? value}${unit}`;
    }
    return `<div class="bg-slate-800 rounded p-1.5">
      <div class="text-xs text-slate-500">${label}</div>
      <div class="text-xs text-slate-200 font-medium">${display}</div>
    </div>`;
  },

  _esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); },
};
