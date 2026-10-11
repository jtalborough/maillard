// ---------------------------------------------------------------------------
// Profiles — Roast profile CRUD UI with phase editor
// ---------------------------------------------------------------------------
const Profiles = {
  data: [],
  editPhases: [],
  _aiRationale: null,

  async render(container) {
    container.innerHTML = '<div class="text-slate-400 text-sm">Loading...</div>';
    this.data = await apiGet('/api/profiles') || [];
    this._aiRationale = null;
    this._renderList(container);
  },

  _renderList(container) {
    let html = `
      <div class="flex justify-between items-center mb-4">
        <span class="text-sm text-slate-400">${this.data.length} profiles</span>
        <button onclick="Profiles.showForm()" class="text-xs bg-green-700 hover:bg-green-600 text-white px-3 py-1 rounded">
          + Add Profile
        </button>
      </div>`;

    if (this.data.length === 0) {
      html += '<div class="text-slate-500 text-sm italic">No profiles created yet.</div>';
    } else {
      html += '<div class="space-y-2">';
      this.data.forEach(p => {
        const detail = [p.roast_level, p.brew_method, p.charge_temp ? `${p.charge_temp}°C` : null].filter(Boolean).join(' · ');
        const coffeeName = App.coffeeList.find(c => c.id === p.coffee_id)?.name;
        html += `
          <div class="bg-slate-800 rounded-lg p-3 flex justify-between items-center hover:bg-slate-750 cursor-pointer"
               onclick="Profiles.showForm('${p.id}')">
            <div>
              <div class="text-sm font-medium text-slate-200">${this._esc(p.name)}</div>
              <div class="text-xs text-slate-400">${this._esc(detail)}${coffeeName ? ` — ${this._esc(coffeeName)}` : ''}</div>
            </div>
            <button onclick="event.stopPropagation(); Profiles.deleteProfile('${p.id}', '${this._esc(p.name)}')"
                    class="text-xs text-red-400 hover:text-red-300 px-1">Del</button>
          </div>`;
      });
      html += '</div>';
    }

    container.innerHTML = html;
  },

  async showForm(profileId = null) {
    const container = document.getElementById('drawer-body');
    let profile = null;
    if (profileId) {
      profile = await apiGet(`/api/profiles/${profileId}`);
    }
    const isEdit = !!profile;

    this.editPhases = profile?.phases ? [...profile.phases] : this._defaultPhases();
    this._aiRationale = profile?.ai_rationale || null;
    this._planSource = profile?.plan_source || null;
    this._aiModel = profile?.ai_model || null;
    this._warnings = profile?.warnings || [];

    const coffeeOptions = App.coffeeList.map(c =>
      `<option value="${c.id}" ${profile?.coffee_id === c.id ? 'selected' : ''}>${this._esc(c.name)}</option>`
    ).join('');

    container.innerHTML = `
      <div class="space-y-3">
        <div class="flex justify-between items-center mb-2">
          <h3 class="text-sm font-semibold text-slate-300">${isEdit ? 'Edit' : 'Add'} Profile</h3>
          <button onclick="Profiles.render(document.getElementById('drawer-body'))" class="text-xs text-slate-400 hover:text-slate-300">Cancel</button>
        </div>
        <div>
          <label class="text-xs text-slate-400">Name *</label>
          <input id="pf-name" value="${this._esc(profile?.name || '')}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
        </div>
        <div>
          <label class="text-xs text-slate-400">Coffee</label>
          <select id="pf-coffee" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
            <option value="">— None —</option>
            ${coffeeOptions}
          </select>
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="text-xs text-slate-400">Roast Level</label>
            <select id="pf-level" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
              <option value="">—</option>
              ${['Light', 'Medium-Light', 'Medium', 'Medium-Dark', 'Dark'].map(v =>
                `<option value="${v}" ${profile?.roast_level === v ? 'selected' : ''}>${v}</option>`
              ).join('')}
            </select>
          </div>
          <div>
            <label class="text-xs text-slate-400">Brew Method</label>
            <select id="pf-brew" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
              <option value="">—</option>
              ${['Filter', 'Espresso', 'Both'].map(v =>
                `<option value="${v}" ${profile?.brew_method === v ? 'selected' : ''}>${v}</option>`
              ).join('')}
            </select>
          </div>
        </div>
        <div class="grid grid-cols-3 gap-2">
          <div>
            <label class="text-xs text-slate-400">Charge Temp</label>
            <input id="pf-charge" type="number" value="${profile?.charge_temp || ''}" placeholder="°C" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
          </div>
          <div>
            <label class="text-xs text-slate-400">Batch Size (g)</label>
            <input id="pf-batch" type="number" value="${profile?.batch_size_grams || ''}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
          </div>
          <div>
            <label class="text-xs text-slate-400">Target DTR %</label>
            <input id="pf-dtr" type="number" step="0.1" value="${profile?.target_dtr || ''}" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
          </div>
        </div>
        <div>
          <label class="text-xs text-slate-400">Flavor Goal</label>
          <input id="pf-flavor" value="${this._esc(profile?.flavor_goal || '')}" placeholder="e.g., highlight floral and citrus" class="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm mt-0.5">
        </div>

        <button id="pf-ai-btn" onclick="Profiles.generateWithAI()"
                class="w-full bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium py-1.5 rounded flex items-center justify-center gap-1">
          Generate with AI
        </button>
        <div id="pf-warnings" role="alert" class="hidden border border-amber-500 bg-amber-950 text-amber-200 rounded p-3 text-sm whitespace-pre-line"></div>
        <div id="pf-ai-rationale" class="${this._aiRationale ? '' : 'hidden'}">
          <details class="text-xs">
            <summary class="text-indigo-400 cursor-pointer">AI Rationale</summary>
            <p id="pf-ai-rationale-text" class="text-slate-400 mt-1 whitespace-pre-wrap">${this._esc(this._aiRationale || '')}</p>
          </details>
        </div>

        <div class="border-t border-slate-700 pt-3">
          <div class="flex justify-between items-center mb-2">
            <h4 class="text-xs font-semibold text-slate-400">PHASES</h4>
            <button onclick="Profiles.addPhase()" class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 px-2 py-0.5 rounded">+ Phase</button>
          </div>
          <div id="pf-phases" class="space-y-2"></div>
        </div>

        <button onclick="Profiles.save('${profileId || ''}')"
                class="w-full bg-amber-600 hover:bg-amber-500 text-white text-sm font-medium py-2 rounded">
          ${isEdit ? 'Update' : 'Save'} Profile
        </button>
      </div>`;

    this._renderPhases();
    this._showWarnings();
  },

  _showWarnings() {
    const box = document.getElementById('pf-warnings');
    box.textContent = ['Review plan warnings before charge:', ...this._warnings].join('\n');
    box.classList.toggle('hidden', !this._warnings.length);
  },

  async generateWithAI() {
    const coffeeId = document.getElementById('pf-coffee').value;
    if (!coffeeId) {
      App.showAlert('Select a coffee first to generate with AI', 'warning');
      return;
    }

    const btn = document.getElementById('pf-ai-btn');
    btn.disabled = true;
    btn.textContent = 'Generating...';
    btn.classList.add('opacity-50');

    try {
      const result = await apiPost('/api/ai/generate-profile', {
        coffee_id: coffeeId,
        roast_level: document.getElementById('pf-level').value || 'Medium',
        flavor_goal: document.getElementById('pf-flavor').value.trim() || '',
        brew_method: document.getElementById('pf-brew').value || 'Filter',
        batch_size_grams: parseInt(document.getElementById('pf-batch').value) || 1200,
      });

      if (result?.status === 'ok' && result.profile) {
        const p = result.profile;

        // Populate form fields
        document.getElementById('pf-name').value = p.name || '';
        document.getElementById('pf-charge').value = p.charge_temp || '';
        document.getElementById('pf-dtr').value = p.target_dtr || '';

        // Update phases
        this.editPhases = p.phases;
        this._renderPhases();
        this._planSource = p.source || null;
        this._aiModel = p.ai_model || null;
        if (p.batch_size_grams) document.getElementById('pf-batch').value = p.batch_size_grams;
        this._warnings = p.warnings || [];
        this._showWarnings();

        // Show rationale
        this._aiRationale = p.ai_rationale || null;
        const rationaleDiv = document.getElementById('pf-ai-rationale');
        const rationaleText = document.getElementById('pf-ai-rationale-text');
        if (this._aiRationale) {
          rationaleText.textContent = this._aiRationale;
          rationaleDiv.classList.remove('hidden');
        }

        const sourceLabel = p.source === 'ai' ? 'AI-generated' : 'Heuristic';
        App.logEvent(`Profile generated (${sourceLabel}): ${p.name}`, 'text-indigo-400');
      } else {
        App.showAlert(result?.error || 'Failed to generate profile', 'error');
      }
    } catch (e) {
      App.showAlert('Failed to generate profile', 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Generate with AI';
      btn.classList.remove('opacity-50');
    }
  },

  _renderPhases() {
    const container = document.getElementById('pf-phases');
    const phaseNames = ['charge', 'turning_point', 'dry_end', 'maillard', 'fc_start', 'development', 'drop'];

    container.innerHTML = this.editPhases.map((ph, i) => `
      <div class="bg-slate-800 rounded p-2 text-xs space-y-1">
        <div class="flex justify-between items-center">
          <select onchange="Profiles.editPhases[${i}].name=this.value" class="bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs">
            ${phaseNames.map(n => `<option value="${n}" ${ph.name === n ? 'selected' : ''}>${n.replace(/_/g, ' ')}</option>`).join('')}
          </select>
          <button onclick="Profiles.removePhase(${i})" class="text-red-400 hover:text-red-300 px-1">x</button>
        </div>
        <div class="grid grid-cols-4 gap-1">
          <div>
            <label class="text-slate-500">Time</label>
            <input type="text" value="${this._fmtTime(ph.target_time_seconds)}" placeholder="mm:ss"
                   onchange="Profiles.editPhases[${i}].target_time_seconds=Profiles._parseTime(this.value)"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs text-center">
          </div>
          <div>
            <label class="text-slate-500">Temp °C</label>
            <input type="number" value="${ph.target_temp}" onchange="Profiles.editPhases[${i}].target_temp=+this.value"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs text-center">
          </div>
          <div>
            <label class="text-slate-500">Heat %</label>
            <input type="number" value="${ph.heat_percent}" min="0" max="100" onchange="Profiles.editPhases[${i}].heat_percent=+this.value"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs text-center">
          </div>
          <div>
            <label class="text-slate-500">Fan %</label>
            <input type="number" value="${ph.fan_percent}" min="0" max="100" onchange="Profiles.editPhases[${i}].fan_percent=+this.value"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs text-center">
          </div>
        </div>
        <div class="grid grid-cols-3 gap-1">
          <div>
            <label class="text-slate-500">Drum %</label>
            <input type="number" value="${ph.drum_percent}" min="0" max="100" onchange="Profiles.editPhases[${i}].drum_percent=+this.value"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs text-center">
          </div>
          <div>
            <label class="text-slate-500">RoR</label>
            <input type="number" step="0.1" value="${ph.target_ror || ''}" onchange="Profiles.editPhases[${i}].target_ror=this.value?+this.value:null"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs text-center">
          </div>
          <div>
            <label class="text-slate-500">Notes</label>
            <input type="text" value="${this._esc(ph.notes || '')}" onchange="Profiles.editPhases[${i}].notes=this.value||null"
                   class="w-full bg-slate-900 border border-slate-600 rounded px-1 py-0.5 text-xs">
          </div>
        </div>
      </div>
    `).join('');
  },

  addPhase() {
    this.editPhases.push({
      name: 'maillard', target_time_seconds: 0, target_temp: 170,
      heat_percent: 60, fan_percent: 50, drum_percent: 60,
      target_ror: null, notes: null,
    });
    this._renderPhases();
  },

  removePhase(idx) {
    this.editPhases.splice(idx, 1);
    this._renderPhases();
  },

  async save(profileId) {
    const body = {
      name: document.getElementById('pf-name').value.trim(),
      coffee_id: document.getElementById('pf-coffee').value || null,
      roast_level: document.getElementById('pf-level').value || null,
      brew_method: document.getElementById('pf-brew').value || null,
      flavor_goal: document.getElementById('pf-flavor').value.trim() || null,
      phases: this.editPhases,
      warnings: this._warnings || [],
    };

    const charge = document.getElementById('pf-charge').value;
    body.charge_temp = charge ? parseFloat(charge) : null;
    const batch = document.getElementById('pf-batch').value;
    body.batch_size_grams = batch ? parseInt(batch) : null;
    const dtr = document.getElementById('pf-dtr').value;
    body.target_dtr = dtr ? parseFloat(dtr) : null;

    if (this._aiRationale) {
      body.ai_rationale = this._aiRationale;
    }
    if (this._planSource) {
      body.plan_source = this._planSource;
      body.ai_model = this._aiModel;
    }

    if (!body.name) { App.showAlert('Name is required', 'warning'); return; }
    if (body.phases.length === 0) { App.showAlert('At least one phase is required', 'warning'); return; }

    let result;
    if (profileId) {
      result = await apiPut(`/api/profiles/${profileId}`, body);
    } else {
      result = await apiPost('/api/profiles', body);
    }

    if (result && !result.status?.startsWith('error')) {
      App.logEvent(`Profile ${profileId ? 'updated' : 'added'}: ${body.name}`, 'text-green-400');
      Roast.loadProfiles();
      this.render(document.getElementById('drawer-body'));
    }
  },

  async deleteProfile(id, name) {
    if (!confirm(`Delete profile "${name}"?`)) return;
    const result = await apiDelete(`/api/profiles/${id}`);
    if (result && result.status === 'deleted') {
      App.logEvent(`Profile deleted: ${name}`, 'text-red-400');
      Roast.loadProfiles();
      this.render(document.getElementById('drawer-body'));
    }
  },

  _defaultPhases() {
    // Kaleido M10 1200 g, from ai/calibration.py defaults (fit of the author's 300-800 g
    // roasts; 1200 g is a LOW-CONFIDENCE extrapolation). Prefer "Generate" for a
    // batch-specific calibrated plan.
    const lowConf = 'LOW CONFIDENCE: extrapolated beyond 800 g max observed';
    return [
      { name: 'charge', target_time_seconds: 0, target_temp: 209, heat_percent: 70, fan_percent: 30, drum_percent: 60, target_ror: null, notes: lowConf },
      { name: 'turning_point', target_time_seconds: 40, target_temp: 88, heat_percent: 70, fan_percent: 30, drum_percent: 60, target_ror: null, notes: null },
      { name: 'dry_end', target_time_seconds: 511, target_temp: 155, heat_percent: 60, fan_percent: 35, drum_percent: 60, target_ror: 11.0, notes: null },
      { name: 'maillard', target_time_seconds: 667, target_temp: 168.5, heat_percent: 50, fan_percent: 40, drum_percent: 60, target_ror: 9.0, notes: null },
      { name: 'fc_start', target_time_seconds: 824, target_temp: 182, heat_percent: 40, fan_percent: 50, drum_percent: 60, target_ror: 7.0, notes: null },
      { name: 'development', target_time_seconds: 940, target_temp: 195.8, heat_percent: 30, fan_percent: 60, drum_percent: 60, target_ror: 4.0, notes: null },
      { name: 'drop', target_time_seconds: 1056, target_temp: 205, heat_percent: 0, fan_percent: 100, drum_percent: 60, target_ror: null, notes: null },
    ];
  },

  _fmtTime(secs) {
    if (secs == null) return '0:00';
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  },

  _parseTime(str) {
    const parts = str.split(':');
    if (parts.length === 2) return parseInt(parts[0]) * 60 + parseInt(parts[1]);
    return parseInt(str) || 0;
  },

  _esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); },
};
