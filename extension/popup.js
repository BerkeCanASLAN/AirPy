'use strict';

const DEFAULTS = { enabled: true, mode: 'auto', manual_ms: 250, offset_ms: 0, disabled_sites: [] };
const $ = (id) => document.getElementById(id);

let settings = { ...DEFAULTS };
let tab = null;
let host = '';

const signed = (n) => (n > 0 ? `+${n}` : `${n}`);

function save(patch) {
  settings = { ...settings, ...patch };
  chrome.storage.local.set(patch);
  render();
}

function render() {
  $('enabled').checked = settings.enabled;
  for (const button of document.querySelectorAll('[data-mode]')) {
    button.setAttribute('aria-selected', String(button.dataset.mode === settings.mode));
  }
  $('autoBox').hidden = settings.mode !== 'auto';
  $('manualBox').hidden = settings.mode !== 'manual';
  $('offset').value = settings.offset_ms;
  $('offsetOut').textContent = `${signed(settings.offset_ms)} ms`;
  $('manual').value = settings.manual_ms;
  $('manualOut').textContent = `${settings.manual_ms} ms`;
  $('siteOff').checked = !!host && settings.disabled_sites.includes(host);
  $('siteOff').disabled = !host;
  $('siteLabel').textContent = host ? `Turn off on ${host}` : 'Turn off on this site';
}

function bindSlider(id, key, min, max) {
  const input = $(id);
  input.addEventListener('input', () => save({ [key]: Number(input.value) }));
  $(`${id}Down`).addEventListener('click', () => save({ [key]: Math.max(min, settings[key] - 5) }));
  $(`${id}Up`).addEventListener('click', () => save({ [key]: Math.min(max, settings[key] + 5) }));
}

async function refreshStatus() {
  const status = await chrome.runtime.sendMessage({ type: 'status' }).catch(() => null);
  const dot = $('dot');
  const text = $('airpyText');
  if (!status || !status.connected) {
    dot.className = 'dot';
    text.textContent = 'AirPy is not running or was not found';
    $('calibrate').disabled = true;
    $('footHint').textContent = 'AirPy must be running for the sync test.';
  } else if (!status.streaming) {
    dot.className = 'dot warn';
    text.textContent = 'AirPy connected · streaming is off';
    $('calibrate').disabled = false;
    $('footHint').textContent = 'For the test, switch a speaker on in AirPy.';
  } else {
    dot.className = 'dot ok';
    text.textContent = `AirPy is streaming · latency ${status.latency_ms} ms`;
    $('calibrate').disabled = false;
    $('footHint').textContent = '';
  }
  $('autoHint').dataset.port = status && status.port ? status.port : '';

  const line = $('tabText');
  if (!tab) { line.textContent = ''; return; }
  const summary = await chrome.runtime.sendMessage({ type: 'tab', tabId: tab.id }).catch(() => null);
  if (!settings.enabled) line.textContent = 'The extension is off';
  else if (host && settings.disabled_sites.includes(host)) line.textContent = 'Off on this site';
  else if (!summary || !summary.videos) line.textContent = 'No video on this page';
  else if (summary.engaged) line.textContent = `Delaying ${summary.engaged} video(s) by ${summary.delay} ms`;
  else if (summary.failed) line.textContent = `Does not work on this video: ${summary.failed}`;
  else if (settings.mode === 'auto' && (!status || !status.streaming)) line.textContent = 'No delay is applied because streaming is off';
  else line.textContent = 'The delay is applied when a video plays';
}

async function init() {
  settings = { ...DEFAULTS, ...(await chrome.storage.local.get(DEFAULTS)) };
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try { host = tab && tab.url ? new URL(tab.url).hostname : ''; } catch (_) { host = ''; }
  if (host && !/^[a-z0-9.-]+$/i.test(host)) host = '';  // chrome://, files, etc.
  render();
  requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add('ready')));

  $('enabled').addEventListener('change', (e) => save({ enabled: e.target.checked }));
  for (const button of document.querySelectorAll('[data-mode]')) {
    button.addEventListener('click', () => save({ mode: button.dataset.mode }));
  }
  bindSlider('offset', 'offset_ms', -300, 600);
  bindSlider('manual', 'manual_ms', 0, 1500);
  $('siteOff').addEventListener('change', (e) => {
    const sites = new Set(settings.disabled_sites);
    if (e.target.checked) sites.add(host); else sites.delete(host);
    save({ disabled_sites: [...sites] });
  });
  $('calibrate').addEventListener('click', async () => {
    const status = await chrome.runtime.sendMessage({ type: 'status' }).catch(() => null);
    if (status && status.port) {
      chrome.tabs.create({ url: `http://127.0.0.1:${status.port}/airpy/calibrate` });
      window.close();
    }
  });

  refreshStatus();
  setInterval(refreshStatus, 1000);
}

init();
