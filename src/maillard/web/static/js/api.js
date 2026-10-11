// ---------------------------------------------------------------------------
// Shared API helper — wraps fetch with JSON handling and error logging
// ---------------------------------------------------------------------------
const API_BASE = window.location.origin;

async function apiGet(path) {
  try {
    const res = await fetch(`${API_BASE}${path}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error(`GET ${path} failed:`, err);
    return null;
  }
}

async function apiPost(path, body = {}) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = await res.json();
    if (!res.ok) data = {status: 'error', error: typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data.error || data)};
    if (data.status === 'error') {
      App.logEvent(`Error: ${data.error}`, 'text-red-400');
    }
    return data;
  } catch (err) {
    App.logEvent(`API error: ${err.message}`, 'text-red-400');
    return null;
  }
}

async function apiPut(path, body = {}) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.status === 'error') {
      App.logEvent(`Error: ${data.error}`, 'text-red-400');
    }
    return data;
  } catch (err) {
    App.logEvent(`API error: ${err.message}`, 'text-red-400');
    return null;
  }
}

async function apiDelete(path) {
  try {
    const res = await fetch(`${API_BASE}${path}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.status === 'error') {
      App.logEvent(`Error: ${data.error}`, 'text-red-400');
    }
    return data;
  } catch (err) {
    App.logEvent(`API error: ${err.message}`, 'text-red-400');
    return null;
  }
}

async function apiPatch(path, body = {}) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.status === 'error') {
      App.logEvent(`Error: ${data.error}`, 'text-red-400');
    }
    return data;
  } catch (err) {
    App.logEvent(`API error: ${err.message}`, 'text-red-400');
    return null;
  }
}
