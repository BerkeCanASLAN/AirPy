// AirPy Sync - background: reads AirPy's local API and collects tab states.
'use strict';

const FIRST_PORT = 53307;
const PORT_COUNT = 10;
const STATUS_TTL_MS = 1000;     // share the same reply for this long (many tabs/frames)
const OFFLINE_TTL_MS = 4000;    // if AirPy was not found, do not scan again for this long
const REPORT_TTL_MS = 8000;     // ignore a tab report older than this

let port = 0;
let cached = { at: 0, value: null };
let inflight = null;

const OFFLINE = { connected: false, streaming: false, latency_ms: 0, buffer_ms: 0, port: 0 };

async function getJson(url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'no-store' });
    return response.ok ? await response.json() : null;
  } catch (_) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

const valid = (info) => info && info.app === 'AirPy' && Number.isFinite(info.latency_ms);

async function queryAirpy() {
  let info = null;
  if (port) {
    info = await getJson(`http://127.0.0.1:${port}/airpy/sync`, 800);
    if (!valid(info)) { info = null; port = 0; }
  }
  if (!info) {
    for (let p = FIRST_PORT; p < FIRST_PORT + PORT_COUNT && !info; p++) {
      const found = await getJson(`http://127.0.0.1:${p}/airpy/sync`, 300);
      if (valid(found)) { info = found; port = p; }
    }
  }
  if (!info) return { ...OFFLINE };
  return {
    connected: true,
    streaming: !!info.streaming,
    latency_ms: Math.max(0, Math.round(info.latency_ms)),
    buffer_ms: info.buffer_ms | 0,
    version: info.version || '',
    port,
  };
}

async function airpyStatus() {
  const now = Date.now();
  if (cached.value && now - cached.at < (cached.value.connected ? STATUS_TTL_MS : OFFLINE_TTL_MS)) {
    return cached.value;
  }
  if (!inflight) {
    inflight = queryAirpy().then((value) => {
      cached = { at: Date.now(), value };
      return value;
    }).finally(() => { inflight = null; });
  }
  return inflight;
}

// ---- Tab states ----------------------------------------------------
const tabStates = new Map(); // tabId -> Map(frameId -> { state, at })

function remember(tabId, frameId, state) {
  let frames = tabStates.get(tabId);
  if (!frames) { frames = new Map(); tabStates.set(tabId, frames); }
  frames.set(frameId, { state, at: Date.now() });
  badge(tabId);
}

function summary(tabId) {
  const frames = tabStates.get(tabId);
  const out = { videos: 0, engaged: 0, delay: 0, failed: '' };
  if (!frames) return out;
  const now = Date.now();
  for (const [frameId, entry] of frames) {
    if (now - entry.at > REPORT_TTL_MS) { frames.delete(frameId); continue; }
    out.videos += entry.state.videos | 0;
    out.engaged += entry.state.engaged | 0;
    out.delay = Math.max(out.delay, entry.state.delay | 0);
    if (entry.state.failed && !out.failed) out.failed = entry.state.failed;
  }
  return out;
}

function badge(tabId) {
  const s = summary(tabId);
  chrome.action.setBadgeBackgroundColor({ tabId, color: '#2f6df6' }).catch(() => {});
  chrome.action.setBadgeText({ tabId, text: s.engaged > 0 ? String(s.delay) : '' }).catch(() => {});
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch (message && message.type) {
    case 'status':
      airpyStatus().then(sendResponse);
      return true;
    case 'report':
      if (sender.tab) remember(sender.tab.id, sender.frameId || 0, message.state || {});
      sendResponse(true);
      return false;
    case 'tab':
      sendResponse(summary(message.tabId));
      return false;
    default:
      return false;
  }
});

chrome.tabs.onRemoved.addListener((tabId) => tabStates.delete(tabId));
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.status === 'loading') { tabStates.delete(tabId); badge(tabId); }
});
