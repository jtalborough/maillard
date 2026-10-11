// ---------------------------------------------------------------------------
// App — shared state, WebSocket, init, navigation
// ---------------------------------------------------------------------------
const App = {
  ws: null,
  wsRetryDelay: 1000,
  roastActive: false,
  coffeeList: [],
  profileList: [],
  voiceEnabled: true,
  aiMode: 'off',
  aiAutoEnabled: false,
  aiAvailable: false,
  activePanel: null, // null = roast view, 'coffees', 'profiles', 'history'

  // -- Formatters -----------------------------------------------------------
  formatTime(seconds) {
    if (seconds == null) return '00:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  },

  // -- Voice ----------------------------------------------------------------
  speak(text, urgent = false) {
    if (!this.voiceEnabled || !window.speechSynthesis) return;
    try {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.rate = urgent ? 1.2 : 1.0;
      u.pitch = urgent ? 1.1 : 1.0;
      u.volume = 1.0;
      window.speechSynthesis.speak(u);
    } catch (e) {
      console.warn('Speech synthesis failed:', e);
    }
  },

  toggleVoice() {
    this.voiceEnabled = !this.voiceEnabled;
    const btn = document.getElementById('btn-voice');
    btn.textContent = this.voiceEnabled ? 'Voice: ON' : 'Voice: OFF';
    // Warm up speech synthesis with user interaction (browser autoplay policy)
    if (this.voiceEnabled) {
      this.speak('Voice enabled');
    }
    btn.className = this.voiceEnabled
      ? 'text-xs bg-slate-700 hover:bg-slate-600 text-white px-2 py-0.5 rounded'
      : 'text-xs bg-slate-800 hover:bg-slate-700 text-slate-500 px-2 py-0.5 rounded';
  },

  // -- Event Log ------------------------------------------------------------
  logEvent(msg, color = 'text-slate-300') {
    const log = document.getElementById('event-log');
    if (!log) return;
    const placeholder = log.querySelector('.italic');
    if (placeholder) placeholder.remove();

    const now = new Date().toLocaleTimeString();
    const div = document.createElement('div');
    div.className = `log-entry ${color}`;
    div.textContent = `[${now}] ${msg}`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    while (log.children.length > 200) log.removeChild(log.firstChild);
  },

  safetyAlerts: new Map(),
  alertTimer: null,

  showAlert(msg, level = 'warning', condition = null, cleared = false) {
    level = level.toLowerCase();
    const banner = document.getElementById('alert-banner');
    if (condition && cleared && !this.safetyAlerts.has(condition)) return;
    clearTimeout(this.alertTimer);
    if (condition) {
      if (cleared) this.safetyAlerts.delete(condition);
      else this.safetyAlerts.set(condition, {msg, level});
    }
    if (this.safetyAlerts.size) {
      msg = Array.from(this.safetyAlerts.values()).map(a => a.msg).join(' | ');
      level = 'critical';
    } else if (cleared) {
      banner.classList.add('hidden');
      return;
    }
    banner.className = level === 'critical' || level === 'emergency'
      ? 'fixed top-0 left-0 right-0 z-50 p-3 text-center font-bold text-white bg-red-600 alert-banner'
      : 'fixed top-0 left-0 right-0 z-50 p-3 text-center font-bold text-white bg-yellow-600 alert-banner';
    banner.textContent = msg;
    banner.classList.remove('hidden');
    if (!this.safetyAlerts.size) {
      this.alertTimer = setTimeout(() => banner.classList.add('hidden'), 8000);
    }
  },

  // -- WebSocket ------------------------------------------------------------
  connectWS() {
    const dot = document.getElementById('ws-dot');
    const label = document.getElementById('ws-label');
    dot.className = 'connection-dot dot-connecting';
    label.textContent = 'Connecting...';

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.ws = new WebSocket(`${proto}://${location.host}/ws`);

    this.ws.onopen = () => {
      dot.className = 'connection-dot dot-connected';
      label.textContent = 'Connected';
      this.wsRetryDelay = 1000;
      this.logEvent('WebSocket connected', 'text-green-400');
      Roast.fetchStatus();
    };

    this.ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        Roast.handleWSMessage(msg);
      } catch (err) {
        console.error('WS parse error', err);
      }
    };

    this.ws.onclose = () => {
      dot.className = 'connection-dot dot-disconnected';
      label.textContent = 'Disconnected';
      this.logEvent('WebSocket disconnected — reconnecting...', 'text-yellow-400');
      setTimeout(() => this.connectWS(), this.wsRetryDelay);
      this.wsRetryDelay = Math.min(this.wsRetryDelay * 1.5, 30000);
    };

    this.ws.onerror = () => { this.ws.close(); };
  },

  // -- Navigation -----------------------------------------------------------
  showPanel(name) {
    const drawer = document.getElementById('side-drawer');
    const drawerTitle = document.getElementById('drawer-title');
    const drawerBody = document.getElementById('drawer-body');

    // Toggle: clicking same tab closes drawer
    if (this.activePanel === name) {
      this.activePanel = null;
      drawer.classList.add('hidden');
      this.updateNavButtons();
      return;
    }

    this.activePanel = name;
    drawer.classList.remove('hidden');
    this.updateNavButtons();

    switch (name) {
      case 'coffees':
        drawerTitle.textContent = 'Coffee Inventory';
        Coffees.render(drawerBody);
        break;
      case 'inventory':
        drawerTitle.textContent = 'Green Inventory';
        Inventory.render(drawerBody);
        break;
      case 'profiles':
        drawerTitle.textContent = 'Roast Profiles';
        Profiles.render(drawerBody);
        break;
      case 'history':
        drawerTitle.textContent = 'Roast History';
        History.render(drawerBody);
        break;
    }
  },

  closeDrawer() {
    this.activePanel = null;
    document.getElementById('side-drawer').classList.add('hidden');
    this.updateNavButtons();
  },

  updateNavButtons() {
    ['coffees', 'inventory', 'profiles', 'history'].forEach(name => {
      const btn = document.getElementById(`nav-${name}`);
      if (btn) {
        btn.className = this.activePanel === name
          ? 'text-xs px-3 py-1 rounded font-medium bg-amber-600 text-white'
          : 'text-xs px-3 py-1 rounded font-medium bg-slate-700 text-slate-300 hover:bg-slate-600';
      }
    });
  },

  // -- Init -----------------------------------------------------------------
  init() {
    Roast.initChart();
    Roast.setupSliders();
    this.connectWS();
    Roast.loadCoffees();
    Roast.loadProfiles();
    Roast.checkAIAvailable();
    setInterval(() => Roast.fetchStatus(), 5000);
  },
};

document.addEventListener('DOMContentLoaded', () => App.init());
