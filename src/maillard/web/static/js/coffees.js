// ---------------------------------------------------------------------------
// Coffees — Coffee inventory CRUD UI
// ---------------------------------------------------------------------------
const Coffees = {
  data: [],

  async render(container) {
    container.innerHTML = '<div class="text-slate-400 text-sm">Loading...</div>';
    this.data = await apiGet('/api/coffees') || [];
    this._renderList(container);
  },

  _renderList(container) {
    let html = `
      <div class="flex justify-between items-center mb-4">
        <span class="text-sm text-slate-400">${this.data.length} coffees</span>
        <button onclick="Coffees.showForm()" class="text-xs bg-green-700 hover:bg-green-600 text-white px-3 py-1 rounded">
          + Add Coffee
        </button>
      </div>`;

    if (this.data.length === 0) {
      html += '<div class="text-slate-500 text-sm italic">No coffees registered yet.</div>';
    } else {
      html += '<div class="space-y-2">';
      this.data.forEach(c => {
        const detail = [c.origin, c.processing].filter(Boolean).join(' · ');
        const qty = c.inventory_known === false ? 'Green unknown (count needed)' : `${c.quantity_grams ?? 0}g green`;
        html += `
          <div class="bg-slate-800 rounded-lg p-3 flex justify-between items-center hover:bg-slate-750 cursor-pointer"
               onclick="Coffees.showForm('${c.id}')">
            <div>
              <div class="text-sm font-medium text-slate-200">${this._esc(c.name)}</div>
              <div class="text-xs text-slate-400">${this._esc(detail)}</div>
            </div>
            <div class="flex items-center gap-2">
              ${qty ? `<span class="text-xs text-slate-500">${qty}</span>` : ''}
              <button onclick="event.stopPropagation(); Coffees.deleteCoffee('${c.id}', '${this._esc(c.name)}')"
                      class="text-xs text-red-400 hover:text-red-300 px-1">Del</button>
            </div>
          </div>`;
      });
      html += '</div>';
    }

    container.innerHTML = html;
  },

  showForm(coffeeId = null) {
    const container = document.getElementById('drawer-body');
    const coffee = coffeeId ? this.data.find(c => c.id === coffeeId) : null;
    const isEdit = !!coffee;

    const varietalStr = coffee?.varietal ? coffee.varietal.join(', ') : '';

    container.innerHTML = `
      <div class="space-y-3">
        <div class="flex justify-between items-center mb-2">
          <h3 class="text-sm font-semibold text-slate-300">${isEdit ? 'Edit' : 'Add'} Coffee</h3>
          <button onclick="Coffees.render(document.getElementById('drawer-body'))" class="text-xs text-slate-400 hover:text-slate-300">Cancel</button>
        </div>
        <div>
          <label class="text-xs text-slate-400">Name *</label>
          <input id="cf-name" value="${this._esc(coffee?.name || '')}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
        </div>
        <div>
          <label class="text-xs text-slate-400">Origin *</label>
          <input id="cf-origin" value="${this._esc(coffee?.origin || '')}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
        </div>
        <div>
          <label class="text-xs text-slate-400">Processing</label>
          <select id="cf-processing" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
            <option value="">—</option>
            ${['Washed', 'Natural', 'Honey', 'Anaerobic'].map(v =>
              `<option value="${v}" ${coffee?.processing === v ? 'selected' : ''}>${v}</option>`
            ).join('')}
          </select>
        </div>
        <div>
          <label class="text-xs text-slate-400">Varietals (comma-separated)</label>
          <input id="cf-varietal" value="${this._esc(varietalStr)}" placeholder="Heirloom, SL28" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="text-xs text-slate-400">Altitude (MASL)</label>
            <input id="cf-altitude" type="number" min="0" max="3000" step="1" value="${coffee?.altitude_m ?? coffee?.altitude ?? ''}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
          </div>
          <div>
            <label class="text-xs text-slate-400">Density</label>
            <select id="cf-density" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
              <option value="">—</option>
              ${['Low', 'Medium', 'High'].map(v =>
                `<option value="${v}" ${coffee?.density === v ? 'selected' : ''}>${v}</option>`
              ).join('')}
            </select>
          </div>
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="text-xs text-slate-400">Moisture %</label>
            <input id="cf-moisture" type="number" min="6" max="16" step="0.1" value="${coffee?.moisture_pct ?? coffee?.moisture ?? ''}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
          </div>
          <div>
            <label class="text-xs text-slate-400">Green balance (g; receive lots in Inventory)</label>
            <input id="cf-quantity" type="number" disabled value="${coffee?.quantity_grams ?? 0}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
          </div>
        </div>
        <div>
          <label class="text-xs text-slate-400">Density (g/L)</label>
          <input id="cf-density-g-l" type="number" min="500" max="900" step="0.1" value="${coffee?.density_g_l ?? ''}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
        </div>
        <div>
          <label class="text-xs text-slate-400">Notes</label>
          <textarea id="cf-notes" rows="2" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">${this._esc(coffee?.notes || '')}</textarea>
        </div>
        <button onclick="Coffees.save('${coffeeId || ''}')"
                class="w-full bg-amber-600 hover:bg-amber-500 text-white text-sm font-medium py-2 rounded">
          ${isEdit ? 'Update' : 'Save'} Coffee
        </button>
      </div>`;
  },

  async save(coffeeId) {
    for (const id of ['cf-altitude', 'cf-moisture', 'cf-density-g-l']) {
      const input = document.getElementById(id);
      if (!input.reportValidity()) return;
    }
    const body = {
      name: document.getElementById('cf-name').value.trim(),
      origin: document.getElementById('cf-origin').value.trim(),
      processing: document.getElementById('cf-processing').value || null,
      density: document.getElementById('cf-density').value || null,
      notes: document.getElementById('cf-notes').value.trim() || null,
    };

    const varietalRaw = document.getElementById('cf-varietal').value.trim();
    body.varietal = varietalRaw ? varietalRaw.split(',').map(v => v.trim()).filter(Boolean) : null;

    const altitude = document.getElementById('cf-altitude').value;
    body.altitude_m = altitude ? Number(altitude) : null;

    const moisture = document.getElementById('cf-moisture').value;
    body.moisture_pct = moisture ? Number(moisture) : null;
    const density = document.getElementById('cf-density-g-l').value;
    body.density_g_l = density ? Number(density) : null;


    if (!body.name || !body.origin) {
      App.showAlert('Name and Origin are required', 'warning');
      return;
    }

    let result;
    if (coffeeId) {
      result = await apiPut(`/api/coffees/${coffeeId}`, body);
    } else {
      result = await apiPost('/api/coffees', body);
    }

    if (result && !result.status?.startsWith('error')) {
      App.logEvent(`Coffee ${coffeeId ? 'updated' : 'added'}: ${body.name}`, 'text-green-400');
      // Refresh roast dropdowns
      Roast.loadCoffees();
      Roast.loadProfiles();
      this.render(document.getElementById('drawer-body'));
    }
  },

  async deleteCoffee(id, name) {
    if (!confirm(`Delete "${name}"? This may cascade to linked profiles and roasts.`)) return;
    const result = await apiDelete(`/api/coffees/${id}?force=true`);
    if (result && result.status === 'deleted') {
      App.logEvent(`Coffee deleted: ${name}`, 'text-red-400');
      Roast.loadCoffees();
      Roast.loadProfiles();
      this.render(document.getElementById('drawer-body'));
    }
  },

  _esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); },
};
