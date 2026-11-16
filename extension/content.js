// AirPy Sync - content script.
//
// When Windows audio goes to the speakers over AirPlay the sound trails the picture by `latency_ms`.
// We cannot advance the sound (it is live), but we can delay the picture by the same amount:
//   video.captureStream() -> MediaStreamTrackProcessor -> hold the frames -> <canvas>
// The original <video> is made invisible (its sound keeps playing) and a canvas is drawn in its place.
(() => {
  'use strict';
  if (globalThis.__airpySync) return;

  const DEFAULTS = { enabled: true, mode: 'auto', manual_ms: 250, offset_ms: 0, disabled_sites: [] };
  const MAX_DELAY_MS = 2500;
  // captureStream delivers frames ~60 ms after the video element put them on screen
  // (measured: a constant +60-75 ms over the target). That difference already adds to the
  // delay in the audio/video match, so it is subtracted from the wait time.
  const CAPTURE_LAG_MS = 60;
  const MIN_ACTIVE_MS = CAPTURE_LAG_MS + 10;  // no delay smaller than this is applied (the pipeline is already later)
  const MIN_W = 200;
  const MIN_H = 110;
  const MEMORY_BUDGET = 320 * 1024 * 1024;  // upper limit for the frames being held back
  const MAX_QUEUE = 400;
  const SCAN_MS = 500;
  const POLL_MS = 1500;
  const HEARTBEAT_MS = 3000;
  const FIRST_FRAME_TIMEOUT_MS = 3500;
  const RETRY_AFTER_FAIL_MS = 30000;

  const supported =
    typeof MediaStreamTrackProcessor === 'function' &&
    typeof HTMLMediaElement.prototype.captureStream === 'function' &&
    typeof createImageBitmap === 'function';

  let settings = { ...DEFAULTS };
  let airpy = { connected: false, streaming: false, latency_ms: 0 };
  let lastReport = '';
  let lastReportAt = 0;
  let disposed = false;

  const renderers = new Map();   // video -> DelayedVideo (active ones only)
  const failures = new WeakMap(); // video -> { until, reason }

  const topHost = () => {
    try {
      const origins = location.ancestorOrigins;
      if (origins && origins.length) return new URL(origins[origins.length - 1]).hostname;
    } catch (_) { /* ignore */ }
    return location.hostname;
  };

  function targetDelay() {
    if (!settings.enabled || settings.disabled_sites.includes(topHost())) return 0;
    let delay;
    if (settings.mode === 'manual') delay = settings.manual_ms;
    else delay = airpy.streaming ? airpy.latency_ms + settings.offset_ms : 0;
    return Math.max(0, Math.min(MAX_DELAY_MS, Math.round(delay)));
  }

  const waitForTrack = (stream, ms) =>
    new Promise((resolve) => {
      const done = (track) => { clearTimeout(timer); stream.removeEventListener('addtrack', onAdd); resolve(track); };
      const onAdd = () => { const t = stream.getVideoTracks()[0]; if (t) done(t); };
      const timer = setTimeout(() => done(null), ms);
      stream.addEventListener('addtrack', onAdd);
    });

  class DelayedVideo {
    constructor(video) {
      this.video = video;
      this.delay = 0;
      this.alive = false;
      this.generation = 0;
      this.canvas = null;
      this.ctx = null;
      this.queue = [];
      this.current = null;
      this.reader = null;
      this.track = null;
      this.raf = 0;
      this.saved = null;
      this.box = '';
      this.fitMode = 'contain';
      this.minInterval = 14;
      this.lastKept = -Infinity;
      this.startedAt = 0;
      this.gotFrame = false;
      this.restartTimer = 0;
      this.tick = this.tick.bind(this);
    }

    get engaged() { return this.alive && this.gotFrame; }

    setDelay(ms) {
      if (Math.abs(ms - this.delay) < 8 && this.delay) return;  // ignore small fluctuations
      this.delay = ms;
      this.box = '';  // recompute the memory budget
    }

    async start() {
      if (this.alive) return;
      this.alive = true;
      this.gotFrame = false;
      this.startedAt = performance.now();
      const gen = ++this.generation;
      try {
        const stream = this.video.captureStream();
        let track = stream.getVideoTracks()[0] || (await waitForTrack(stream, 3000));
        if (gen !== this.generation) { if (track) track.stop(); return; }
        if (!track) throw new Error('no video track');
        this.track = track;
        this.reader = new MediaStreamTrackProcessor({ track }).readable.getReader();
      } catch (error) {
        if (gen === this.generation) {
          this.fail(error && error.name === 'NotSupportedError' ? 'protected content (DRM)' : 'could not capture frames');
        }
        return;
      }
      this.raf = requestAnimationFrame(this.tick);
      this.pump(gen);
    }

    fail(reason) {
      failures.set(this.video, { until: performance.now() + RETRY_AFTER_FAIL_MS, reason });
      this.stop();
      renderers.delete(this.video);
    }

    async pump(gen) {
      const reader = this.reader;
      try {
        for (;;) {
          const { value: frame, done } = await reader.read();
          if (done) break;
          if (gen !== this.generation) { frame.close(); break; }
          this.accept(frame);
        }
      } catch (_) { /* the track ended or was cancelled */ }
      if (gen === this.generation && this.alive) this.scheduleRestart();
    }

    scheduleRestart() {
      // The source changed (new video, MSE rebuilt): restart after a short wait
      this.stopStream();
      clearTimeout(this.restartTimer);
      this.restartTimer = setTimeout(() => {
        if (!this.alive) return;
        this.alive = false;
        this.start();
      }, 400);
    }

    accept(frame) {
      const now = performance.now();
      if (!this.canvas) this.mount();
      if (document.hidden || now - this.lastKept < this.minInterval || this.queue.length >= MAX_QUEUE) {
        frame.close();
        return;
      }
      this.lastKept = now;
      const srcW = frame.displayWidth || frame.codedWidth;
      const srcH = frame.displayHeight || frame.codedHeight;
      const fit = this.computeFit(srcW, srcH);
      createImageBitmap(frame, { resizeWidth: fit.w, resizeHeight: fit.h, resizeQuality: 'low' })
        .then((bmp) => {
          if (!this.alive) { bmp.close(); return; }
          this.gotFrame = true;
          this.insert({ t: now, bmp, srcW, srcH });
        })
        .catch(() => { /* frame skipped */ })
        .finally(() => frame.close());
    }

    insert(entry) {
      let i = this.queue.length;
      while (i > 0 && this.queue[i - 1].t > entry.t) i--;
      this.queue.splice(i, 0, entry);
    }

    mount() {
      const video = this.video;
      const parent = video.parentNode;
      if (!parent) return;
      const canvas = document.createElement('canvas');
      canvas.setAttribute('data-airpy-sync', '');
      canvas.setAttribute('aria-hidden', 'true');
      const z = getComputedStyle(video).zIndex;
      canvas.style.cssText =
        'position:absolute!important;margin:0!important;padding:0!important;border:0!important;' +
        'pointer-events:none!important;background:transparent!important;max-width:none!important;' +
        'max-height:none!important;min-width:0!important;min-height:0!important;' +
        'transform:none!important;filter:none!important;' + (z && z !== 'auto' ? `z-index:${z}!important;` : '');
      parent.insertBefore(canvas, video.nextSibling);
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d', { alpha: true });
      this.layout();
      // Make the original invisible (sound and frames keep flowing)
      const style = video.style;
      this.saved = { value: style.getPropertyValue('opacity'), priority: style.getPropertyPriority('opacity') };
      style.setProperty('opacity', '0', 'important');
    }

    layout() {
      const v = this.video;
      const c = this.canvas;
      const w = v.clientWidth;
      const h = v.clientHeight;
      const key = `${v.offsetLeft},${v.offsetTop},${w},${h},${window.devicePixelRatio},${this.delay}`;
      if (key === this.box) return false;
      this.box = key;
      c.style.setProperty('left', `${v.offsetLeft}px`, 'important');
      c.style.setProperty('top', `${v.offsetTop}px`, 'important');
      c.style.setProperty('width', `${w}px`, 'important');
      c.style.setProperty('height', `${h}px`, 'important');
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      let cw = Math.max(1, Math.round(w * dpr));
      let ch = Math.max(1, Math.round(h * dpr));
      const cap = Math.min(1, 2560 / cw, 1440 / ch);
      cw = Math.max(1, Math.round(cw * cap));
      ch = Math.max(1, Math.round(ch * cap));
      let resized = false;
      if (c.width !== cw || c.height !== ch) { c.width = cw; c.height = ch; resized = true; }
      const fit = getComputedStyle(v).objectFit;
      this.fitMode = fit === 'cover' || fit === 'fill' ? fit : 'contain';
      // Keep the number of held frames within the memory budget: lower the frame rate if needed
      const maxFrames = Math.max(6, Math.floor(MEMORY_BUDGET / (cw * ch * 4)));
      this.minInterval = Math.max(14, (this.delay + 250) / maxFrames);
      return resized;
    }

    computeFit(srcW, srcH) {
      const cw = this.canvas.width;
      const ch = this.canvas.height;
      let w = cw;
      let h = ch;
      if (this.fitMode !== 'fill' && srcW > 0 && srcH > 0) {
        const scale = this.fitMode === 'cover' ? Math.max(cw / srcW, ch / srcH) : Math.min(cw / srcW, ch / srcH);
        w = srcW * scale;
        h = srcH * scale;
      }
      w = Math.max(1, Math.round(w));
      h = Math.max(1, Math.round(h));
      return { x: Math.round((cw - w) / 2), y: Math.round((ch - h) / 2), w, h };
    }

    tick() {
      if (!this.alive) return;
      this.raf = requestAnimationFrame(this.tick);
      if (!this.gotFrame && performance.now() - this.startedAt > FIRST_FRAME_TIMEOUT_MS && !this.video.paused) {
        this.fail('could not capture frames (possibly protected content)');
        return;
      }
      if (!this.canvas) return;
      const resized = this.layout();
      const now = performance.now();
      let due = null;
      const hold = Math.max(0, this.delay - CAPTURE_LAG_MS);
      while (this.queue.length && now - this.queue[0].t >= hold) {
        if (due) due.bmp.close();
        due = this.queue.shift();
      }
      if (due) {
        if (this.current) this.current.bmp.close();
        this.current = due;
        this.draw();
      } else if (resized && this.current) {
        this.draw();
      }
    }

    draw() {
      const { bmp, srcW, srcH } = this.current;
      const fit = this.computeFit(srcW, srcH);
      this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
      this.ctx.drawImage(bmp, fit.x, fit.y, fit.w, fit.h);
    }

    stopStream() {
      try { if (this.reader) this.reader.cancel(); } catch (_) { /* ignore */ }
      try { if (this.track) this.track.stop(); } catch (_) { /* ignore */ }
      this.reader = null;
      this.track = null;
    }

    stop() {
      this.alive = false;
      this.generation++;
      clearTimeout(this.restartTimer);
      cancelAnimationFrame(this.raf);
      this.stopStream();
      for (const entry of this.queue) entry.bmp.close();
      this.queue = [];
      if (this.current) { this.current.bmp.close(); this.current = null; }
      if (this.canvas) { this.canvas.remove(); this.canvas = null; this.ctx = null; }
      if (this.saved) {
        const style = this.video.style;
        if (this.saved.value) style.setProperty('opacity', this.saved.value, this.saved.priority);
        else style.removeProperty('opacity');
        this.saved = null;
      }
      this.gotFrame = false;
    }
  }

  // ---- Which videos does it apply to? ------------------------------------------
  function wants(video, delay, renderer) {
    if (!supported || delay < MIN_ACTIVE_MS) return false;
    // Live streams (calls/WebRTC): captureStream may return the same track and track.stop() kills the source;
    // also, extra delay is unwanted in a call. Touch file/MSE videos, not these.
    if (video.srcObject) return false;
    if (document.hidden || document.fullscreenElement === video) return false;
    if (video.clientWidth < MIN_W || video.clientHeight < MIN_H || video.readyState < 2) return false;
    if (video.muted || video.volume < 0.01) return false;  // no sync is needed if the sound is not heard
    if (typeof video.webkitAudioDecodedByteCount === 'number' &&
        video.webkitAudioDecodedByteCount === 0 && !(renderer && renderer.alive)) return false;
    if (!(renderer && renderer.alive) && video.paused) return false;
    const failure = failures.get(video);
    if (failure && failure.until > performance.now()) return false;
    return true;
  }

  function scan() {
    if (disposed) return;
    const delay = targetDelay();
    const videos = document.getElementsByTagName('video');
    for (const video of videos) {
      let renderer = renderers.get(video);
      if (wants(video, delay, renderer)) {
        if (!renderer) { renderer = new DelayedVideo(video); renderers.set(video, renderer); }
        renderer.setDelay(delay);
        if (!renderer.alive) renderer.start();
      } else if (renderer) {
        renderer.stop();
        renderers.delete(video);
      }
    }
    for (const [video, renderer] of renderers) {
      if (!video.isConnected) { renderer.stop(); renderers.delete(video); }
    }
    report();
  }

  function report(force) {
    const videos = document.getElementsByTagName('video');
    if (!videos.length && !lastReport) return;  // stay quiet if no video was ever seen
    let engaged = 0;
    for (const renderer of renderers.values()) if (renderer.engaged) engaged++;
    let failed = '';
    for (const video of videos) {
      const failure = failures.get(video);
      if (failure && failure.until > performance.now()) { failed = failure.reason; break; }
    }
    const state = { videos: videos.length, engaged, delay: engaged ? targetDelay() : 0, failed };
    const text = JSON.stringify(state);
    const now = Date.now();
    if (!force && text === lastReport && now - lastReportAt < HEARTBEAT_MS) return;
    lastReport = text;
    lastReportAt = now;
    send({ type: 'report', state });
  }

  function send(message) {
    if (!chrome.runtime || !chrome.runtime.id) { dispose(); return Promise.resolve(null); }
    try {
      return chrome.runtime.sendMessage(message).catch(() => null);
    } catch (_) {
      return Promise.resolve(null);
    }
  }

  async function poll() {
    if (disposed) return;
    if (document.getElementsByTagName('video').length && settings.enabled && settings.mode === 'auto') {
      const status = await send({ type: 'status' });
      if (status) airpy = status;
    }
  }

  function dispose() {
    // The extension was reloaded/removed: keep the old script from lingering and leaving a canvas behind
    if (disposed) return;
    disposed = true;
    for (const renderer of renderers.values()) renderer.stop();
    renderers.clear();
  }

  function applySettings(stored) {
    settings = { ...DEFAULTS, ...stored };
    if (!Array.isArray(settings.disabled_sites)) settings.disabled_sites = [];
    scan();
  }

  globalThis.__airpySync = { renderers, scan, dispose, get settings() { return settings; }, get airpy() { return airpy; } };

  chrome.storage.local.get(DEFAULTS).then(applySettings).catch(() => {});
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    const next = { ...settings };
    for (const [key, change] of Object.entries(changes)) next[key] = change.newValue;
    applySettings(next);
  });

  document.addEventListener('play', scan, true);
  document.addEventListener('pause', () => setTimeout(scan, 0), true);
  document.addEventListener('volumechange', scan, true);
  document.addEventListener('fullscreenchange', scan);
  document.addEventListener('visibilitychange', scan);
  setInterval(scan, SCAN_MS);
  setInterval(() => poll().then(scan), POLL_MS);
  poll().then(scan);
})();
