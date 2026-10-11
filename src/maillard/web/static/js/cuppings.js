// ---------------------------------------------------------------------------
// Cuppings — Cupping notes CRUD (rendered inside roast detail view)
// ---------------------------------------------------------------------------
const Cuppings = {

  renderList(cuppings, roastId) {
    const container = document.getElementById('cupping-list');
    if (!container) return;

    if (!cuppings || cuppings.length === 0) {
      container.innerHTML = '<div class="text-xs text-slate-500 italic">No cupping notes yet.</div>';
      return;
    }

    container.innerHTML = cuppings.map(c => {
      const scores = ['aroma', 'acidity', 'sweetness', 'body', 'aftertaste', 'overall'];
      const total = scores.reduce((sum, k) => sum + (c[k] || 0), 0);
      const scoreParts = scores.map(k => c[k] ? `${k[0].toUpperCase()}:${c[k]}` : null).filter(Boolean).join(' ');
      const flavors = c.flavor_notes ? c.flavor_notes.join(', ') : '';
      const defects = c.defects ? c.defects.join(', ') : '';
      const date = c.cupped_at ? new Date(c.cupped_at).toLocaleDateString() : '';

      return `
        <div class="bg-slate-800 rounded p-2 mb-2 text-xs">
          <div class="flex justify-between items-center mb-1">
            <span class="text-slate-400">${date}${c.rest_days != null ? ` · ${c.rest_days}d rest` : ''}${c.brew_method ? ` · ${this._esc(c.brew_method)}` : ''}</span>
            <div class="flex gap-1">
              <button onclick="Cuppings.showForm('${roastId}', '${c.id}')" class="text-slate-400 hover:text-slate-300">Edit</button>
              <button onclick="Cuppings.deleteCupping('${c.id}', '${roastId}')" class="text-red-400 hover:text-red-300">Del</button>
            </div>
          </div>
          <div class="text-slate-300 font-medium mb-1">Score: ${total.toFixed(1)}/60 <span class="text-slate-500 font-normal">(${scoreParts})</span></div>
          ${flavors ? `<div class="text-green-400">${this._esc(flavors)}</div>` : ''}
          ${defects ? `<div class="text-red-400">Defects: ${this._esc(defects)}</div>` : ''}
          ${c.next_time_notes ? `<div class="text-slate-400 mt-1">Next time try: ${this._esc(c.next_time_notes)}</div>` : ''}
          ${c.notes ? `<div class="text-slate-400 mt-1">${this._esc(c.notes)}</div>` : ''}
        </div>`;
    }).join('');
  },

  async showForm(roastId, cuppingId = null) {
    let cupping = null;
    if (cuppingId) {
      cupping = await apiGet(`/api/cuppings/${cuppingId}`);
    }
    const isEdit = !!cupping;
    const flavorsStr = cupping?.flavor_notes ? cupping.flavor_notes.join(', ') : '';
    const defectsStr = cupping?.defects ? cupping.defects.join(', ') : '';

    // Insert form at the cupping-list position
    const container = document.getElementById('cupping-list');
    container.innerHTML = `
      <div class="bg-slate-800 rounded p-3 space-y-2">
        <div class="flex justify-between items-center">
          <h4 class="text-xs font-semibold text-slate-300">${isEdit ? 'Edit' : 'Add'} Cupping</h4>
          <button onclick="History.showDetail('${roastId}')" class="text-xs text-slate-400 hover:text-slate-300">Cancel</button>
        </div>

        <div class="text-xs text-slate-400 mb-1">Scores (1-10)</div>
        <div class="grid grid-cols-3 gap-2">
          ${this._scoreInput('cup-aroma', 'Aroma', cupping?.aroma)}
          ${this._scoreInput('cup-acidity', 'Acidity', cupping?.acidity)}
          ${this._scoreInput('cup-sweetness', 'Sweet', cupping?.sweetness)}
          ${this._scoreInput('cup-body', 'Body', cupping?.body)}
          ${this._scoreInput('cup-aftertaste', 'After', cupping?.aftertaste)}
          ${this._scoreInput('cup-overall', 'Overall', cupping?.overall)}
        </div>
        <div id="cup-total" class="text-xs text-slate-300 font-medium">Total: —/60</div>

        <div>
          <label class="text-xs text-slate-400">Flavor Notes (comma-separated)</label>
          <input id="cup-flavors" value="${this._esc(flavorsStr)}" placeholder="citrus, floral, berry"
                 class="w-full bg-slate-900 border border-slate-600 rounded px-2 py-1 text-xs mt-0.5">
        </div>
        <div>
          <label class="text-xs text-slate-400">Defects (comma-separated)</label>
          <input id="cup-defects" value="${this._esc(defectsStr)}" placeholder="baked, grassy"
                 class="w-full bg-slate-900 border border-slate-600 rounded px-2 py-1 text-xs mt-0.5">
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="text-xs text-slate-400">Rest Days</label>
            <input id="cup-rest" type="number" min="0" value="${cupping?.rest_days ?? ''}"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-2 py-1 text-xs mt-0.5">
          </div>
          <div>
            <label class="text-xs text-slate-400">Brew Method</label>
            <input id="cup-brew" value="${this._esc(cupping?.brew_method || '')}" placeholder="V60, Cupping"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-2 py-1 text-xs mt-0.5">
          </div>
        </div>
        <div>
          <label class="text-xs text-slate-400">Notes</label>
          <textarea id="cup-notes" rows="2" class="w-full bg-slate-900 border border-slate-600 rounded px-2 py-1 text-xs mt-0.5">${this._esc(cupping?.notes || '')}</textarea>
        </div>
        <div>
          <label class="text-xs text-slate-400">Next time try</label>
          <textarea id="cup-next-time" rows="2" class="w-full bg-slate-900 border border-slate-600 rounded px-2 py-1 text-xs mt-0.5">${this._esc(cupping?.next_time_notes || '')}</textarea>
        </div>
        <button onclick="Cuppings.save('${roastId}', '${cuppingId || ''}')"
                class="w-full bg-amber-600 hover:bg-amber-500 text-white text-xs font-medium py-1.5 rounded">
          ${isEdit ? 'Update' : 'Save'} Cupping
        </button>
      </div>`;

    // Auto-sum scores
    this._setupScoreTotal();
  },

  _scoreInput(id, label, value) {
    return `<div>
      <label class="text-slate-500 text-xs">${label}</label>
      <input id="${id}" type="number" min="1" max="10" step="0.5" value="${value || ''}"
             onchange="Cuppings._updateTotal()"
             class="w-full bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs text-center mt-0.5">
    </div>`;
  },

  _setupScoreTotal() {
    this._updateTotal();
  },

  _updateTotal() {
    const ids = ['cup-aroma', 'cup-acidity', 'cup-sweetness', 'cup-body', 'cup-aftertaste', 'cup-overall'];
    const total = ids.reduce((sum, id) => {
      const v = parseFloat(document.getElementById(id)?.value);
      return sum + (isNaN(v) ? 0 : v);
    }, 0);
    const el = document.getElementById('cup-total');
    if (el) el.textContent = `Total: ${total.toFixed(1)}/60`;
  },

  async save(roastId, cuppingId) {
    const body = { roast_id: roastId };

    const scoreIds = {
      aroma: 'cup-aroma', acidity: 'cup-acidity', sweetness: 'cup-sweetness',
      body: 'cup-body', aftertaste: 'cup-aftertaste', overall: 'cup-overall',
    };
    for (const [key, id] of Object.entries(scoreIds)) {
      const v = parseFloat(document.getElementById(id).value);
      if (!isNaN(v)) body[key] = v;
    }

    const flavors = document.getElementById('cup-flavors').value.trim();
    body.flavor_notes = flavors ? flavors.split(',').map(s => s.trim()).filter(Boolean) : null;

    const defects = document.getElementById('cup-defects').value.trim();
    body.defects = defects ? defects.split(',').map(s => s.trim()).filter(Boolean) : null;

    const rest = document.getElementById('cup-rest').value;
    body.rest_days = rest ? parseInt(rest) : null;
    body.brew_method = document.getElementById('cup-brew').value.trim() || null;
    body.next_time_notes = document.getElementById('cup-next-time').value.trim() || null;
    body.notes = document.getElementById('cup-notes').value.trim() || null;

    let result;
    if (cuppingId) {
      result = await apiPut(`/api/cuppings/${cuppingId}`, body);
    } else {
      result = await apiPost('/api/cuppings', body);
    }

    if (result && !result.status?.startsWith('error')) {
      App.logEvent(`Cupping ${cuppingId ? 'updated' : 'added'}`, 'text-green-400');
      History.showDetail(roastId);
    }
  },

  async deleteCupping(cuppingId, roastId) {
    if (!confirm('Delete this cupping?')) return;
    const result = await apiDelete(`/api/cuppings/${cuppingId}`);
    if (result && result.status === 'deleted') {
      App.logEvent('Cupping deleted', 'text-red-400');
      History.showDetail(roastId);
    }
  },

  _esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); },
};
