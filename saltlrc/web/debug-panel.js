"use strict";
/**
 * SaltLRC debug panel — a self-contained performance probe. 颜色写死深色：
 * 它同样是叠加在工作区上的诊断面板，不走主题令牌。
 *
 * Drop it into any page: it needs nothing but a renderer-like object exposing
 * `stats`, `peaks`, `doc`, `canvas` and a 2d context it can instrument.
 *
 *   const panel = new SaltDebugPanel(wave, {hotkey: true});
 *   panel.toggle();          // show / hide
 *   panel.report();          // text summary for bug reports
 *   panel.exportJSON();      // download the raw frame samples
 *
 * While hidden it removes its context wrappers, so it costs nothing during
 * normal use. The number that matters for zoom jank is "绘制调用" (canvas draw
 * calls per repaint): wall-clock canvas time cannot be measured from script,
 * but draw-call count can.
 */
class SaltDebugPanel {
  constructor(renderer, options = {}) {
    this.renderer = renderer;
    this.options = options;
    this.visible = false;
    this.installed = false;
    this.samples = [];
    this.recorded = null;
    this.calls = {};
    this.baseCalls = {};
    this.perRepaint = {};
    this.seenFrames = 0;
    this.last = performance.now();
    this.window = options.window || 120;
    this.raf = 0;
    this.build();
    if (options.hotkey !== false) {
      document.addEventListener('keydown', (event) => {
        if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === 'p') {
          event.preventDefault();
          this.toggle();
        }
      });
    }
  }

  // ---------------------------------------------------------------- DOM
  build() {
    const host = document.createElement('div');
    host.id = 'saltDebugPanel';
    host.style.cssText = [
      'position:fixed', 'right:14px', 'bottom:14px', 'z-index:99', 'width:330px',
      'background:rgba(8,25,29,.94)', 'color:#dceee9', 'border:1px solid #2b5a5c',
      'border-radius:10px', 'font:12px/1.55 Consolas,Menlo,monospace',
      'box-shadow:0 10px 30px rgba(0,0,0,.45)', 'padding:10px 12px 12px', 'display:none',
    ].join(';');
    host.innerHTML =
      '<div id="sdpBar" style="display:flex;align-items:center;gap:6px;cursor:move;margin-bottom:6px">' +
      '<strong style="font-size:12px;color:#8fdbb9;flex:1">SaltLRC 调试面板</strong>' +
      '<button id="sdpRecord" style="font:11px inherit;padding:2px 7px">录制</button>' +
      '<button id="sdpCopy" style="font:11px inherit;padding:2px 7px">复制报告</button>' +
      '<button id="sdpJson" style="font:11px inherit;padding:2px 7px">导出 JSON</button>' +
      '<button id="sdpClose" style="font:11px inherit;padding:2px 7px">×</button></div>' +
      '<div id="sdpBody"></div>' +
      '<canvas id="sdpGraph" width="304" height="42" style="display:block;margin-top:6px;background:#0d272c;border-radius:4px"></canvas>' +
      '<div style="color:#5f8a86;margin-top:4px">Ctrl+Shift+P 开关面板</div>';
    document.body.append(host);
    this.host = host;
    this.body = host.querySelector('#sdpBody');
    this.graph = host.querySelector('#sdpGraph');
    // An environment without canvas (jsdom in the checks, or a disabled canvas)
    // returns null here, and every later call would throw on it.
    this.graphCtx = this.graph.getContext('2d');
    host.querySelector('#sdpClose').onclick = () => this.toggle(false);
    host.querySelector('#sdpRecord').onclick = () => this.toggleRecord();
    host.querySelector('#sdpCopy').onclick = () => this.copyReport();
    host.querySelector('#sdpJson').onclick = () => this.exportJSON();
    this.draggable(host, host.querySelector('#sdpBar'));
  }

  draggable(host, handle) {
    let drag = null;
    handle.addEventListener('pointerdown', (event) => {
      if (event.target.tagName === 'BUTTON') return;
      const rect = host.getBoundingClientRect();
      drag = {x: event.clientX - rect.left, y: event.clientY - rect.top};
      handle.setPointerCapture(event.pointerId);
    });
    handle.addEventListener('pointermove', (event) => {
      if (!drag) return;
      host.style.left = (event.clientX - drag.x) + 'px';
      host.style.top = (event.clientY - drag.y) + 'px';
      host.style.right = 'auto'; host.style.bottom = 'auto';
    });
    handle.addEventListener('pointerup', () => { drag = null; });
  }

  // ------------------------------------------------------- instrumentation
  install() {
    if (this.installed) return;
    const ctx = this.renderer.layerCtx;
    if (!ctx) return;
    this.originals = {};
    for (const name of ['stroke', 'fill', 'fillRect', 'fillText', 'beginPath', 'clip', 'drawImage', 'measureText']) {
      if (typeof ctx[name] !== 'function') continue;
      const original = ctx[name].bind(ctx);
      this.originals[name] = original;
      this.calls[name] = 0;
      ctx[name] = (...args) => { this.calls[name]++; return original(...args); };
    }
    this.installed = true;
  }

  uninstall() {
    if (!this.installed) return;
    const ctx = this.renderer.layerCtx;
    for (const [name, original] of Object.entries(this.originals)) ctx[name] = original;
    this.installed = false;
  }

  // -------------------------------------------------------------- control
  toggle(force) {
    this.visible = force === undefined ? !this.visible : Boolean(force);
    this.host.style.display = this.visible ? 'block' : 'none';
    if (this.visible) { this.install(); this.last = performance.now(); this.loop(); }
    else { this.uninstall(); if (this.raf) cancelAnimationFrame(this.raf); this.raf = 0; }
  }

  toggleRecord() {
    if (!this.recorded) {
      this.recorded = {started: new Date().toISOString(), frames: [], repaints: []};
      this.host.querySelector('#sdpRecord').textContent = '停止';
      return;
    }
    this.host.querySelector('#sdpRecord').textContent = '录制';
    this.recorded.stopped = new Date().toISOString();
  }

  loop() {
    if (!this.visible) return;
    this.raf = requestAnimationFrame(() => { this.sample(); this.loop(); });
  }

  sample() {
    const now = performance.now();
    const frame = now - this.last;
    this.last = now;
    this.samples.push(frame);
    if (this.samples.length > 3600) this.samples.shift();
    const stats = this.renderer.stats || {};
    if (stats.frames !== this.seenFrames) {
      this.seenFrames = stats.frames;
      this.perRepaint = {};
      for (const name of Object.keys(this.calls)) {
        this.perRepaint[name] = this.calls[name] - (this.baseCalls[name] || 0);
        this.baseCalls[name] = this.calls[name];
      }
      this.layerMs = stats.layerMs;
      this.repaintAt = now;
    }
    if (this.recorded) {
      this.recorded.frames.push(Math.round(frame * 100) / 100);
      if (stats.frames !== this.seenLast) {
        this.recorded.repaints.push({t: Math.round(now - this.recordedStart), ...this.perRepaint, layerMs: stats.layerMs});
      }
      this.seenLast = stats.frames;
    }
    this.paint();
  }

  metrics() {
    const recent = this.samples.slice(-this.window);
    const sorted = [...recent].sort((a, b) => a - b);
    const pick = (q) => sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))] : 0;
    const mean = recent.length ? recent.reduce((a, b) => a + b, 0) / recent.length : 0;
    const renderer = this.renderer;
    const groups = renderer.doc?.groups || [];
    const width = renderer.canvas?.parentElement?.clientWidth || 0;
    const visible = groups.filter((g) => {
      const from = renderer.offset * 1000, to = from + width / renderer.pps * 1000;
      const start = (g.main.start ?? g.start);
      const end = (g.main.end ?? start + 2500);
      return end > from && start < to && g.main.text;
    }).length;
    return {
      fps: mean ? Math.round(1000 / mean * 10) / 10 : 0,
      frame: mean, p95: pick(.95), max: sorted[sorted.length - 1] || 0,
      dropped: recent.filter((v) => v > 16.7).length,
      stalled: recent.filter((v) => v > 33).length,
      layerMs: this.layerMs || 0,
      repaints: (renderer.stats || {}).layerPaints || 0,
      calls: this.perRepaint,
      width, height: renderer.canvas?.height,
      dpr: window.devicePixelRatio || 1,
      peaks: Object.values(renderer.peaks || {}).reduce((sum, p) => sum + (p.values?.length || 0), 0),
      visible, groups: groups.length,
      samples: recent.length,
    };
  }

  paint() {
    const m = this.metrics();
    const calls = m.calls || {};
    const line = (label, value, tone) =>
      `<div style="display:flex;gap:6px"><span style="color:#6f9b96;min-width:74px">${label}</span>` +
      `<span style="flex:1;color:${tone || '#dceee9'}">${value}</span></div>`;
    this.body.innerHTML = [
      line('FPS', m.fps.toFixed(1), m.fps >= 50 ? '#8fdbb9' : m.fps >= 30 ? '#efd296' : '#ff9d8a'),
      line('帧耗时', `${m.frame.toFixed(2)} ms`, m.frame <= 16.7 ? '#8fdbb9' : '#efd296'),
      line('p95 / 最大', `${m.p95.toFixed(1)} / ${m.max.toFixed(1)} ms`),
      line('掉帧', `>16.7ms ${m.dropped} · >33ms ${m.stalled}`, m.stalled ? '#ff9d8a' : '#dceee9'),
      line('静态层', `${m.layerMs.toFixed(2)} ms · 累计 ${m.repaints} 次`),
      line('绘制调用', `stroke ${calls.stroke || 0} · fill ${(calls.fill || 0) + (calls.fillRect || 0)} · ` +
                       `text ${calls.fillText || 0} · path ${calls.beginPath || 0}`, (calls.stroke || 0) > 30 ? '#ff9d8a' : '#8fdbb9'),
      line('画布', `${m.width} css · ${m.height} 设备px · dpr ${m.dpr}`),
      line('波形', `${m.peaks} 峰值点`),
      line('歌词', `可见 ${m.visible} / 共 ${m.groups} 行`),
      line('采样', `${m.samples} 帧窗口`),
    ].join('');
    this.sparkline(m);
  }

  sparkline(m) {
    const ctx = this.graphCtx;
    if (!ctx) return;              // no 2d context: skip the graph, keep the numbers
    const w = this.graph.width, h = this.graph.height;
    ctx.clearRect(0, 0, w, h);
    // 60 fps budget line
    ctx.strokeStyle = '#2f6a68';
    ctx.beginPath(); ctx.moveTo(0, h - 16.7 / 40 * h); ctx.lineTo(w, h - 16.7 / 40 * h); ctx.stroke();
    const data = this.samples.slice(-304);
    data.forEach((value, i) => {
      const bar = Math.min(1, value / 40) * h;
      ctx.fillStyle = value > 33 ? '#ff8a76' : value > 16.7 ? '#efd296' : '#5fc39c';
      ctx.fillRect(i, h - bar, 1, bar);
    });
    ctx.fillStyle = '#5f8a86';
    ctx.fillText('帧时间（红 >33ms / 黄 >16.7ms）', 4, 12);
  }

  report() {
    const m = this.metrics();
    const calls = Object.entries(m.calls || {}).map(([k, v]) => `${k}=${v}`).join(' ');
    const source = this.recorded?.frames?.length ? this.recorded.frames : this.samples.slice(-600);
    const sorted = [...source].sort((a, b) => a - b);
    const pick = (q) => sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))].toFixed(2) : '0';
    return [
      'SaltLRC 性能报告',
      `时间: ${new Date().toISOString()}`,
      `样本: ${source.length} 帧`,
      `FPS(均): ${m.fps.toFixed(1)}`,
      `帧耗时: avg ${pick(.5)} / p95 ${pick(.95)} / max ${pick(.999)} ms`,
      `掉帧: >16.7ms ${source.filter((v) => v > 16.7).length} · >33ms ${source.filter((v) => v > 33).length}`,
      `静态层重绘: ${m.layerMs.toFixed(2)} ms (最近一次) / 累计 ${m.repaints} 次`,
      `绘制调用(最近一次重绘): ${calls}`,
      `画布: ${m.width} css px, 设备高 ${m.height}px, devicePixelRatio ${m.dpr}`,
      `波形峰值点: ${m.peaks}`,
      `歌词: 可见 ${m.visible} 行 / 共 ${m.groups} 行`,
      `缩放: ${this.renderer.pps} px/s · 视窗起点 ${this.renderer.offset.toFixed(2)} s`,
      `音频: ${this.options.audio?.() || 'n/a'}`,
    ].join('\n');
  }

  copyReport() {
    const text = this.report();
    navigator.clipboard?.writeText(text).then(
      () => this.flash('报告已复制'),
      () => this.flash('复制失败，请手动选择'));
    return text;
  }

  flash(message) {
    const note = document.createElement('div');
    note.textContent = message;
    note.style.cssText = 'color:#8fdbb9;margin-top:4px';
    this.host.append(note);
    setTimeout(() => note.remove(), 1600);
  }

  exportJSON() {
    const payload = {
      report: this.report(),
      metrics: this.metrics(),
      samples: (this.recorded?.frames?.length ? this.recorded.frames : this.samples).slice(-3600),
      repaints: this.recorded?.repaints?.slice(-200) || [],
      environment: {userAgent: navigator.userAgent, dpr: window.devicePixelRatio || 1,
                    hardwareConcurrency: navigator.hardwareConcurrency || 0},
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 1)], {type: 'application/json'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'saltlrc-perf-' + Date.now() + '.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    return payload;
  }
}

if (typeof window !== 'undefined') window.SaltDebugPanel = SaltDebugPanel;
