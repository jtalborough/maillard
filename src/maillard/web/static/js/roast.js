// ---------------------------------------------------------------------------
// Roast — chart, controls, events, AI panel, polling
// ---------------------------------------------------------------------------
const Roast = {
  chart: null,
  chartData: { bt: [], et: [], ror: [], plan: [], labels: [] },
  activePlan: null,       // RoastPlan locked at charge (planned BT curve)
  chargeOffset: null,     // chart elapsed seconds at CHARGE (aligns plan with stream)
  eventMarkers: [],
  pollTimer: null,
  sliderActive: false,
  sliderDebounce: null,
  coolingOn: false,
  chargeTimestamp: null,  // Date.now() at charge — for WS-based chart
  streamStart: null,      // Date.now() at first WS data — for chart timeline
  lastBT: null,           // previous BT for simple RoR calc
  lastBTTime: null,        // timestamp of previous BT
  rorBuffer: [],             // rolling buffer for RoR smoothing
  _recDebounce: null,      // debounce timer for recommendation requests
  _lastRecommendation: null, // last recommendation data
  _aiTranscript: [],
  _lastAISuggestion: null,

  // -- Chart ----------------------------------------------------------------
  initChart() {
    const ctx = document.getElementById('roast-chart').getContext('2d');
    this.chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: this.chartData.labels,
        datasets: [
          {
            label: 'BT (°C)', data: this.chartData.bt,
            borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.1)',
            borderWidth: 2, pointRadius: 0, tension: 0.3, yAxisID: 'y',
          },
          {
            label: 'ET (°C)', data: this.chartData.et,
            borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.1)',
            borderWidth: 2, pointRadius: 0, tension: 0.3, yAxisID: 'y',
          },
          {
            label: 'Plan BT (°C)', data: this.chartData.plan,
            borderColor: '#fbbf24', borderDash: [6, 4],
            borderWidth: 1.5, pointRadius: 0, tension: 0, yAxisID: 'y', spanGaps: true,
          },
          {
            label: 'RoR (°C/min)', data: this.chartData.ror,
            borderColor: '#4ade80', borderDash: [5, 3],
            borderWidth: 1.5, pointRadius: 0, tension: 0.3, yAxisID: 'y1',
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 0 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { position: 'top', labels: { color: '#94a3b8', font: { size: 11 } } },
          annotation: { annotations: {} },
        },
        scales: {
          x: {
            title: { display: true, text: 'Time', color: '#64748b' },
            ticks: { color: '#64748b', maxTicksLimit: 20 },
            grid: { color: '#1e293b' },
          },
          y: {
            type: 'linear', position: 'left',
            title: { display: true, text: '°C', color: '#64748b' },
            ticks: { color: '#64748b' }, grid: { color: '#1e293b' },
            min: 0, max: 280,
          },
          y1: {
            type: 'linear', position: 'right',
            title: { display: true, text: 'RoR °C/min', color: '#64748b' },
            ticks: { color: '#64748b' }, grid: { drawOnChartArea: false },
            min: -5, max: 30,
          },
        },
      },
    });
  },

  _resetChartData() {
    this.chartData = { bt: [], et: [], ror: [], plan: [], labels: [] };
    this.chargeOffset = null;
    this.eventMarkers = [];
    this.streamStart = null;
    this.lastBT = null;
    this.lastBTTime = null;
    this.rorBuffer = [];
    if (this.chart) {
      this.chart.data.labels = this.chartData.labels;
      this.chart.data.datasets[0].data = this.chartData.bt;
      this.chart.data.datasets[1].data = this.chartData.et;
      this.chart.data.datasets[2].data = this.chartData.plan;
      this.chart.data.datasets[3].data = this.chartData.ror;
      this.chart.options.plugins.annotation.annotations = {};
      this.chart.update('none');
    }
  },

  addChartPoint(timeSec, bt, et, ror) {
    const label = App.formatTime(timeSec);
    this.chartData.labels.push(label);
    this.chartData.bt.push(bt);
    this.chartData.et.push(et);
    this.chartData.ror.push(ror);
    const planned = this._plannedAt(timeSec);
    this.chartData.plan.push(planned);
    this._updatePlanDelta(bt, planned, timeSec);
    if (this.chartData.labels.length > 1200) {
      this.chartData.labels.shift();
      this.chartData.bt.shift();
      this.chartData.et.shift();
      this.chartData.ror.shift();
      this.chartData.plan.shift();
    }
    this.chart.update('none');
  },

  // -- Planned curve overlay ----------------------------------------------
  // Planned BT (linear between phase targets) at chart time `timeSec`.
  _plannedAt(timeSec) {
    const plan = this.activePlan;
    if (!plan || this.chargeOffset == null) return null;
    const curve = plan.planned_curve || {};
    const ts = curve.time_seconds || [];
    const bt = curve.bt || [];
    const t = timeSec - this.chargeOffset;
    if (!ts.length || t < ts[0] || t > ts[ts.length - 1]) return null;
    for (let i = 1; i < ts.length; i++) {
      if (t <= ts[i]) {
        const f = (t - ts[i - 1]) / ((ts[i] - ts[i - 1]) || 1);
        return Math.round((bt[i - 1] + (bt[i] - bt[i - 1]) * f) * 10) / 10;
      }
    }
    return bt[bt.length - 1];
  },

  _updatePlanDelta(bt, planned, timeSec) {
    const el = document.getElementById('val-plan-delta');
    if (!el) return;
    if (planned == null || bt == null) { el.textContent = '—'; el.className = 'text-lg font-bold text-slate-400'; return; }
    const d = Math.round((bt - planned) * 10) / 10;
    el.textContent = (d > 0 ? '+' : '') + d.toFixed(1);
    el.className = 'text-lg font-bold ' + (Math.abs(d) <= 3 ? 'text-green-400' : Math.abs(d) <= 8 ? 'text-amber-400' : 'text-red-400');
    const plan = this.activePlan;
    const t = timeSec - this.chargeOffset;
    const next = (plan.phases || []).find(p => p.target_time_seconds > t);
    const nextEl = document.getElementById('val-plan-next');
    if (nextEl) {
      nextEl.textContent = next
        ? `${next.name.replace(/_/g, ' ')} ${next.target_temp}°C in ${App.formatTime(Math.max(0, next.target_time_seconds - t))}`
        : '';
    }
  },

  setActivePlan(plan, chargeOffset) {
    this.activePlan = plan || null;
    this._showPlanWarnings(plan);
    if (chargeOffset != null) this.chargeOffset = chargeOffset;
    const label = document.getElementById('plan-summary');
    if (label) label.textContent = plan ? this._planSummary(plan) : 'No plan (unplanned roast)';
    if (plan) {
      // Plan target markers: first crack + drop
      if (plan.target_fc_seconds != null && this.chargeOffset != null) {
        this._addAnnotation(this.chargeOffset + plan.target_fc_seconds, 'PLAN FC', '#b45309');
      }
      if (plan.target_total_seconds != null && this.chargeOffset != null) {
        this._addAnnotation(this.chargeOffset + plan.target_total_seconds, 'PLAN DROP', '#b45309');
      }
    }
  },

  _planSummary(plan) {
    const bits = [
      plan.batch_size_grams ? `${plan.batch_size_grams} g` : null,
      plan.charge_temp != null ? `charge ${Math.round(plan.charge_temp)}°C` : null,
      plan.target_fc_seconds != null ? `FC ${App.formatTime(plan.target_fc_seconds)}` : null,
      plan.target_total_seconds != null ? `drop ${App.formatTime(plan.target_total_seconds)}` : null,
      plan.target_drop_temp != null ? `@ ${Math.round(plan.target_drop_temp)}°C` : null,
      plan.target_dtr != null ? `DTR ${plan.target_dtr}%` : null,
    ].filter(Boolean).join(' · ');
    const src = plan.source === 'ai' ? 'AI plan' : plan.source === 'heuristic' ? 'Heuristic plan' : 'Plan';
    const warn = (plan.warnings || []).length ? ` ⚠ ${plan.warnings.join('; ')}` : '';
    return `${src}: ${bits}${warn}`;
  },

  _showPlanWarnings(plan) {
    const box = document.getElementById('plan-warnings');
    if (!box) return;
    const warnings = plan?.warnings || [];
    box.textContent = ['Review plan warnings before charge:', ...warnings].join('\n');
    box.classList.toggle('hidden', !warnings.length);
  },

  async previewPlan() {
    const profileId = document.getElementById('profile-select').value;
    const label = document.getElementById('plan-summary');
    if (!label || App.roastActive) return;
    if (!profileId) { this._showPlanWarnings(null); label.textContent = 'No plan selected (roast will be logged without a plan)'; return; }
    const batch = parseInt(document.getElementById('batch-weight').value) || '';
    const data = await apiGet(`/api/profiles/${profileId}/planned-curve?batch_size_grams=${batch}`);
    if (profileId !== document.getElementById('profile-select').value || App.roastActive) return;
    if (data && data.status === 'ok') {
      label.textContent = this._planSummary(data.plan);
      this._showPlanWarnings(data.plan);
    }
  },

  async restoreActivePlan(elapsedSeconds) {
    const data = await apiGet('/api/roast/plan');
    if (data && data.plan && this.streamStart != null && elapsedSeconds != null) {
      const offset = (Date.now() - this.streamStart) / 1000 - elapsedSeconds;
      this.setActivePlan(data.plan, Math.max(0, offset));
    }
  },

  addEventLine(timeSec, eventName) {
    this.eventMarkers.push({ time: timeSec, name: eventName });
  },

  _addAnnotation(timeSec, label, color) {
    if (!this.chart) return;
    var id = 'evt-' + label + '-' + Math.round(timeSec);
    this.chart.options.plugins.annotation.annotations[id] = {
      type: 'line',
      xMin: App.formatTime(timeSec),
      xMax: App.formatTime(timeSec),
      borderColor: color || '#94a3b8',
      borderWidth: 1.5,
      borderDash: [4, 4],
      label: {
        display: true,
        content: label,
        position: 'start',
        backgroundColor: color || '#94a3b8',
        color: '#fff',
        font: { size: 10, weight: 'bold' },
        padding: 2,
      },
    };
    this.chart.update('none');
  },

  // -- Gauges ---------------------------------------------------------------
  updateGauges(data) {
    if (data.bt != null) document.getElementById('val-bt').textContent = data.bt.toFixed(1);
    if (data.et != null) document.getElementById('val-et').textContent = data.et.toFixed(1);
    if (data.ror_instant != null) document.getElementById('val-ror').textContent = data.ror_instant.toFixed(1);
    else if (data.ror_smoothed != null) document.getElementById('val-ror').textContent = data.ror_smoothed.toFixed(1);
    if (data.elapsed_seconds != null) document.getElementById('val-time').textContent = App.formatTime(data.elapsed_seconds);
    if (data.current_phase) document.getElementById('val-phase').textContent = data.current_phase;
    if (data.heat_percent != null) document.getElementById('val-heat').textContent = data.heat_percent;
  },

  updateFromStatus(data) {
    const r = data.roaster || {};
    const roast = data.roast || {};
    if (data.monitoring) {
      App.showAlert("CONNECTION LOST / BT DATA STALE - use Kaleido controls",
        "critical", "connection_lost", !data.monitoring.connection_lost);
    }
    if (data.heating) {
      App.showAlert("Heating paused after disconnect - send a new heat command to resume",
        "critical", "heating_disconnected", data.heating.state !== "paused_disconnected");
    }
    if (data.cooling) {
      this.syncCoolingUi(data.cooling);
    }
    if (data.ai && data.ai.mode) {
      const prev = App.aiMode;
      App.aiMode = data.ai.mode;
      App.aiAutoEnabled = data.ai.auto_control_enabled === true;
      this.updateAIModeButtons();
      if (prev === 'auto' && App.aiMode !== 'auto') {
        App.logEvent('AI auto stopped — manual control', 'text-amber-400');
        document.getElementById('ai-panel')?.classList.toggle('ai-active', App.aiMode !== 'off');
      }
    }

    const rDot = document.getElementById('roaster-dot');
    const rLabel = document.getElementById('roaster-label');
    const connectBtn = document.getElementById('btn-connect');
    if (r.connected) {
      rDot.className = 'connection-dot dot-connected';
      rLabel.textContent = 'Roaster: on';
      connectBtn.textContent = 'Disconnect';
      connectBtn.onclick = () => this.doDisconnect();
      connectBtn.className = 'text-xs bg-slate-600 hover:bg-slate-500 text-white px-2 py-0.5 rounded';
    } else {
      rDot.className = 'connection-dot dot-disconnected';
      rLabel.textContent = 'Roaster: off';
      connectBtn.textContent = 'Connect';
      connectBtn.onclick = () => this.doConnect();
      connectBtn.className = 'text-xs bg-green-700 hover:bg-green-600 text-white px-2 py-0.5 rounded';
    }

    if (data.monitoring?.link_state === 'degraded') {
      rDot.style.backgroundColor = '#eab308';
      rLabel.textContent = 'Roaster: reconnecting…';
    } else {
      rDot.style.backgroundColor = '';
    }

    if ((data.monitoring?.data_age_s ?? 0) < 5 && r.bt != null) document.getElementById('val-bt').textContent = r.bt.toFixed(1);
    if ((data.monitoring?.data_age_s ?? 0) < 5 && r.et != null) document.getElementById('val-et').textContent = r.et.toFixed(1);
    if (r.heat_percent != null) {
      document.getElementById('val-heat').textContent = r.heat_percent;
      document.getElementById('ctrl-heat').value = r.heat_percent;
      document.getElementById('heat-val').textContent = r.heat_percent + '%';
    }
    if (r.fan_percent != null) {
      document.getElementById('ctrl-fan').value = r.fan_percent;
      document.getElementById('fan-val').textContent = r.fan_percent + '%';
    }
    if (r.drum_percent != null) {
      document.getElementById('ctrl-drum').value = r.drum_percent;
      document.getElementById('drum-val').textContent = r.drum_percent + '%';
    }

    App.roastActive = roast.active || false;
    const roastState = roast.state || '';
    // Single-screen roasting layout while a roast is running
    const wasRoasting = document.body.classList.contains('roasting');
    const nowRoasting = roastState === 'roasting';
    document.body.classList.toggle('roasting', nowRoasting);
    if (wasRoasting !== nowRoasting && this.chart) setTimeout(() => this.chart.resize(), 50);
    const dd = document.getElementById('detected-drop');
    if (dd) {
      if (nowRoasting && roast.detected_drop) {
        document.getElementById('detected-drop-time').textContent = App.formatTime(roast.detected_drop.time_seconds);
        document.getElementById('detected-drop-temp').textContent = Number(roast.detected_drop.temp).toFixed(1);
        if (dd.classList.contains('hidden')) App.speak('Drop detected. Finish the roast when ready.', true);
        dd.classList.remove('hidden');
      } else {
        dd.classList.add('hidden');
      }
    }
    const isPreheating = roastState === 'preheating' || roastState === 'ready_to_charge';
    if (roast.elapsed_seconds != null) document.getElementById('val-time').textContent = App.formatTime(roast.elapsed_seconds);
    if (roast.phase) document.getElementById('val-phase').textContent = roast.phase;
    if (roast.latest_ror != null) document.getElementById('val-ror').textContent = roast.latest_ror.toFixed(1);

    // Resume chart polling if we detect an active roast (e.g. after page refresh)
    if (roast.active && roastState === 'roasting' && !this.pollTimer) {
      if (roast.elapsed_seconds != null) {
        this.chargeTimestamp = Date.now() - (roast.elapsed_seconds * 1000);
        if (this.streamStart == null) {
          this.streamStart = this.chargeTimestamp;
        }
      }
      this.startPolling();
      if (!this.activePlan) this.restoreActivePlan(roast.elapsed_seconds);
    }

    this.updateButtons(isPreheating);
  },

  updateButtons(isPreheating = false) {
    document.getElementById('btn-charge').disabled = App.roastActive && !isPreheating;
    document.getElementById('btn-tp').disabled = !App.roastActive || isPreheating;
    document.getElementById('btn-dry').disabled = !App.roastActive || isPreheating;
    document.getElementById('btn-fc').disabled = !App.roastActive || isPreheating;
    document.getElementById('btn-fce').disabled = !App.roastActive || isPreheating;
    document.getElementById('btn-drop').disabled = !App.roastActive || isPreheating;
  },

  // -- WebSocket message handler --------------------------------------------
  handleWSMessage(msg) {
    switch (msg.type) {
      case 'ping': break;

      case 'StateChangeEvent': {
        if (msg.stale) break;
        const s = msg.state || {};
        if (s.BT != null) document.getElementById('val-bt').textContent = s.BT.toFixed(1);
        if (s.ET != null) document.getElementById('val-et').textContent = s.ET.toFixed(1);
        if (s.HP != null) {
          document.getElementById('val-heat').textContent = s.HP;
          if (!this.sliderActive) {
            document.getElementById('ctrl-heat').value = s.HP;
            document.getElementById('heat-val').textContent = s.HP + '%';
          }
        }
        if (s.FC != null && !this.sliderActive) {
          document.getElementById('ctrl-fan').value = s.FC;
          document.getElementById('fan-val').textContent = s.FC + '%';
        }
        if (s.RC != null && !this.sliderActive) {
          document.getElementById('ctrl-drum').value = s.RC;
          document.getElementById('drum-val').textContent = s.RC + '%';
        }
        // Start chart timeline on first WS data with valid BT
        if (s.BT > 0 && this.streamStart == null) {
          this.streamStart = Date.now();
        }
        // Feed chart from WS during active stream
        if (this.streamStart && s.BT != null && s.ET != null) {
          const elapsed = (Date.now() - this.streamStart) / 1000;
          let ror = null;
          const now = Date.now();
          if (this.lastBT != null && this.lastBTTime != null) {
            const dt = (now - this.lastBTTime) / 1000;
            if (dt > 0.5) {
              ror = ((s.BT - this.lastBT) / dt) * 60; // °C/min
              ror = Math.round(ror * 10) / 10;
            }
          }
          this.lastBT = s.BT;
          this.lastBTTime = now;
          // Smooth RoR with rolling average (~15s window at 2Hz)
          if (ror != null) {
            this.rorBuffer.push(ror);
            if (this.rorBuffer.length > 30) this.rorBuffer.shift();
            // Don't plot RoR until buffer has enough samples
            if (this.rorBuffer.length >= 10) {
              ror = Math.round(
                (this.rorBuffer.reduce((a, b) => a + b, 0) / this.rorBuffer.length) * 10
              ) / 10;
            } else {
              ror = null;
            }
          }
          this.addChartPoint(elapsed, s.BT, s.ET, ror);
          document.getElementById('val-time').textContent = App.formatTime(elapsed);
          if (ror != null) document.getElementById('val-ror').textContent = ror.toFixed(1);
        }
        break;
      }

      case 'RoastEventMarker':
        App.logEvent(`${msg.event}: ${msg.temp_bt?.toFixed(1) || '?'}°C @ ${App.formatTime(msg.time_seconds)}`, 'text-amber-400');
        this.addEventLine(msg.time_seconds, msg.event);
        if (msg.event === 'CHARGE' || msg.event === 1) {
          App.roastActive = true;
          this.chargeTimestamp = Date.now();
          // Add CHARGE vertical marker instead of resetting chart
          if (this.streamStart) {
            const chargeElapsed = (Date.now() - this.streamStart) / 1000;
            this._addAnnotation(chargeElapsed, 'CHARGE', '#22c55e');
            if (this.chargeOffset == null) this.chargeOffset = chargeElapsed;
          }
          this.updateButtons();
          this.startPolling();
        }
        if (msg.event === 'DROP' || msg.event === 9) {
          App.roastActive = false;
          this.chargeTimestamp = null;
          this.streamStart = null;
          this.updateButtons();
        }
        break;

      case 'SafetyAlertEvent':
        App.logEvent(`SAFETY: ${msg.message}`, 'text-red-400');
        App.showAlert(msg.message, msg.level, msg.condition, msg.cleared);
        if (!msg.cleared) App.speak(`Safety warning. ${msg.message}`, true);
        break;

      case 'ConnectionEvent':
        App.logEvent(`Connection: ${msg.state}`, 'text-blue-400');
        break;

      case 'TurningPointSuggested':
        App.logEvent(`Turning point suggested @ ${msg.temp_bt?.toFixed(1)}°C`, 'text-purple-400');
        App.showAlert('Turning point detected — confirm when ready', 'warning');
        App.speak('Turning point detected. Confirm when ready.');
        break;

      case 'DryEndSuggested':
        App.logEvent(`Dry end approaching @ ${msg.temp_bt?.toFixed(1)}°C`, 'text-yellow-400');
        break;

      case 'FirstCrackSuggested':
        App.logEvent(`First crack approaching @ ${msg.temp_bt?.toFixed(1)}°C`, 'text-orange-400');
        App.showAlert('Approaching first crack — listen for cracks!', 'warning');
        App.speak('Approaching first crack. Listen for cracks.');
        break;

      case 'DropSuggested':
        App.logEvent(`Drop suggested @ ${msg.temp_bt?.toFixed(1)}°C — ${msg.reason}`, 'text-red-400');
        App.showAlert('Profile target reached — confirm drop!', 'critical');
        App.speak('Drop now! Target reached.', true);
        break;

      case 'AISuggestionEvent':
        this.handleAISuggestion(msg);
        break;

      case 'AITranscriptEvent':
        this.handleAITranscriptEvent(msg);
        break;

      default:
        App.logEvent(`${msg.type}`, 'text-slate-500');
    }
  },

  // -- REST API calls -------------------------------------------------------
  async fetchStatus() {
    const data = await apiGet('/api/status');
    if (data) this.updateFromStatus(data);
  },

  async doConnect() {
    const statusData = await apiGet('/api/status');
    const dtype = statusData?.driver_type || 'kaleido';
    App.logEvent(`Connecting to ${dtype}...`, 'text-blue-400');
    const data = await apiPost('/api/connect', { driver_type: dtype });
    if (data && (data.status === 'connected' || data.status === 'already_connected')) {
      App.logEvent(`${dtype} connected`, 'text-green-400');
    }
    this.fetchStatus();
  },

  async doDisconnect() {
    App.logEvent('Disconnecting...', 'text-blue-400');
    await apiPost('/api/disconnect');
    this.fetchStatus();
  },

  async doEmergencyStop() {
    App.logEvent('EMERGENCY STOP triggered!', 'text-red-500');
    App.showAlert('EMERGENCY STOP', 'emergency');
    const result = await apiPost('/api/emergency-stop');
    App.showAlert(result?.message || 'Emergency stop unconfirmed - use Kaleido controls',
      result?.confirmed ? 'warning' : 'critical', result?.confirmed ? null : 'emergency_stop_failed');
    App.roastActive = false;
    this.chargeTimestamp = null;
    this.streamStart = null;
    this.stopPolling();
    this.updateButtons();
    this.showNewRoastButton();
    this.fetchStatus();
  },

  async doPreheat() {
    const temp = parseFloat(document.getElementById('preheat-temp').value) || 200;
    App.logEvent(`Preheating to ${temp}°C...`, 'text-amber-400');
    await apiPost('/api/preheat', {
      target_temp: temp,
      heat_percent: parseInt(document.getElementById('ctrl-heat').value) || 100,
      fan_percent: parseInt(document.getElementById('ctrl-fan').value) || 30,
      drum_percent: parseInt(document.getElementById('ctrl-drum').value) || 50,
    });
    this.fetchStatus();
  },

  // -- Coffee & Profile Dropdowns -------------------------------------------
  async loadCoffees() {
    const coffees = await apiGet('/api/coffees');
    if (!coffees) return;
    App.coffeeList = coffees;
    const sel = document.getElementById('coffee-select');
    sel.innerHTML = '<option value="">Select coffee...</option>';
    coffees.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      const detail = [c.origin, c.processing].filter(Boolean).join(' · ');
      opt.textContent = detail ? `${c.name} (${detail})` : c.name;
      sel.appendChild(opt);
    });
  },

  async loadProfiles() {
    const profiles = await apiGet('/api/profiles');
    if (!profiles) return;
    App.profileList = profiles;
    this.updateProfileDropdown();
  },

  updateProfileDropdown(coffeeId = null) {
    const sel = document.getElementById('profile-select');
    sel.innerHTML = '<option value="">Select profile...</option>';
    const filtered = coffeeId
      ? App.profileList.filter(p => p.coffee_id === coffeeId || !p.coffee_id)
      : App.profileList;
    filtered.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      const detail = [p.roast_level, p.charge_temp ? `${p.charge_temp}°C` : null].filter(Boolean).join(' · ');
      opt.textContent = detail ? `${p.name} (${detail})` : p.name;
      sel.appendChild(opt);
    });
  },

  onCoffeeChange() {
    const coffeeId = document.getElementById('coffee-select').value || null;
    this.updateProfileDropdown(coffeeId);
    if (coffeeId) {
      const match = App.profileList.find(p => p.coffee_id === coffeeId);
      if (match) {
        document.getElementById('profile-select').value = match.id;
        this.onProfileChange();
      }
    } else {
      this.hideRecommendation();
    }
  },

  onProfileChange() {
    const profileId = document.getElementById('profile-select').value;
    if (!profileId) return;
    const profile = App.profileList.find(p => p.id === profileId);
    if (!profile) return;
    if (profile.charge_temp) document.getElementById('preheat-temp').value = profile.charge_temp;
    if (profile.batch_size_grams) document.getElementById('batch-weight').value = profile.batch_size_grams;
    this.previewPlan();
    if (profile.roast_level) {
      const rlSel = document.getElementById('roast-level');
      for (const opt of rlSel.options) {
        if (opt.value.toLowerCase() === profile.roast_level.toLowerCase()) {
          rlSel.value = opt.value;
          break;
        }
      }
    }
  },

  async doCharge() {
    App.logEvent('CHARGE...', 'text-green-400');
    const coffeeId = document.getElementById('coffee-select').value || null;
    const profileId = document.getElementById('profile-select').value || null;
    const batchWeight = parseInt(document.getElementById('batch-weight').value) || null;
    const roastLevel = document.getElementById('roast-level').value || null;
    const body = { coffee_id: coffeeId, profile_id: profileId, batch_size_grams: batchWeight, drop_id: App.nextDropId || null };
    const coffee = App.coffeeList.find(c => c.id === coffeeId);
    const beanName = coffee ? coffee.name : '';
    if (beanName || roastLevel) {
      apiPost('/api/ai/context', { bean_name: beanName, roast_level: roastLevel, batch_grams: batchWeight });
    }
    const data = await apiPost('/api/charge', body);
    if (data && data.status === 'charged') {
      Inventory.clearDrop();
      App.roastActive = true;
      this.chargeTimestamp = Date.now();
      // Add CHARGE vertical marker instead of resetting chart
      if (this.streamStart) {
        const chargeElapsed = (Date.now() - this.streamStart) / 1000;
        this._addAnnotation(chargeElapsed, 'CHARGE', '#22c55e');
        this.chargeOffset = chargeElapsed;
      } else {
        this.streamStart = Date.now();
        this.chargeOffset = 0;
      }
      this.setActivePlan(data.plan || null);
      if (data.code) App.logEvent(`Roast code ${data.code}`, 'text-amber-300');
      this.updateButtons();
      this.hideNewRoastButton();
      this.startPolling();
      if (beanName) App.speak(`Charging ${batchWeight || ''} grams of ${beanName}`);
      else App.speak('Beans charged');
    }
    this.fetchStatus();
  },

  async doEvent(name) {
    App.logEvent(`Marking ${name}...`, 'text-amber-400');
    const data = await apiPost(`/api/event/${name}`);
    if (data && data.status !== 'error') {
      App.logEvent(`${name.replace(/_/g, ' ')} marked`, 'text-green-400');
      App.speak(`${name.replace(/_/g, ' ')} marked`);
    }
    this.fetchStatus();
  },

  syncCoolingUi(cooling) {
    this.coolingOn = !!cooling.enabled;
    const btn = document.getElementById('btn-cooling');
    if (!btn) return;
    btn.textContent = this.coolingOn ? 'Cooling Fan: ON' : 'Cooling Fan: OFF';
    btn.className = this.coolingOn
      ? 'w-full bg-cyan-500 hover:bg-cyan-400 text-white text-sm font-bold py-2 rounded-lg mb-2'
      : 'w-full bg-cyan-700 hover:bg-cyan-600 text-white text-sm font-bold py-2 rounded-lg mb-2';
  },

  async toggleCooling() {
    const want = !this.coolingOn;
    const data = await apiPost('/api/cooling', { enabled: want });
    if (data && data.status === 'ok') {
      this.syncCoolingUi(data);
      App.logEvent((want ? 'Cooling fan ON' : 'Cooling fan OFF') + (data.confirmed === false ? ' (sent, roaster did not confirm)' : ''), 'text-cyan-400');
      if (want) App.speak('Cooling fan on.');
    }
  },

  async dismissDetectedDrop() {
    await apiPost('/api/drop/detected/dismiss');
    document.getElementById('detected-drop').classList.add('hidden');
    App.logEvent('Detected drop dismissed', 'text-slate-400');
  },

  async doDrop() {
    App.logEvent('DROP — cooling fan on, heat off', 'text-red-400');
    App.speak('Drop! Cooling fans on.', true);
    const data = await apiPost('/api/drop');
    if (data && data.status === 'dropped') {
      App.roastActive = false;
      this.chargeTimestamp = null;
      this.streamStart = null;
      this.updateButtons();
      this.stopPolling();
      if (data.cooling) this.syncCoolingUi(data.cooling);
      else this.syncCoolingUi({ enabled: true });
      const coolNote = data.cooling && data.cooling.confirmed === false
        ? ' (CS sent, roaster did not confirm — check tray fans)'
        : '';
      App.logEvent('Cooling fan ON' + coolNote, 'text-cyan-400');
      App.logEvent(`Roast complete! Drop ${data.drop_temp?.toFixed(1) || '?'}°C, DTR: ${data.dtr?.toFixed(1) || '?'}%`, 'text-green-400');
      App.speak(`Roast complete. ${data.drop_temp?.toFixed(0) || ''} degrees. D T R ${data.dtr?.toFixed(0) || ''} percent.`);
      App.logEvent('Weigh roasted coffee and record bags packed in History → Weights', 'text-slate-400');
      this.showNewRoastButton();
    }
    this.fetchStatus();
  },

  // -- Polling --------------------------------------------------------------
  startPolling() {
    this.stopPolling();
    this.pollTimer = setInterval(async () => {
      const data = await apiGet('/api/roast');
      if (data && data.status === 'active') {
        this.updateGauges(data);
        // Use server-computed RoR for gauge (more accurate than WS estimate)
        if (data.ror_smoothed != null) {
          document.getElementById('val-ror').textContent = data.ror_smoothed.toFixed(1);
        }
      } else if (data && data.status === 'no_active_roast' && App.roastActive) {
        App.roastActive = false;
        this.chargeTimestamp = null;
        this.streamStart = null;
        this.updateButtons();
        this.stopPolling();
      }
    }, 3000);
  },

  stopPolling() {
    if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
  },

  // -- New Roast flow -------------------------------------------------------
  showNewRoastButton() {
    document.getElementById('btn-new-roast').classList.remove('hidden');
  },

  hideNewRoastButton() {
    document.getElementById('btn-new-roast').classList.add('hidden');
  },

  async resetForNewRoast() {
    App.logEvent('Resetting for new roast...', 'text-blue-400');
    this._resetChartData();
    this.setActivePlan(null);
    this.previewPlan();
    this.chargeTimestamp = null;
    this.stopPolling();

    // Reset gauges
    document.getElementById('val-bt').textContent = '--';
    document.getElementById('val-et').textContent = '--';
    document.getElementById('val-ror').textContent = '--';
    document.getElementById('val-time').textContent = '0:00';
    document.getElementById('val-phase').textContent = 'IDLE';

    // Clear event log
    document.getElementById('event-log').innerHTML = '';

    // Reset state
    App.roastActive = false;
    this.coolingOn = false;
    document.getElementById('btn-cooling').textContent = 'Cooling Fan: OFF';
    document.getElementById('btn-cooling').className = 'w-full bg-cyan-700 hover:bg-cyan-600 text-white text-sm font-bold py-2 rounded-lg mb-2';

    // Re-enable charge, disable roast events
    this.updateButtons();
    this.hideNewRoastButton();
    this.hideRecommendation();

    // Tell backend to clear stale roast state
    await apiPost('/api/reset-roast');
    await this.fetchStatus();

    App.logEvent('Ready for new roast', 'text-green-400');
  },

  // -- Sliders --------------------------------------------------------------
  setupSliders() {
    this._setupSlider('ctrl-heat', 'heat-val', 'heat');
    this._setupSlider('ctrl-fan', 'fan-val', 'fan');
    this._setupSlider('ctrl-drum', 'drum-val', 'drum');
  },

  _setupSlider(id, valId, controlName) {
    const slider = document.getElementById(id);
    const valLabel = document.getElementById(valId);
    slider.addEventListener('input', () => {
      this.sliderActive = true;
      valLabel.textContent = slider.value + '%';
    });
    slider.addEventListener('change', () => {
      this.sliderActive = false;
      clearTimeout(this.sliderDebounce);
      this.sliderDebounce = setTimeout(() => {
        const body = {};
        body[controlName] = parseInt(slider.value);
        apiPost('/api/controls', body);
      }, 100);
    });
  },

  // -- AI Advisor -----------------------------------------------------------
  async checkAIAvailable() {
    const data = await apiGet('/api/ai/status');
    if (!data) return;
    App.aiAvailable = data.available || false;
    if (App.aiAvailable) {
      App.aiMode = data.mode || 'off';
      App.aiAutoEnabled = data.auto_control_enabled === true;
      const autoBtn = document.getElementById('ai-btn-auto');
      if (autoBtn && !App.aiAutoEnabled) {
        autoBtn.disabled = true;
        autoBtn.title = 'AI auto control is off. Set MAILLARD_AI_AUTO_CONTROL_ENABLED=true to enable.';
      }
      this.updateAIModeButtons();
      const m = data.model || '';
      const short = m.includes('sonnet') ? 'sonnet 4.5' : m.includes('haiku') ? 'haiku 4.5' : m;
      document.getElementById('ai-model').textContent = short;
      this.loadAITranscript();
      this.refreshAIPrompt();
    } else {
      document.getElementById('ai-reasoning').textContent = 'No API key — set ANTHROPIC_API_KEY';
      document.querySelectorAll('#ai-panel button[id^="ai-btn"]').forEach(b => b.disabled = true);
    }
  },

  async setAIMode(mode) {
    if (!App.aiAvailable) return;
    if (mode === 'auto' && !App.aiAutoEnabled) {
      App.logEvent('AI auto control is disabled (manual mode)', 'text-amber-400');
      return;
    }
    const data = await apiPost('/api/ai/toggle', { mode });
    if (data && data.status === 'error') {
      App.logEvent(data.error || 'AI mode change failed', 'text-amber-400');
      return;
    }
    if (data && data.status === 'ok') {
      App.aiMode = data.mode;
      this.updateAIModeButtons();
      App.logEvent(`AI mode: ${App.aiMode}`, 'text-violet-400');
      if (App.aiMode !== 'off') {
        document.getElementById('ai-panel').classList.add('ai-active');
        document.getElementById('ai-reasoning').textContent = App.roastActive ? 'Analyzing...' : 'Ready — will analyze after Charge';
      } else {
        document.getElementById('ai-panel').classList.remove('ai-active');
        document.getElementById('ai-reasoning').textContent = 'AI advisor inactive';
        document.getElementById('ai-suggestions').innerHTML = '';
        document.getElementById('ai-ask').classList.add('hidden');
      }
    }
  },

  updateAIModeButtons() {
    ['auto', 'suggest', 'off'].forEach(m => {
      const btn = document.getElementById(`ai-btn-${m}`);
      btn.className = m === App.aiMode
        ? 'flex-1 text-xs py-1 px-2 rounded ai-mode-active transition-colors'
        : 'flex-1 text-xs py-1 px-2 rounded bg-slate-700 text-slate-400 hover:bg-slate-600 transition-colors';
      if (btn.disabled) btn.className += ' opacity-40 cursor-not-allowed';
    });
  },

  _esc(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  },

  _formatTranscriptTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString();
  },

  async loadAITranscript() {
    if (!App.aiAvailable) return;
    const data = await apiGet('/api/ai/transcript');
    if (!data || !data.available) return;
    this._aiTranscript = data.entries || [];
    this.renderAITranscript();
  },

  handleAITranscriptEvent(msg) {
    const entry = {
      id: msg.entry_id || msg.id,
      timestamp: msg.timestamp,
      role: msg.role,
      kind: msg.kind,
      content: msg.content,
      suggestion: msg.suggestion || null,
      metadata: msg.metadata || {},
    };
    if (entry.id && this._aiTranscript.some(e => e.id === entry.id)) return;
    this._aiTranscript.push(entry);
    if (this._aiTranscript.length > 200) this._aiTranscript.shift();
    this.renderAITranscript();
  },

  renderAITranscript() {
    const container = document.getElementById('ai-transcript');
    if (!container) return;
    if (!this._aiTranscript.length) {
      container.innerHTML = '<div class="text-slate-500 italic">No AI transcript yet.</div>';
      return;
    }
    container.innerHTML = '';
    this._aiTranscript.slice(-80).forEach(entry => {
      const div = document.createElement('div');
      const role = entry.role || 'system';
      const roleColor = role === 'operator' ? 'text-amber-300' : role === 'ai' ? 'text-violet-300' : 'text-slate-400';
      div.className = 'rounded border border-slate-800 bg-slate-950/60 p-2';
      div.innerHTML = `
        <div class="flex items-center justify-between gap-2 mb-1">
          <span class="${roleColor} uppercase tracking-wide text-[10px]">${this._esc(role)} · ${this._esc(entry.kind || 'message')}</span>
          <span class="text-[10px] text-slate-600">${this._esc(this._formatTranscriptTime(entry.timestamp))}</span>
        </div>
        <div class="text-slate-300 whitespace-pre-wrap">${this._esc(entry.content || '')}</div>`;
      container.appendChild(div);
    });
    container.scrollTop = container.scrollHeight;
  },

  handleAISuggestion(msg) {
    const reasonEl = document.getElementById('ai-reasoning');
    reasonEl.textContent = msg.reasoning || '';
    reasonEl.classList.add('ai-pulse');
    setTimeout(() => reasonEl.classList.remove('ai-pulse'), 500);

    this._lastAISuggestion = {
      heat: msg.suggested_heat,
      fan: msg.suggested_fan,
      drum: msg.suggested_drum,
      event: msg.suggested_event,
      drop: !!msg.drop,
    };

    const container = document.getElementById('ai-suggestions');
    container.innerHTML = '';

    if (msg.suggested_heat != null) {
      const div = document.createElement('div');
      div.className = 'flex items-center justify-between';
      if (msg.auto_applied) {
        div.innerHTML = `<span class="text-xs ai-applied">Applied: Heat &rarr; ${msg.suggested_heat}%</span>`;
      } else {
        div.innerHTML = `<span class="text-xs text-slate-300">Heat &rarr; ${msg.suggested_heat}%</span>
          <button onclick="Roast.applyAISuggestion({heat:${msg.suggested_heat}})"
                  class="text-xs bg-violet-700 hover:bg-violet-600 text-white px-2 py-0.5 rounded">Apply</button>`;
      }
      container.appendChild(div);
    }

    if (msg.suggested_fan != null) {
      const div = document.createElement('div');
      div.className = 'flex items-center justify-between';
      if (msg.auto_applied) {
        div.innerHTML = `<span class="text-xs ai-applied">Applied: Fan &rarr; ${msg.suggested_fan}%</span>`;
      } else {
        div.innerHTML = `<span class="text-xs text-slate-300">Fan &rarr; ${msg.suggested_fan}%</span>
          <button onclick="Roast.applyAISuggestion({fan:${msg.suggested_fan}})"
                  class="text-xs bg-violet-700 hover:bg-violet-600 text-white px-2 py-0.5 rounded">Apply</button>`;
      }
      container.appendChild(div);
    }

    if (msg.suggested_drum != null) {
      const div = document.createElement('div');
      div.className = 'flex items-center justify-between';
      if (msg.auto_applied) {
        div.innerHTML = `<span class="text-xs ai-applied">Applied: Drum &rarr; ${msg.suggested_drum}%</span>`;
      } else {
        div.innerHTML = `<span class="text-xs text-slate-300">Drum &rarr; ${msg.suggested_drum}%</span>
          <button onclick="Roast.applyAISuggestion({drum:${msg.suggested_drum}})"
                  class="text-xs bg-violet-700 hover:bg-violet-600 text-white px-2 py-0.5 rounded">Apply</button>`;
      }
      container.appendChild(div);
    }

    if (msg.suggested_event) {
      const label = msg.suggested_event.replace(/_/g, ' ');
      const div = document.createElement('div');
      div.className = 'flex items-center justify-between';
      div.innerHTML = `<span class="text-xs text-amber-300">Mark: ${label}</span>
        <button onclick="Roast.applyAISuggestion({event:'${msg.suggested_event}'})"
                class="text-xs bg-amber-700 hover:bg-amber-600 text-white px-2 py-0.5 rounded">Confirm</button>`;
      container.appendChild(div);
    }

    if (msg.drop) {
      const div = document.createElement('div');
      div.className = 'flex items-center justify-between';
      div.innerHTML = `<span class="text-xs text-red-400 font-bold">DROP recommended</span>
        <button onclick="Roast.doDrop()"
                class="text-xs bg-red-700 hover:bg-red-600 text-white px-2 py-0.5 rounded">Drop</button>`;
      container.appendChild(div);
      App.speak('Drop recommended! End the roast now.', true);
    }

    if (!msg.auto_applied && (msg.suggested_heat != null || msg.suggested_fan != null || msg.suggested_drum != null || msg.suggested_event || msg.drop)) {
      const div = document.createElement('div');
      div.className = 'flex gap-2 justify-end';
      div.innerHTML = `
        <button onclick="Roast.recordAIAction('rejected')"
                class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-2 py-0.5 rounded">Reject</button>
        <button onclick="Roast.recordAIAction('dismissed')"
                class="text-xs bg-slate-800 hover:bg-slate-700 text-slate-400 px-2 py-0.5 rounded">Dismiss</button>`;
      container.appendChild(div);
    }

    if (!msg.drop) {
      const parts = [];
      if (msg.auto_applied) {
        if (msg.suggested_heat != null) parts.push(`Heat set to ${msg.suggested_heat} percent`);
        if (msg.suggested_fan != null) parts.push(`Fan set to ${msg.suggested_fan} percent`);
        if (msg.suggested_drum != null) parts.push(`Drum set to ${msg.suggested_drum} percent`);
      } else {
        if (msg.suggested_heat != null) parts.push(`Suggests heat ${msg.suggested_heat} percent`);
        if (msg.suggested_fan != null) parts.push(`Suggests fan ${msg.suggested_fan} percent`);
        if (msg.suggested_drum != null) parts.push(`Suggests drum ${msg.suggested_drum} percent`);
      }
      if (msg.suggested_event) parts.push(`Mark ${msg.suggested_event.replace(/_/g, ' ')}`);
      if (parts.length) App.speak(parts.join('. '));
    }

    const askEl = document.getElementById('ai-ask');
    if (msg.ask_operator) {
      askEl.innerHTML = `<div class="flex items-start justify-between gap-2">
        <span>${msg.ask_operator.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</span>
        <button onclick="document.getElementById('ai-ask').classList.add('hidden')"
                class="shrink-0 text-xs bg-yellow-700 hover:bg-yellow-600 text-white px-2 py-0.5 rounded">OK</button>
      </div>`;
      askEl.classList.remove('hidden');
      App.speak(msg.ask_operator);
    } else {
      askEl.classList.add('hidden');
    }

    App.logEvent(`AI: ${msg.reasoning || ''}`, 'text-violet-400');
  },

  async applyAISuggestion(suggestion) {
    const data = await apiPost('/api/ai/confirm', suggestion);
    if (data && data.status === 'ok' && data.applied) {
      App.logEvent(`Applied: ${data.applied.join(', ')}`, 'text-green-400');
      this.fetchStatus();
    }
  },

  async recordAIAction(action) {
    if (!this._lastAISuggestion) return;
    const data = await apiPost('/api/ai/action', {
      action,
      suggestion: this._lastAISuggestion,
    });
    if (data && data.status === 'ok') {
      App.logEvent(`AI suggestion ${action}`, action === 'rejected' ? 'text-yellow-400' : 'text-slate-400');
      if (action !== 'accepted') {
        document.getElementById('ai-suggestions').innerHTML = '';
      }
    }
  },

  async sendAIMessage(event) {
    event.preventDefault();
    if (!App.aiAvailable) return;
    const input = document.getElementById('ai-message-input');
    const message = input.value.trim();
    if (!message) return;
    const data = await apiPost('/api/ai/message', { message });
    if (data && data.status === 'ok') {
      input.value = '';
      App.logEvent('Sent operator message to AI', 'text-violet-400');
      this.loadAITranscript();
    }
  },

  async refreshAIPrompt() {
    if (!App.aiAvailable) return;
    const el = document.getElementById('ai-prompt-snapshot');
    if (!el) return;
    const data = await apiGet('/api/ai/prompt');
    if (!data || !data.available) {
      el.textContent = data?.message || 'No prompt built yet.';
      return;
    }
    el.textContent = JSON.stringify({
      model: data.model,
      interval_seconds: data.interval_seconds,
      metadata: data.metadata,
      system_prompt: data.system_prompt,
      messages: data.messages,
    }, null, 2);
  },

  // -- Pre-Roast Recommendations -------------------------------------------
  requestRecommendation() {
    clearTimeout(this._recDebounce);
    this._recDebounce = setTimeout(() => this._fetchRecommendation(), 500);
  },

  async _fetchRecommendation() {
    const coffeeId = document.getElementById('coffee-select').value;
    if (!coffeeId) { this.hideRecommendation(); return; }

    const profileId = document.getElementById('profile-select').value || null;
    const batchSize = parseInt(document.getElementById('batch-weight').value) || null;

    // Show loading state
    const card = document.getElementById('recommendation-card');
    card.classList.remove('hidden');
    document.getElementById('rec-loading').classList.remove('hidden');
    document.getElementById('rec-content').classList.add('hidden');

    const data = await apiPost('/api/ai/recommend', {
      coffee_id: coffeeId,
      profile_id: profileId,
      batch_size_grams: batchSize,
    });

    if (data && data.status === 'ok' && data.recommendation) {
      this.renderRecommendation(data.recommendation);
    } else {
      this.hideRecommendation();
    }
  },

  renderRecommendation(rec) {
    this._lastRecommendation = rec;
    const card = document.getElementById('recommendation-card');
    card.classList.remove('hidden');
    document.getElementById('rec-loading').classList.add('hidden');
    document.getElementById('rec-content').classList.remove('hidden');

    document.getElementById('rec-charge').textContent = `${rec.charge_temp.toFixed(0)}°C`;
    document.getElementById('rec-heat').textContent = `${rec.heat_percent}%`;
    document.getElementById('rec-fan').textContent = `${rec.fan_percent}%`;
    document.getElementById('rec-drum').textContent = `${rec.drum_percent}%`;
    document.getElementById('rec-reasoning').textContent = rec.reasoning;

    // Confidence badge
    const badge = document.getElementById('rec-confidence');
    const colors = {
      high: 'bg-green-700 text-green-300',
      medium: 'bg-yellow-700 text-yellow-300',
      low: 'bg-slate-700 text-slate-400',
    };
    badge.className = `text-[10px] px-1.5 py-0.5 rounded-full ${colors[rec.confidence] || colors.low}`;
    badge.textContent = rec.source === 'ai' ? `AI · ${rec.confidence}` : rec.confidence;

    // Auto-fill preheat temp
    document.getElementById('preheat-temp').value = rec.charge_temp.toFixed(0);
  },

  applyRecommendation() {
    const rec = this._lastRecommendation;
    if (!rec) return;

    document.getElementById('preheat-temp').value = rec.charge_temp.toFixed(0);
    document.getElementById('ctrl-heat').value = rec.heat_percent;
    document.getElementById('heat-val').textContent = rec.heat_percent + '%';
    document.getElementById('ctrl-fan').value = rec.fan_percent;
    document.getElementById('fan-val').textContent = rec.fan_percent + '%';
    document.getElementById('ctrl-drum').value = rec.drum_percent;
    document.getElementById('drum-val').textContent = rec.drum_percent + '%';

    App.logEvent(`Applied recommendation: ${rec.charge_temp.toFixed(0)}°C, H${rec.heat_percent}% F${rec.fan_percent}% D${rec.drum_percent}%`, 'text-violet-400');
  },

  hideRecommendation() {
    this._lastRecommendation = null;
    document.getElementById('recommendation-card').classList.add('hidden');
  },
};
