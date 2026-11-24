"use strict";
// Both canvases are fully opaque (they paint their own background), so the
// compositor can skip blending them against the page behind. This is the one
// genuinely GPU-side win available here, and it is about how the finished
// canvas is composited rather than how it is drawn.
//
// Deliberately NOT set:
//   desynchronized  would save about a frame of input latency, but it lets the
//                 canvas be composited on its own path, which can break stacking
//                 against the DOM overlays drawn over it (preview bar, karaoke
//                 overlay, match panel). Not worth a visual regression for 16ms.
//
// And deliberately not rewritten in WebGL. The cost was never fill rate — the
// canvas is 900x292 — it was submitting ~2100 drawing calls per interaction.
// Fixing the work took a hover from 2176 calls to ~28; a GPU API would only make
// each of those 2100 calls faster, not fewer.
const OPAQUE = {alpha: false};

// The waveform's own background. Used both to paint the static layer and to
// prime the canvas, so an uncovered sliver can never be a different colour.
// The waveform's own background, read per draw rather than once at load: the
// value lives in style.css and follows the theme.
function canvasBackground() { return Theme.token('--wave-bg'); }

// Independently implemented, viewport-sized renderer. Long songs do not create giant canvases.
//
// Performance model: everything except the playhead is static. Background,
// waveform, grid and lyric tracks are painted once into an offscreen layer and
// only repainted when the viewport, zoom, audio or document actually change.
// Each frame then costs one blit plus one playhead line, which is what makes a
// 60fps playhead possible (audio "timeupdate" only fires ~4/s).
//
// Three things dominate a full repaint, so all three are avoided:
//   * per-group canvas clip()       -> measured text truncation;
//   * one stroked segment per pixel -> a single filled envelope;
//   * one stroke() per word tick    -> all ticks batched into one path per track.
// The tick batching matters most: a stroke() per word boundary would submit
// ~370 separate two-point strokes per repaint at 20 px/s.
class SaltWaveform {
  constructor(canvas, options) {
    this.canvas = canvas;
    this.options = options;
    this.duration = 0;
    this.peaks = [];
    this.pps = 90;
    this.offset = 0;
    this.time = 0;
    this.hits = [];
    this.drag = null;
    this.highlight = -1;
    this.fitLines = null;       // 贴唱预览（见 setFit）：只在覆盖层画，不进内容层
    this.hover = null;          // table-row hover (see setHover) — NOT the cursor
    this.pointer = null;        // the tick/block under the cursor
    this.pointerKey = '';
    this.selected = null;       // the tick clicked last (keyboard nudging acts on it)
    this.doc = {groups: []};
    this.docVersion = 0;
    this.dirty = true;
    this.frame = 0;
    this.key = '';
    this.textWidths = new Map();
    this.tops = null;
    this.bottoms = null;
    this.stats = {layerMs: 0, frameMs: 0, layerPaints: 0, frames: 0, blits: 0};
    this.followSuspended = false;
    // Panning is a pure translation, so the static layer is painted at a
    // coarse anchor offset and blitted at (anchor - offset) instead of being
    // rebuilt for every scrolled pixel. See paintLayerLayer().
    this.layerAnchor = null;
    this.layerSpan = 0;
    this.layerWidth = 0;
    this.layerHeight = 0;
    this.layer = document.createElement('canvas');
    this.layerCtx = this.layer.getContext('2d');
    // Observe the pane that owns both the waveform and the strip: a width
    // change must invalidate both, and observing the canvas' immediate parent
    // missed layouts where only the pane was re-columned (the splitter drag).
    new ResizeObserver(() => this.invalidate()).observe(canvas.parentElement?.parentElement || canvas.parentElement);
    canvas.addEventListener('pointerdown', e => this.down(e));
    canvas.addEventListener('pointermove', e => this.move(e));
    canvas.addEventListener('pointerleave', () => {
      if (this.drag) return;
      this.pointer = null;
      this.pointerKey = '';
      this.options.pointer?.(null);       // hide the readout with the cursor
      this.draw();
    });
    canvas.addEventListener('pointerup', e => this.up(e));
    canvas.addEventListener('pointercancel', () => { this.drag = null; this.draw(); });
    canvas.addEventListener('dblclick', e => {
      // A tick is checked first: it sits ON the block edge, where blockAt() can
      // miss by a pixel or two — and a tick is exactly what you aim at when you
      // want the word-timing editor.
      const rect = canvas.getBoundingClientRect();
      const hit = this.hitAt(e.clientX - rect.left, e.clientY - rect.top);
      const target = hit || this.blockAt(e);
      if (target) this.options.edit?.(target.index, target.field, hit ? 'words' : 'text');
    });
    canvas.addEventListener('wheel', e => {
      e.preventDefault();
      if (e.ctrlKey) {
        this.pps = SaltWaveform.clampPps(this.pps * (e.deltaY < 0 ? 1.2 : 1 / 1.2));
        this.options.zoom?.(Math.round(this.pps));
        this.draw();
        return;
      }
      // The wheel slides the viewport along the song (no modifier): scrolling
      // is the primary way to move through the timeline here.
      const delta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      this.offset = Math.max(0, Math.min(this.maxOffset(), this.offset + delta / this.pps));
      this.suspendFollow();
      this.options.pan?.(this.offset);
      this.draw();
    }, {passive: false});
  }
  // THE width authority. Every geometric decision — how many seconds fit on
  // screen, how far the viewport may scroll, where the playhead lands, where
  // the strip frame sits and how wide it is — must read the layout from here.
  //
  // Why the parent and not the canvas: #waveCanvas deliberately has no CSS
  // width (it is sized from JS so the DPR-scaled backing store stays exact),
  // which means its own clientWidth is only meaningful *after* paint() has
  // written style.width. Before that first write the element falls back to the
  // `width` ATTRIBUTE — 300px — and every consumer silently computes geometry
  // for a 300px viewport. That is what made the strip frame change length for
  // no reason: a repaint that read the canvas before its style was applied
  // divided by 300 instead of the real pane width (off by ~4x in the report).
  // The parent's clientWidth is the one number that is always already correct.
  measureWidth() {
    const parent = this.canvas?.parentElement;
    const px = parent ? parent.clientWidth : 0;
    if (px > 0) return px;
    // No layout yet (detached node, hidden pane): fall back to a sane width
    // rather than 0, which would make maxOffset() collapse and freeze the strip.
    const fallback = this.canvas?.clientWidth || 0;
    return fallback > 0 ? fallback : 200;
  }
  _fallbackDuration() {
    // No decoded audio: derive the song length from the lyric itself, the end
    // of the last line included (not just its start).
    let last = 10;
    for (const g of this.doc.groups) {
      last = Math.max(last, (g.main.end ?? g.start + 5000) / 1000, g.start / 1000 + 5);
    }
    return last;
  }
  // The width the canvas is actually LAID OUT at: the 200px floor paint() uses.
  // ONE authority — paint() and every viewport calculation share this number,
  // otherwise the coverage check measures a width the paint did not use.
  canvasWidth() {
    return Math.max(200, this.measureWidth());
  }
  // Seconds currently visible in the waveform. One authority, so the strip
  // frame, the pan limit and the follow logic can never disagree.
  viewportSeconds() {
    return this.canvasWidth() / this.pps;
  }
  // Single source of truth for "how far right can the viewport go". Every pan
  // path (strip drag, wheel, setPan, follow) must use this — computing it
  // inline with `duration || 0` is what froze the strip whenever no audio was
  // loaded (max was always 0, so the frame snapped back instantly).
  maxOffset() {
    return Math.max(0, this.songSeconds() - this.viewportSeconds());
  }
  // Total length of the song being edited: the decoded audio when there is
  // any, otherwise the lyric's own extent (last line's *end*, not its start).
  songSeconds() {
    return this.duration || this._fallbackDuration();
  }
  // Width the strip is drawn at. Keeps the hit-test mapping (timeAt) and the
  // frame geometry on the same number, so clicking a spot on the strip lands
  // exactly where the frame says it should.
  stripWidth() {
    const canvas = this.overviewCanvas;
    const px = canvas?.parentElement?.clientWidth || 0;
    if (px > 0) return Math.max(120, px);
    const rect = canvas?.getBoundingClientRect?.();
    return rect?.width ? Math.max(120, rect.width) : this.measureWidth();
  }
  // Full-song strip: the whole song at a glance with the
  // playhead and the visible viewport. Drag the frame to slide the viewport,
  // press+drag anywhere else to scrub the play position.
  attachOverview(canvas) {
    this.overviewCanvas = canvas;
    const state = this.overviewState = {mode: null, grabOffset: 0, grabTime: 0};
    // `getBoundingClientRect()` forces a layout flush, so it must not be read
    // once per pointermove on a hot drag path. The strip cannot move while the
    // pointer is captured, so the rect is measured at drag start and reused;
    // outside a drag it is read fresh so a scroll or resize is still picked up.
    let rect = null;
    const rectOf = () => {
      if (!rect) rect = canvas.getBoundingClientRect();
      return rect;
    };
    const timeAt = e => {
      const r = rectOf();
      const duration = this.songSeconds();
      return Math.max(0, Math.min(duration, (e.clientX - r.left) / Math.max(1, this.stripWidth()) * duration));
    };
    const seekTo = time => {
      this.resumeFollow();
      this.options.seek?.(time);   // strip time is already in seconds
      const viewport = this.viewportSeconds();
      const max = this.maxOffset();
      if (time < this.offset || time > this.offset + viewport) {
        this.offset = Math.max(0, Math.min(time - viewport / 2, max));
      }
      this.draw();
    };
    canvas.addEventListener('pointerdown', e => {
      const duration = this.songSeconds();
      if (!duration) return;
      // Capture can be rejected (e.g. pointer already released); that must not
      // kill the whole drag — pan/scrub still works without it.
      try { canvas.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      rect = canvas.getBoundingClientRect();   // measure once, reuse while dragging
      const time = timeAt(e);
      const viewport = this.viewportSeconds();
      if (time >= this.offset && time <= this.offset + viewport) {
        state.mode = 'pan';
        state.grabOffset = this.offset;
        state.grabTime = time;
        canvas.style.cursor = 'grabbing';
        // Widen the pre-rendered margin for the duration of the drag so a long
        // pan does not keep rebuilding the static layer (see panSpan()).
        this.drag = {kind: 'pan', time: time * 1000};
        this.reanchorForDrag();
      } else {
        state.mode = 'seek';
        seekTo(time);
      }
    });
    canvas.addEventListener('pointermove', e => {
      if (!state.mode) {
        // Hover affordance: grab inside the viewport frame, pointer elsewhere.
        const viewport = this.viewportSeconds();
        const time = timeAt(e);
        canvas.style.cursor = (time >= this.offset && time <= this.offset + viewport) ? 'grab' : 'pointer';
        return;
      }
      const time = timeAt(e);
      if (state.mode === 'seek') { seekTo(time); return; }
      const max = this.maxOffset();
      this.offset = Math.max(0, Math.min(max, state.grabOffset + (time - state.grabTime)));
      this.suspendFollow();
      this.options.pan?.(this.offset);
      this.draw();
    });
    const stop = () => {
      state.mode = null;
      canvas.style.cursor = '';
      rect = null;                       // next gesture re-measures
      if (this.drag?.kind === 'pan') { this.drag = null; this.reanchorForDrag(); }
    };
    canvas.addEventListener('pointerup', stop);
    canvas.addEventListener('pointercancel', stop);
  }
  // The strip has the same shape as the main canvas: a static background (the
  // ruler and the lyric bars, which depend only on the song and the document)
  // plus a small dynamic overlay (the viewport frame and the playhead). It is
  // cached for the same reason — a drag repaints the strip every frame, and
  // re-laying-out every lyric bar each time was ~360 calls per frame, more than
  // the waveform itself. The background only changes when the song length, the
  // document or the strip width does.
  stripKeyMatches(w, h, ratio) {
    return this.stripW === w && this.stripH === h &&
           this.stripRatio === ratio && this.stripDocVersion === this.docVersion &&
           this.stripSongSeconds === this.songSeconds() &&
           this.stripGroups === this.doc.groups;
  }
  paintStripBackground(w, h, ratio, width, height) {
    const bg = this.stripLayer || (this.stripLayer = document.createElement('canvas'));
    if (bg.width !== w || bg.height !== h) { bg.width = w; bg.height = h; }
    const ctx = bg.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const duration = this.songSeconds() || 1;
    ctx.fillStyle = Theme.token('--wave-track-bg');
    ctx.fillRect(0, 0, width, height);
    const xAt = seconds => seconds / duration * width;
    // Time ruler. The step adapts to the song length so ticks never crowd, and
    // a label is only drawn when it has room — the gate is expressed in the
    // same px-per-second the ruler itself uses, so it stays correct at any
    // strip width instead of only at the width it was tuned on.
    ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.font = '9px Segoe UI, sans-serif';
    const tickStep = duration > 900 ? 60 : duration > 300 ? 30 : 15;
    const labelEvery = tickStep * 2;
    const pxPerSecond = width / duration;
    const labelFits = pxPerSecond * labelEvery > 34;
    for (let s = 0; s <= duration; s += tickStep) {
      const x = Math.round(xAt(s)) + .5;
      ctx.strokeStyle = Theme.token('--wave-axis');
      ctx.beginPath(); ctx.moveTo(x, height - 9); ctx.lineTo(x, height); ctx.stroke();
      if (labelFits && s % labelEvery === 0) {
        ctx.fillStyle = Theme.token('--wave-axis-ink');
        ctx.fillText(s + 's', x + 3, height - 10);
      }
    }
    for (const [field, y, color] of [['main', 7, Theme.token('--wave-env-main')], ['translation', 22, Theme.token('--wave-env-trans')]]) {
      ctx.fillStyle = color;
      const groups = this.doc.groups;
      for (let index = 0; index < groups.length; index++) {
        const g = groups[index], track = g[field];
        if (!track.text) continue;
        const startMs = track.start ?? g.start;
        // Same rule as the full waveform, including the fallback: a line with no
        // 句尾 runs to the NEXT line, not to a fixed 2.5s. The fixed default made
        // translation blocks overlap whenever lines were closer than 2.5s apart,
        // so the row read as one continuous band — "翻译轨道没修".
        const endMs = track.end ?? (groups[index + 1] ? groups[index + 1].start : startMs + 2500);
        const left = xAt(startMs / 1000);
        const span = xAt(Math.min(endMs / 1000, duration)) - left;
        // Two blocks must never abut — but a PROPORTIONAL seam is wrong here.
        // A whole song fits in a few hundred pixels, so a 3s line is ~9px and
        // "15% of the block" is 1px: a field of hairlines instead of separate
        // blocks. The seam is a small absolute width, skipped entirely when the
        // block is too thin to pay for it (a 3px block minus 2px is a sliver,
        // not a block).
        const seam = span >= 5 ? Math.max(2, Math.min(4, Math.round(span * 0.25))) : 0;
        ctx.fillRect(left, y, Math.max(1.5, span - seam), 12);
      }
    }
    this.stripW = w; this.stripH = h; this.stripRatio = ratio;
    this.stripDocVersion = this.docVersion;
    this.stripSongSeconds = this.songSeconds();
    this.stripGroups = this.doc.groups;
  }
  drawOverview() {
    const canvas = this.overviewCanvas;
    if (!canvas) return;
    const height = 44;
    // One width for the strip: the same number timeAt() divides by and the same
    // number frameW is expressed as a proportion of.
    const width = this.stripWidth();
    const ratio = window.devicePixelRatio || 1;
    const w = Math.round(width * ratio), h = Math.round(height * ratio);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w; canvas.height = h;
      canvas.style.width = width + 'px'; canvas.style.height = height + 'px';
      this.stripW = null;                 // new backing store: background is stale
    }
    // The static background is only rebuilt when it can actually differ; a drag
    // reuses it and pays for the frame overlay only.
    if (!this.stripLayer || !this.stripKeyMatches(w, h, ratio)) {
      this.paintStripBackground(w, h, ratio, width, height);
    }
    // `alpha: false` lets the compositor skip blending this opaque canvas
    // against the page behind it. It only takes effect on the FIRST getContext
    // for a canvas; later calls return the same context and ignore attributes,
    // so passing it every time is how the first call is guaranteed to carry it.
    // The context is deliberately not cached on the instance: getContext already
    // returns the same object, so caching saves nothing, and it would stop tests
    // from intercepting the drawing.
    const ctx = canvas.getContext('2d', OPAQUE);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(this.stripLayer, 0, 0);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const duration = this.songSeconds() || 1;
    // ---- viewport frame -------------------------------------------------
    // Geometry, stated once so the two invariants can be checked by eye:
    //
    //   len(song)      = songSeconds()      seconds the strip represents
    //   len(viewport)  = viewportSeconds()  seconds the waveform shows
    //   len(strip)     = width              px the strip is drawn at
    //
    //   INVARIANT 1 (the user's request): frameW depends ONLY on the zoom and
    //     the song length — never on this.offset. frameMs/spanMs is a ratio of
    //     two song lengths, so scrolling cannot change it.
    //   INVARIANT 2: scrolling maps the *slidable* range onto the *travellable*
    //     distance. The slider can move maxOffset() seconds, and the frame can
    //     travel (width - frameW) px, so position = offset/maxOffset * travel.
    //     Deriving it from (span - frameMs) — the old form — silently disagreed
    //     with maxOffset() whenever frameW hit the minimum-width clamp.
    const spanMs = this.songSeconds() * 1000 || 1;
    const frameMs = this.viewportSeconds() * 1000;
    // A viewport that already covers the whole song shows as the full bar.
    const frameW = Math.min(width, (frameMs / spanMs) * width);
    // Minimum is applied for grab-ability, but it is a *pixel* floor on an
    // already-proportional result: it does not feed back into the value the
    // position is derived from, so scrolling cannot resize the frame.
    const grip = 28;
    const fw = Math.max(grip, frameW);
    const travelPx = Math.max(0, width - fw);
    const maxOffset = this.maxOffset();
    const at = maxOffset > 0 ? Math.max(0, Math.min(1, this.offset / maxOffset)) : 0;
    const fx = travelPx * at;
    ctx.fillStyle = Theme.token('--wave-pick-weak');
    ctx.fillRect(fx, 1, fw, height - 2);
    ctx.strokeStyle = Theme.token('--ink-highlight');
    ctx.lineWidth = 1.5;
    ctx.strokeRect(fx + .5, 1.5, fw - 1, height - 3);
    ctx.fillStyle = Theme.token('--ink-highlight');
    ctx.fillRect(fx + 2.5, height / 2 - 6, 2, 12);
    ctx.fillRect(fx + fw - 4.5, height / 2 - 6, 2, 12);
    // Playhead: white line with a small downward marker so it is findable fast.
    ctx.lineWidth = 1;
    const playhead = Math.min(width, Math.max(0, this.time / duration * width));
    ctx.strokeStyle = Theme.token('--wave-cursor');
    ctx.beginPath(); ctx.moveTo(playhead, 0); ctx.lineTo(playhead, height); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(playhead - 4, 0); ctx.lineTo(playhead + 4, 0); ctx.lineTo(playhead, 6);
    ctx.closePath(); ctx.fillStyle = Theme.token('--wave-cursor'); ctx.fill();
  }
  load(buffer) {
    this.duration = buffer?.duration || 0;
    this.offset = 0;
    this.resumeFollow();
    this.peaks = [];
    if (buffer) {
      for (let ch = 0; ch < Math.min(2, buffer.numberOfChannels); ch++) {
        const data = buffer.getChannelData(ch);
        // Very long tracks are subsampled: the display is ~1px per bucket anyway.
        const step = data.length > 40e6 ? 4 : data.length > 20e6 ? 2 : 1;
        const stride = Math.max(1, Math.floor(buffer.sampleRate / 100));
        const values = new Float32Array(Math.ceil(data.length / stride));
        for (let i = 0; i < values.length; i++) {
          let peak = 0;
          const from = i * stride, to = Math.min(data.length, from + stride);
          for (let j = from; j < to; j += step) { const v = Math.abs(data[j]); if (v > peak) peak = v; }
          values[i] = peak;
        }
        this.peaks.push({values, rate: buffer.sampleRate / stride});
      }
    }
    this.invalidate();
  }
  setDocument(doc) {
    this.doc = doc || {groups: []};
    // A highlight index belongs to the document it was measured against: after
    // a swap (or a truncation) the old number may point at a completely
    // different line, so neither writer may keep it. hover is transient and
    // self-healing, but the playback index has to be reset explicitly or the
    // outline sticks to whatever line happens to occupy that slot now.
    this.highlight = -1;
    this.hover = null;
    this.docVersion++;
    this.invalidate();
  }
  // Row hover / playback highlight. Lives in the static layer, so it costs one
  // repaint when the line changes rather than one per frame.
  //
  // There are two independent writers here: playback (preview(), once per row
  // change) and the table's pointer hover. They need separate fields: preview()
  // only calls setHighlight when the *row* changes, so with one shared field a
  // hover that lands mid-row wins, after which the outline is drawn for the
  // hovered row while preview() believes the playing row is already set — frozen
  // until the next row boundary (or a pause, which re-runs preview()). Separate
  // slots mean a repaint always resolves the correct one without either writer
  // having to restore the other.
  // These two are OVERLAY state, not layer state: changing either must not
  // rebuild the static layer (that would repaint the whole 900px waveform and
  // both lyric tracks on every hovered or clicked row). The outline is a handful
  // of strokes, so it belongs with the playhead: drawn on top after the blit,
  // every frame for free.
  setHighlight(index) {
    if (this.highlight === index) return;
    this.highlight = index;
    this.draw();
  }
  /** 贴唱预览：把"贴后会落在哪"叠在波形上（**不动内容层**）。
   *
   * 传的是 `[{index, start, end}]`（只传**会动的**那些行；没动的本来就画对了），
   * 或者 `null` 关掉预览。
   *
   * **谁负责清**：只有 `studio.js` 的 `applyFitPreview()`（它也是唯一的写者）。
   * 这里**刻意不在 `setDocument()` 里清** —— 那个函数每渲染一次就被调用一次，
   * 而贴唱预览要一直留在波形上直到用户应用或关掉预览；换歌时由
   * `applyFitPreview()` 按新歌重新算（新歌没有结果就是 null）。
   *
   * 画在覆盖层而不是内容层：这一层随**视窗位置**变（每帧重画，几条虚线而已），
   * 而内容层是"只在内容变时重画"的缓存 —— 判据是"它会随什么变化？"（见分层规则）。
   * 另外它**不注册命中区**：预览不是可拖的把手，免得用户以为能直接拖它。
   */
  setFit(lines) {
    const next = Array.isArray(lines) && lines.length ? lines : null;
    const same = JSON.stringify(next) === JSON.stringify(this.fitLines || null);
    this.fitLines = next;
    if (!same) this.draw();
  }
  // Transient pointer highlight; falls back to the playback row when cleared.
  setHover(index) {
    const next = index == null ? null : index;
    if (this.hover === next) return;
    this.hover = next;
    this.draw();
  }
  // The index actually painted. A stale hover (row removed/reordered under the
  // pointer) must not leave the outline pointing at a line that does not match
  // the table, so an out-of-range hover falls back to playback.
  highlightIndex() {
    const hover = this.hover;
    if (hover != null && this.doc?.groups?.[hover]) return hover;
    return this.highlight;
  }
  // Scroll the viewport so a line is fully visible (used when picking a table row).
  focusLine(index) {
    const groups = this.doc.groups;
    if (!(index >= 0 && index < groups.length)) return;
    this.resumeFollow();
    const width = this.canvasWidth();
    const track = groups[index].main;
    const start = (track.start ?? groups[index].start) / 1000;
    const end = (track.end ?? (track.start ?? groups[index].start) + 2500) / 1000;
    if (start < this.offset || end > this.offset + width / this.pps) {
      this.offset = Math.max(0, start - width / this.pps * .25);
      this.options.pan?.(this.offset);
    }
    this.draw();
  }
  setTime(time, follow) {
    this.time = time;
    const width = this.canvasWidth();
    // Manual panning (strip drag, wheel) suspends auto-follow: preview() runs
    // every frame during playback, so without this flag any pan that puts the
    // playhead off-screen is snapped back within one frame — the "slider is
    // dead" bug. Follow resumes on seek / locate / replay.
    if (follow && !this.followSuspended &&
        (time < this.offset || time > this.offset + width / this.pps * .9)) {
      this.offset = Math.max(0, Math.min(this.maxOffset(), time - width / this.pps * .15));
      this.options.pan?.(this.offset);
    }
    this.draw();
  }
  suspendFollow() {
    if (!this.followSuspended) {
      this.followSuspended = true;
      this.options.followChanged?.(false);
    }
  }
  resumeFollow() {
    if (this.followSuspended) {
      this.followSuspended = false;
      this.options.followChanged?.(true);
    }
  }
  eventTime(e) {
    const rect = this.canvas.getBoundingClientRect();
    // Same song length every other path uses. The old inline expression only
    // looked at `g.start + 5` and ignored each line's *end*, so clicking near
    // the tail of a lyric without audio clamped to a shorter song than the
    // strip displayed.
    const duration = this.songSeconds();
    return Math.round(Math.max(0, Math.min(duration, this.offset + (e.clientX - rect.left) / this.pps)) * 1000);
  }
  // Layer-space -> canvas-space conversion for hit testing.
  //
  // `hits[].x` is recorded while painting the static layer, which is anchored
  // at `layerAnchor` rather than at `offset` — that is what lets panning blit
  // instead of repaint. Everything that compares a pointer position against a
  // hit shifts by the same amount the blit uses. Rebuilding the array on every
  // frame would reinstate exactly the per-frame cost this design removes, so the
  // offset is applied at lookup time instead.
  hitShift() {
    if (this.layerAnchor == null) return 0;
    return (this.layerAnchor - this.offset) * this.pps;
  }
  down(e) {
    const rect = this.canvas.getBoundingClientRect(), x = e.clientX - rect.left, y = e.clientY - rect.top;
    const hit = this.hitAt(x, y);
    // A drag asks the layer for a wider margin (see panSpan()), so start it with
    // a fresh layer. Without this the first drag would keep the narrow
    // non-drag margin and hit its edge almost immediately.
    if (hit) {
      const time = this.eventTime(e);
      // Alt is the "push the followers too" gesture: with monotonic timings a
      // word can never pass its neighbour, so opening a gap otherwise means
      // shoving the words one at a time from the last one backwards.
      this.drag = {...hit, grabTime: time, delta: 0, time, ripple: !!e.altKey};
      // Selecting is what arms the keyboard nudge: you click a tick, then use
      // ←/→ to place it exactly. Without a selection nothing is nudged, so the
      // arrow keys keep their usual meaning everywhere else.
      this.selected = this.drag;
      this.options.select?.(this.pointerInfo(x, y));
      this.canvas.setPointerCapture(e.pointerId);
      this.reanchorForDrag();
      return;
    }
    // Grabbing anywhere inside a block moves the whole line; the ticks stay
    // first-priority so fine adjustments keep working at the edges.
    const block = this.blockAt(e);
    if (block) {
      const time = this.eventTime(e);
      this.drag = {kind: 'block', ...block, grabTime: time, time};
      this.canvas.setPointerCapture(e.pointerId);
      this.canvas.style.cursor = 'grabbing';
    } else {
      if (this.selected) { this.selected = null; this.options.select?.(null); this.draw(); }
      this.options.seek(this.eventTime(e) / 1000);
    }
  }
  // The pre-rendered margin depends on whether a drag is in progress, so the
  // layer is rebuilt once at each end of a drag rather than every frame during
  // it. Rebuilding here is one paint, not a per-frame cost.
  reanchorForDrag() {
    this.layerAnchor = null;
    this.draw();
  }
  move(e) {
    if (!this.drag) {
      // Let the cursor advertise what is editable: ticks resize, blocks move.
      const rect = this.canvas.getBoundingClientRect(), x = e.clientX - rect.left, y = e.clientY - rect.top;
      const hit = this.hitAt(x, y);
      const block = hit ? null : this.blockAt(e);
      const over = hit ? 'col-resize' : (block ? 'grab' : 'crosshair');
      if (this.cursor !== over) { this.cursor = over; this.canvas.style.cursor = over; }
      // Hover feedback: which word is under the cursor, so the tip can name it
      // and the overlay can outline it. Drawn in the overlay, so this costs a
      // few strokes per hover change rather than a layer rebuild.
      const key = hit ? `${hit.index}:${hit.field}:${hit.kind}:${hit.word ?? ''}`
                      : block ? `${block.index}:${block.field}:block` : '';
      if (this.pointerKey !== key) {
        this.pointerKey = key;
        this.pointer = hit || (block ? {...block, kind: 'block'} : null);
        this.options.pointer?.(this.pointerInfo(x, y));
        this.draw();
      }
      return;
    }
    if (this.drag.kind === 'block') this.canvas.style.cursor = 'grabbing';
    // The modifier can be pressed or released mid-drag; the readout explains
    // which mode is active, so it must follow the keyboard, not just the grab.
    if ('altKey' in e) this.drag.ripple = !!e.altKey;
    this.drag.time = this.eventTime(e);
    // Keep the readout live while dragging: the number is the whole point of
    // dragging rather than typing.
    this.options.pointer?.(this.dragInfo());
    this.draw();
  }
  // The tick grab zone, in CSS pixels either side of the line.
  //
  // One constant, shared by BOTH the hit test and the drawing: the hovered tick
  // is drawn inside exactly this zone, so the grabbable area matches the visible
  // one. 8px covers a 1px line plus the jitter of a mouse or a trackpad.
  static TICK_GRAB = 8;
  // 刻度线的粗细：句首/句尾比逐字边界更重。
  // 三样东西都可能落在同一瞬间（某个字的起点、上一行的句尾、下一行的句首），
  // 全用 1px 就分不出"我抓的会是哪一条"。
  static TICK_WIDTH_LINE = 2;
  static TICK_WIDTH_WORD = 1;
  // 两条刻度落在同一像素时谁先被抓：行的边界优先于逐字。
  static TICK_RANK = {end: 0, start: 1, word: 2};
  // 音符条（字格）的高度。格子要有厚度，像钢琴窗里的音符，
  // 而不是一根条；几何断言也读这一个值，不另写一份数字。
  static CELL_HEIGHT = 38;

  // The zoom range is owned HERE, where pps actually lives. A second copy in the
  // wheel handler or in studio.js setZoom lets the slider and the wheel disagree
  // about the ends.
  static MIN_PPS = 20;
  static MAX_PPS = 400;

  static clampPps(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return SaltWaveform.MIN_PPS;
    return Math.max(SaltWaveform.MIN_PPS, Math.min(SaltWaveform.MAX_PPS, Math.round(number)));
  }

  // 0–100 ↔ pps, so the slider can read 0 at the far left and 100 at the far
  // right while the number box keeps the unit that actually means something.
  static percentForPps(pps) {
    const span = SaltWaveform.MAX_PPS - SaltWaveform.MIN_PPS;
    return Math.round((SaltWaveform.clampPps(pps) - SaltWaveform.MIN_PPS) / span * 100);
  }

  static ppsForPercent(percent) {
    const span = SaltWaveform.MAX_PPS - SaltWaveform.MIN_PPS;
    const bounded = Math.max(0, Math.min(100, Number(percent) || 0));
    return SaltWaveform.clampPps(SaltWaveform.MIN_PPS + span * bounded / 100);
  }

  hitAt(x, y) {
    const shift = this.hitShift();
    const tolerance = SaltWaveform.TICK_GRAB;
    // 取**最近**的一条，不是"最后画的那条"。
    //
    // 三条刻度经常挤在同一两个像素里（上一行的句尾、下一行的句首、某个字的起点）。
    // 原来返回的是遍历里最后命中的那条 —— 也就是"画得最晚的"，与指针离哪条更近无关，
    // 于是同一个位置点两次可能抓到不同的东西。现在按距离取，距离相差不到 1px 时
    // 按类型定序（行的句尾 > 句首 > 逐字），保证同一个位置永远给同一条。
    let best = null, bestDistance = Infinity, bestRank = Infinity;
    for (const hit of this.hits) {
      const distance = Math.abs(hit.x + shift - x);
      if (distance > tolerance) continue;
      if (y < hit.y || y > hit.y + hit.height) continue;
      const rank = SaltWaveform.TICK_RANK[hit.kind];
      const wins = !best || distance < bestDistance - 1
        || (distance <= bestDistance + 1 && rank < bestRank);
      if (wins) { best = hit; bestDistance = distance; bestRank = rank; }
    }
    return best;
  }

  /**
   * What to show for a point: the word under it, its index and its time.
   *
   * Returned as data (not drawn) so the page can render a DOM tooltip — easier to
   * style and to keep readable than canvas text, and it can overflow the canvas.
   */
  pointerInfo(x, y) {
    const hit = this.pointer;
    if (!hit) return null;
    const group = this.doc.groups[hit.index];
    if (!group) return null;
    const track = group[hit.field];
    const total = track.words.length;
    const start = this.timeAt(x);
    if (hit.kind === 'word') {
      const word = track.words[hit.word];
      return {x, y, kind: 'word', field: hit.field, index: hit.index, position: hit.word + 1,
              total, text: word?.text ?? '', time: word?.start ?? null};
    }
    if (hit.kind === 'start') {
      return {x, y, kind: 'start', field: hit.field, index: hit.index, position: 1, total,
              text: track.text, time: this.trackStartSafe(group, hit.field), hint: '句首'};
    }
    // The end handle, or the block body.
    return {x, y, kind: hit.kind === 'end' ? 'end' : 'block', field: hit.field, index: hit.index,
            position: 0, total, text: track.text, time: start,
            hint: hit.kind === 'end' ? '句尾' : '整句'};
  }

  trackStartSafe(group, field) {
    const track = group[field];
    return track?.start ?? group?.start ?? 0;
  }

  /**
   * What to show while a drag is in progress: the same information as the hover
   * tip, plus how far the value has moved from where it was grabbed.
   *
   * The feedback carries the absolute time, the word name and the tick index, so
   * placing a syllable does not depend on guessing which one is held.
   */
  dragInfo(drag = this.drag) {
    if (!drag) return null;
    const group = this.doc.groups[drag.index];
    const track = group && group[drag.field];
    const delta = Math.round(drag.time - drag.grabTime);
    if (drag.kind === 'block') {
      return {kind: 'block', field: drag.field, index: drag.index, text: track?.text ?? '',
              time: drag.time, delta, hint: '整句'};
    }
    if (drag.kind === 'word') {
      const word = track?.words?.[drag.word];
      return {kind: 'word', field: drag.field, index: drag.index, text: word?.text ?? '',
              position: drag.word + 1, total: track?.words?.length ?? 0,
              time: drag.time, delta, ripple: !!drag.ripple,
              followers: Math.max(0, (track?.words?.length ?? 0) - (drag.word + 1))};
    }
    const total = track?.words?.length ?? 0;
    return {kind: drag.kind, field: drag.field, index: drag.index, text: track?.text ?? '',
            position: drag.kind === 'start' ? 1 : 0, total, time: drag.time, delta,
            hint: drag.kind === 'start' ? '句首' : '句尾'};
  }

  /** Time at a canvas x, in milliseconds. */
  timeAt(x) {
    return Math.round((x / this.pps + this.offset) * 1000);
  }

  /**
   * The editing affordances for the two lyric tracks: where the word ticks are,
   * which one is under the cursor, and which one is selected.
   *
   * Drawn in the overlay, not the cached layer. These change on every hover, and
   * putting them in the layer would rebuild the whole waveform per mouse move —
   * the highlight outline lives in the overlay for the same reason.
   *
   * Handles mark where the word ticks are, so a tick is something you can aim at
   * rather than an invisible ±6px strip around a 1px line. The tolerance stays
   * deliberately WIDER than the 2px mark (TICK_GRAB); the hover highlight names
   * the tick that would be grabbed before you press, and seeing the target takes
   * priority over making the target exactly as big as it looks.
   */
  paintPointer(ctx) {
    const BANDS = [['main', 158], ['translation', 225]];
    const line = this.pointer && this.pointer.kind !== 'block' ? this.pointer
               : this.selected && this.selected.kind !== 'block' ? this.selected : null;
    const xAt = ms => (ms / 1000 - this.offset) * this.pps;

    // Handles for every word tick of the line being worked on.
    if (line) {
      const group = this.doc.groups[line.index];
      const track = group && group[line.field];
      if (track) {
        const band = this.bandY(line.field);
        const hovered = this.pointer && this.pointer.kind !== 'block' ? this.pointer : null;
        const caps = [], hot = [];
        for (let j = 1; j < track.words.length; j++) {
          const x = xAt(track.words[j].start);
          if (x < -8 || x > this.canvasWidth() + 8) continue;
          const isHot = hovered && hovered.index === line.index && hovered.field === line.field
            && hovered.kind === 'word' && hovered.word === j;
          (isHot ? hot : caps).push(x);
        }
        ctx.fillStyle = Theme.token('--wave-word-cap');
        ctx.beginPath();
        for (const x of caps) ctx.rect(x - 1, band - 7, 2, 7);
        ctx.fill();
        // 指针正压着的那一个画大一档：这是"能抓的那个"，它比旁边那排更重要。
        if (hot.length) {
          ctx.fillStyle = Theme.token('--wave-pointer');
          ctx.beginPath();
          for (const x of hot) ctx.rect(x - 2, band - 9, 4, 11);
          ctx.fill();
        }
      }
    }
    // The tick under the cursor, then the selected one, drawn over it.
    //
    // 这一对是"能抓的那个"：静态层里句首/句尾 2px、逐字 1px，这里再宽一档
    // （悬停 3px、已选中 3.5px），所以指针压在哪一条上一眼就能看出来 ——
    // 几条刻度挤在一起时，看见目标比把目标画得和命中区一样大更要紧。
    for (const [mark, colour, width] of [[this.pointer, Theme.token('--wave-pointer'), 3],
                                         [this.selected, Theme.token('--ink-highlight'), 3.5]]) {
      if (!mark || mark.kind === 'block') continue;
      const y = this.bandY(mark.field);
      const x = xAt(mark.time ?? this.trackStartSafe(this.doc.groups[mark.index], mark.field));
      if (x < -4 || x > this.canvasWidth() + 4) continue;
      ctx.strokeStyle = colour;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(x, y - 8);
      ctx.lineTo(x, y + 34);
      ctx.stroke();
      ctx.lineWidth = 1;
      // 悬停的那条再加一格小方块，形状上也能认出来（不只靠颜色）。
      if (mark === this.pointer) {
        ctx.fillStyle = colour;
        ctx.fillRect(x - 2, y - 11, 4, 4);
      }
    }
  }

  /**
   * How much band to leave showing at the right edge of a block, so consecutive
   * lines never touch. Graduated for the same reason as the word seam: a fixed
   * width is either invisible on a wide block or eats a block that is only a few
   * pixels wide when the whole song is on screen.
   */
  blockGap(span) {
    // Wider than the word seam (6/3/1): two things are being told apart here —
    // where a word ends and where a LINE ends — and a line break is the bigger
    // of the two. A gap of 6px still reads as "粘在一起", so the line gap is
    // 10px. That is also the point where the darker band between two blocks
    // starts to read as a gap rather than as a scratch on the bar; below it the
    // two blocks merge.
    return span > 28 ? 10 : span > 14 ? 5 : 2;
  }

  /**
   * The on-screen span of each word of one line, in layer coordinates.
   *
   * A word owns the time from its own start to the next word's start (the last
   * one runs to the line end), which is exactly what makes the cells tile the
   * line with no gaps and no overlaps. Returns [] when the track carries no word
   * timings, so the caller keeps the single-block behaviour.
   */
  wordCells(track, trackStart, lineEnd, xAt) {
    const words = track.words || [];
    if (words.length <= 1) return [];
    const cells = [];
    for (let j = 0; j < words.length; j++) {
      const from = words[j].start ?? trackStart;
      const to = j + 1 < words.length ? words[j + 1].start : lineEnd;
      const left = xAt(from);
      const right = xAt(Math.max(to, from + 1));
      cells.push({word: j, text: words[j].text, left, right});
    }
    return cells;
  }

  bandY(field) {
    return field === 'translation' ? 225 : 158;
  }

  // Which lyric block sits under a point? Used for double-click-to-edit.
  blockAt(e) {
    const rect = this.canvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const time = this.offset + x / this.pps;
    const groups = this.doc.groups;
    for (const [field, top] of [['main', 158], ['translation', 225]]) {
      if (y < top || y > top + 32) continue;
      for (let index = 0; index < groups.length; index++) {
        const track = groups[index][field];
        if (!track.text) continue;
        const start = (track.start ?? groups[index].start) / 1000;
        const rawEnd = track.end ?? (groups[index + 1] ? groups[index + 1].start : (track.start ?? groups[index].start) + 2500);
        const end = Math.max(rawEnd / 1000, start + 0.4);
        if (time >= start && time <= end) return {index, field};
      }
    }
    return null;
  }
  up(e) {
    if (!this.drag) return;
    const change = {...this.drag, time: this.eventTime(e)};
    // Describe the outcome before the drag state goes away: this is the last
    // chance to tell the user what actually landed (and by how much).
    const info = this.dragInfo(change);
    this.drag = null;
    this.canvas.style.cursor = '';
    // Back to the narrow margin: the wide one only exists to make a drag cheap,
    // and keeping it would leave a needlessly oversized layer allocated.
    this.layerAnchor = null;
    // A press that never moved is a CLICK, not a drag, and must not edit.
    // The rule already existed for lyric blocks; the tick path was missing it,
    // so simply clicking near a word boundary silently shifted that boundary by
    // up to the ±6px grab zone (≈66ms at 90px/s) and pushed a junk "调整时间"
    // step onto the undo stack, discarding redo. Applying the same rule to every
    // kind keeps the two paths from drifting apart again.
    change.delta = change.time - (change.grabTime ?? change.time);
    if (Math.abs(change.delta) < 1) {
      this.options.seek(change.time / 1000);
      this.options.pointer?.(info);
      this.draw();
      return;
    }
    this.options.change(change);
    this.options.pointer?.(info);
    this.draw();
  }
  invalidate() { this.dirty = true; this.draw(); }
  // Coalesce every redraw request into one paint per animation frame.
  //
  // The guard is not just an optimisation: paint() itself calls drawOverview()
  // and the strip's own pointer handlers call draw(), so without it a single
  // frame could schedule the next one and the counter ran away (measured: 121
  // paints and 75 full layer rebuilds for 60 drag frames, which is what made a
  // continuous drag stutter).
  draw() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.paint(); });
  }
  fitText(ctx, text, maxWidth) {
    if (maxWidth < 10) return '';
    let width = this.textWidths.get(text);
    if (width === undefined) {
      width = ctx.measureText(text).width;
      if (this.textWidths.size > 3000) this.textWidths.clear();
      this.textWidths.set(text, width);
    }
    if (width <= maxWidth) return text;
    let low = 0, high = text.length;
    while (low < high) {
      const mid = (low + high + 1) >> 1;
      const piece = text.slice(0, mid);
      let candidate = this.textWidths.get(piece);
      if (candidate === undefined) {
        candidate = ctx.measureText(piece).width;
        this.textWidths.set(piece, candidate);
      }
      if (candidate <= maxWidth - 7) low = mid; else high = mid - 1;
    }
    return low > 0 ? text.slice(0, low) + '…' : '';
  }
  paint() {
    const started = performance.now();
    const width = this.canvasWidth(), height = 292;
    const ratio = window.devicePixelRatio || 1;
    const w = Math.round(width * ratio), h = Math.round(height * ratio);
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w; this.canvas.height = h;
      this.canvas.style.width = width + 'px'; this.canvas.style.height = height + 'px';
      this.dirty = true;
    }
    // A track costs one layout read, so it must not sit inside a per-frame
    // loop; the drag path below reads it only when a drag is actually active.
    //
    // The coverage test is not optional. Blitting replaces "repaint every frame"
    // with "repaint once, then translate", which only works while the viewport
    // stays INSIDE what was painted. Follow mode walks the viewport forward a
    // whole viewport at a time, so once it leaves the margin the layer has no
    // pixels for the newly revealed area and the canvas goes blank — measured:
    // first gap at 9s of playback, the entire canvas blank by 17s, with exactly
    // ONE rebuild in twenty seconds. That is what "跟随功能不能用了" looked like.
    const needsLayer = this.dirty ||
      !this.viewportIsCovered() ||
      !this.staticKeyMatches(w, h, ratio);
    if (needsLayer) {
      const before = performance.now();
      this.paintLayerLayer(w, h, ratio, width, height);
      this.stats.layerMs = performance.now() - before;
      this.stats.layerPaints++;
      this.dirty = false;
    } else {
      this.stats.blits++;
    }
    const ctx = this.canvas.getContext('2d', OPAQUE);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    // Fill rather than clear. The canvas is declared opaque, so clearRect would
    // leave BLACK here; filling with the layer's own background means an
    // uncovered sliver is invisible rather than a black band.
    // panSpan() guarantees the coverage (asserted by
    // tests/web_layer_coverage_check.js); this fill is defence in depth — a
    // silent black band at the canvas edge ships unnoticed.
    ctx.fillStyle = canvasBackground();
    ctx.fillRect(0, 0, w, h);
    // Place the pre-rendered layer by translation. `dx` is the sub-pixel
    // distance the viewport has moved since the layer was painted, so the
    // content stays aligned to the grid no matter when the layer was built.
    const dx = (this.layerAnchor - this.offset) * this.pps * ratio;
    ctx.drawImage(this.layer, dx, 0);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const time = this.drag ? this.drag.time / 1000 : this.time;
    const playhead = (time - this.offset) * this.pps;
    // Outline first, playhead second: the playhead is the thing the eye tracks
    // during playback and must never be covered by a rectangle border.
    // Track captions sit under both: they are static furniture, and the playhead
    // crosses them every time it passes.
    this.paintTrackHints(ctx);
    this.paintHighlight(ctx, height);
    this.paintFit(ctx, height);
    // Editing affordances last of the "furniture", before the playhead: the
    // playhead is what the eye tracks during playback and stays on top.
    this.paintPointer(ctx);
    ctx.strokeStyle = this.drag ? Theme.token('--wave-drag') : Theme.token('--wave-cursor');
    ctx.beginPath(); ctx.moveTo(playhead, 0); ctx.lineTo(playhead, height); ctx.stroke();
    if (this.drag) {
      ctx.fillStyle = Theme.token('--ink-highlight');
      ctx.fillText(time.toFixed(3) + 's', Math.min(width - 90, Math.max(10, playhead + 8)), 18);
    }
    // While a block is being dragged, preview its landing position live.
    if (this.drag?.kind === 'block') {
      const g = this.doc.groups[this.drag.index], track = g?.[this.drag.field];
      if (track) {
        const deltaMs = this.drag.time - this.drag.grabTime;
        const y = this.drag.field === 'main' ? 158 : 225;
        const start = (track.start ?? g.start) + deltaMs;
        const end = (track.end ?? (this.doc.groups[this.drag.index + 1]
          ? this.doc.groups[this.drag.index + 1].start : (track.start ?? g.start) + 2500)) + deltaMs;
        ctx.strokeStyle = Theme.token('--wave-drag');
        ctx.lineWidth = 2;
        ctx.strokeRect((start / 1000 - this.offset) * this.pps, y, Math.max(6, (end - start) / 1000 * this.pps), 32);
        ctx.lineWidth = 1;
      }
    }
    this.stats.frameMs = performance.now() - started;
    this.stats.frames++;
    this.drawOverview();
  }
  // Is the current viewport still inside the window the layer was painted for?
  //
  // The layer covers [layerAnchor, layerAnchor + layerSpan] in song seconds.
  // While the viewport stays inside that, the blit is valid and costs nothing.
  // The moment it steps outside, the layer is worthless for the new area and
  // must be repainted — otherwise the revealed part of the canvas is blank.
  //
  // This is THE condition that makes "paint once, then scroll" safe. Leaving it
  // out is what broke follow: playback advances the viewport continuously, and
  // the only other rebuild triggers (dirty flag, zoom, resize, document change)
  // never fire on their own during playback.
  viewportIsCovered() {
    if (this.layerAnchor === null || !(this.layerSpan > 0)) return false;
    const epsilon = 1e-6;
    return this.offset >= this.layerAnchor - epsilon &&
           this.offset + this.viewportSeconds() <= this.layerAnchor + this.layerSpan + epsilon;
  }
  // Everything that does NOT change while the viewport scrolls: song length,
  // pixels-per-second, size, layer resolution and the document itself. Zoom and
  // resize legitimately change it; panning deliberately does not.
  // 静态层的判据：视窗、缩放、图层分辨率、文档与峰值数 —— 以及**主题**。
  // 主题进了判据，换主题后第一次绘制必然重画图层，不需要谁来记得通知它。
  staticKeyMatches(w, h, ratio) {
    return this.layerWidth === w && this.layerHeight === h &&
           this.layerRatio === ratio && this.layerPps === this.pps &&
           this.layerDocVersion === this.docVersion &&
           this.layerPeaks === this.peaks.length &&
           this.layerTheme === Theme.current();
  }
  // Paint the static layer at a coarse anchor offset, with a margin on both
  // sides so a normal drag can travel for a while before the anchor has to be
  // moved. The margin is what turns "rebuild 900px of lyrics every frame" into
  // "rebuild once, then blit", which is the difference between a drag that
  // stutters and one that does not.
  paintLayerLayer(w, h, ratio, width, height) {
    const MARGIN = this.drag ? 1.5 : 1;
    const anchor = this.snapAnchor();
    this.layerAnchor = anchor;
    this.layerRatio = ratio;
    this.layerPps = this.pps;
    this.layerDocVersion = this.docVersion;
    this.layerTheme = Theme.current();
    this.layerPeaks = this.peaks.length;
    this.layerWidth = w; this.layerHeight = h;
    // Recorded so viewportIsCovered() can tell whether a later frame is still
    // inside this paint. Without it there is no way to know the layer has run
    // out, and the canvas silently goes blank while scrolling.
    this.layerSpan = this.panSpan();
    this.paintLayer(w, h, ratio, width, height, anchor, null, this.layerSpan);
  }
  // How much song time the layer covers while staying pre-rendered.
  //
  // It must ALWAYS exceed the viewport, by at least half a second on each side.
  // The layer is painted at a whole-second anchor and then blitted at
  // (anchor - offset), so it can be shifted by up to half a second in either
  // direction; a layer exactly one viewport wide leaves a strip of the canvas
  // uncovered whenever `offset` is not a whole number — follow mode makes it
  // fractional on every frame during playback.
  //
  // A drag asks for even more, so a long drag crosses the margin slowly instead
  // of hitting the edge and rebuilding.
  panSpan() {
    const viewport = this.viewportSeconds();
    const margin = this.drag ? Math.max(2, viewport * 2) : 2;
    return viewport + margin;
  }
  // The layer is painted at a whole-second anchor (cheap to compare, and the
  // blit immediately corrects the remainder), positioned so the margin is split
  // evenly around the viewport and is not wasted off the front of the song.
  snapAnchor() {
    const span = this.panSpan();
    const raw = this.offset - (span - this.viewportSeconds()) / 2;
    const max = Math.max(0, this.songSeconds());
    const snapped = Math.round(raw);
    return Math.max(0, Math.min(max, snapped));
  }
  // Paints the static content. `anchor` is the song time that maps to x=0 of
  // the layer (usually NOT this.offset — paint() blits the layer to make up the
  // difference), and `span` overrides how much song time the layer covers.
  //
  // Both matter for a smooth drag: painting at a coarse anchor means a scroll
  // does not invalidate the layer, and `span` lets the layer cover a margin
  // beyond the visible area so the anchor survives a long drag.
  paintLayer(w, h, ratio, width, height, anchor = this.offset, _unused, span) {
    const layer = this.layer;
    // The layer may be wider than the canvas when a pan margin is requested.
    const layerWidth = span ? Math.max(width, Math.round(span * this.pps)) : width;
    const lw = Math.round(layerWidth * ratio);
    if (layer.width !== lw || layer.height !== h) { layer.width = lw; layer.height = h; }
    const ctx = this.layerCtx;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.fillStyle = canvasBackground(); ctx.fillRect(0, 0, layerWidth, height);
    ctx.font = '12px Segoe UI, Microsoft YaHei';
    const xAt = ms => (ms / 1000 - anchor) * this.pps;
    // Visible song range of this layer: off-screen groups are skipped instead of
    // walking every line in the document on each rebuild.
    const viewStartMs = anchor * 1000;
    const viewEndMs = (anchor + layerWidth / this.pps) * 1000;
    const channels = this.peaks.length || 1;
    if (this.peaks.length) {
      // Peaks are drawn per canvas pixel, but the layer may be wider than the
      // canvas, so the envelope buffers are sized to the layer.
      if (!this.tops || this.tops.length < layerWidth + 1) {
        this.tops = new Float32Array(layerWidth + 1);
        this.bottoms = new Float32Array(layerWidth + 1);
      }
      // One filled envelope per channel beats ~1400 separately stroked segments.
      for (let ch = 0; ch < this.peaks.length; ch++) {
        const peak = this.peaks[ch], limit = peak.values.length;
        const center = channels === 1 ? 61 : 34 + ch * 53;
        const scale = channels === 1 ? 43 : 23;
        for (let x = 0; x <= layerWidth; x++) {
          const a = Math.floor((anchor + x / this.pps) * peak.rate);
          const b = Math.ceil((anchor + (x + 1) / this.pps) * peak.rate);
          let value = 0;
          for (let j = a < 0 ? 0 : a, stop = b > limit ? limit : b; j < stop; j++) {
            const v = peak.values[j]; if (v > value) value = v;
          }
          const amp = value * scale;
          this.tops[x] = center - amp;
          this.bottoms[x] = center + amp;
        }
        ctx.beginPath();
        ctx.moveTo(0, this.tops[0]);
        for (let x = 1; x <= layerWidth; x++) ctx.lineTo(x, this.tops[x]);
        for (let x = layerWidth; x >= 0; x--) ctx.lineTo(x, this.bottoms[x]);
        ctx.closePath();
        ctx.fillStyle = ch ? Theme.token('--wave-ch-right') : Theme.token('--wave-ch-mono');
        ctx.fill();
        ctx.fillStyle = Theme.token('--wave-ch-label'); ctx.fillText(ch ? 'R' : 'L / MONO', 9, center - 14);
      }
    } else {
      ctx.fillStyle = Theme.token('--wave-hint'); ctx.fillText('添加音频后显示真实波形；歌词双轨可独立编辑', 16, 62);
    }
    const step = this.pps < 50 ? 5 : 1;
    const first = Math.ceil(anchor / step) * step;
    const last = anchor + layerWidth / this.pps;
    // 时间标尺与网格：按三档粗细画（钢琴窗的做法 —— 格子要看得见，细分只作暗示）。
    //   细分线（放大到 100px/s 以上才画）→ 每 step 一条 → 每 5 step 一条加重
    // 三档各一条路径，所以整段网格仍然只有三次 stroke。
    ctx.fillStyle = Theme.token('--wave-name-bg');
    ctx.fillRect(0, 116, layerWidth, 22);
    ctx.strokeStyle = Theme.token('--wave-ring');
    ctx.beginPath(); ctx.moveTo(0, 138.5); ctx.lineTo(layerWidth, 138.5); ctx.stroke();
    if (this.pps >= 100) {
      ctx.strokeStyle = Theme.token('--wave-grid');
      ctx.beginPath();
      for (let t = Math.ceil(anchor * 2) / 2; t < last; t += 0.5) {
        if (Math.abs(t / step - Math.round(t / step)) < 1e-6) continue;
        const x = Math.round(xAt(t * 1000)) + .5;
        ctx.moveTo(x, 138); ctx.lineTo(x, height);
      }
      ctx.stroke();
    }
    // One path for the whole grid instead of one stroke per tick.
    ctx.strokeStyle = Theme.token('--wave-ring');
    ctx.beginPath();
    for (let t = first; t < last; t += step) {
      const x = Math.round(xAt(t * 1000)) + .5;
      ctx.moveTo(x, 138); ctx.lineTo(x, height);
    }
    ctx.stroke();
    // 每第 5 格加重，5 秒 / 25 秒 这类位置一眼能找到。
    ctx.strokeStyle = Theme.token('--wave-ring-label');
    ctx.beginPath();
    for (let t = first; t < last; t += step) {
      if (Math.round(t) % (step * 5) !== 0) continue;
      const x = Math.round(xAt(t * 1000)) + .5;
      ctx.moveTo(x, 138); ctx.lineTo(x, height);
    }
    ctx.stroke();
    ctx.font = '10px Segoe UI, Microsoft YaHei';
    ctx.fillStyle = Theme.token('--wave-ring-label');
    for (let t = first; t < last; t += step) ctx.fillText(t.toFixed(0) + 's', xAt(t * 1000) + 4, 124);
    this.hits = [];
    const groups = this.doc.groups;
    // Groups are ordered by time, so the visible slice can be found by scanning
    // in from the edges instead of testing all of them. Hits still mirror the
    // whole visible range, so no interaction is lost.
    let lo = 0, hi = groups.length;
    while (lo < hi) {                       // first group whose end could be visible
      const mid = (lo + hi) >> 1;
      const g = groups[mid];
      const end = g.main.end ?? g.main.start ?? g.start;
      if (end < viewStartMs) lo = mid + 1; else hi = mid;
    }
    const scanStart = Math.max(0, lo - 1);
    for (const [field, y, color] of [['main', 158, Theme.token('--wave-name-main')], ['translation', 225, Theme.token('--wave-name-trans')]]) {
      ctx.fillStyle = Theme.token('--wave-name-bg'); ctx.fillRect(0, y - 10, layerWidth, 62);
      // Pass 1: one cell per word, with a 1px seam between them.
      //
      // A single rect for the whole line renders it as one continuous bar; the
      // word boundaries are then only 1px strokes in the label colour, which read
      // as text decoration rather than cell edges. A gap that lets the band show
      // through makes the cells read as separate, and it costs nothing: still one
      // path, one fill.
      ctx.beginPath();
      for (let index = scanStart; index < groups.length; index++) {
        const g = groups[index], track = g[field];
        if (!track.text) continue;
        const trackStart = track.start ?? g.start;
        if (trackStart > viewEndMs) break;
        const end = track.end ?? (groups[index + 1] ? groups[index + 1].start : trackStart + 2500);
        if (end < viewStartMs) continue;
        const left = xAt(trackStart), right = xAt(end);
        if (right < 0 || left > layerWidth) continue;
        const cells = this.wordCells(track, trackStart, end, xAt);
        // A line's block must not run into the next line's.
        //
        // In plain LRC a line carries only a start, so the block extends to the
        // next line's start — and two consecutive blocks then ABUT exactly, which
        // reads as one continuous bar: "两句话连在一起我压根不知道普通LRC编辑时长到哪里了".
        // The gap is what shows where one line stops and the next begins, and it is
        // also what makes it possible to see the end you are about to drag.
        const edge = this.blockGap(right - left);
        if (cells.length <= 1) {
          ctx.rect(left, y, Math.max(2, right - left - edge), SaltWaveform.CELL_HEIGHT);
        } else {
          for (let i = 0; i < cells.length; i++) {
            const cell = cells[i];
            if (cell.right < 0 || cell.left > layerWidth) continue;
            // How much band shows through between cells. Graduated: a fixed width
            // fails at one end or the other — 2px still reads as "粘在一起" at a
            // normal zoom, while 6px would eat a cell that is only 8px wide when
            // zoomed out.
            const span = cell.right - cell.left;
            const seam = span > 28 ? 6 : span > 14 ? 3 : 1;
            // The LAST cell also ends the LINE, so it reserves the line's own
            // gap; otherwise the gap between two lines is decided by how wide the
            // final word happens to be, and a line ending on a short syllable
            // leaves only 1–3px before the next line, reading the two sentences
            // as one bar.
            const last = i === cells.length - 1;
            const edge = last ? Math.max(seam, this.blockGap(right - left)) : seam;
            ctx.rect(cell.left, y, Math.max(2, cell.right - cell.left - edge), SaltWaveform.CELL_HEIGHT);
          }
        }
      }
      ctx.fillStyle = field === 'main' ? Theme.token('--wave-cell-main') : Theme.token('--wave-cell-trans');
      ctx.fill();
      // 描边：一格一格的边界要看得见（钢琴窗里的音符有明确边缘，
      // 只靠底色深浅区分，缩放一小就糊成一条）。
      ctx.strokeStyle = Theme.token('--wave-cell-edge');
      ctx.lineWidth = 1;
      ctx.stroke();
      // Pass 2: the tick lines, in two weights.
      //
      // 句首/句尾（行自己的边界）比逐字边界更重：这三样经常落在同一两个像素里，
      // 一样粗就分不出"我准备抓的是哪一条"。命中的每条都照旧记下来，抓哪条由
      // hitAt() 按距离判定，画得重不等于优先。
      ctx.strokeStyle = color;
      const lineTicks = [], wordTicks = [];
      for (let index = scanStart; index < groups.length; index++) {
        const g = groups[index], track = g[field];
        if (!track.text) continue;
        const trackStart = track.start ?? g.start;
        if (trackStart > viewEndMs) break;
        const end = track.end ?? (groups[index + 1] ? groups[index + 1].start : trackStart + 2500);
        if (end < viewStartMs) continue;
        const left = xAt(trackStart), right = xAt(end);
        if (right < 0 || left > layerWidth) continue;
        const startX = xAt(trackStart);
        lineTicks.push(startX);
        this.hits.push({time: trackStart, kind: 'start', word: undefined, x: startX, y: y - 5, height: 42, field, index});
        const words = track.words;
        if (words) {
          for (let j = 1; j < words.length; j++) {
            const x = xAt(words[j].start);
            if (x < -8 || x > layerWidth + 8) continue;
            wordTicks.push(x);
            this.hits.push({time: words[j].start, kind: 'word', word: j, x, y: y - 5, height: 42, field, index});
          }
        }
        if (track.end != null) {
          const endX = xAt(track.end);
          lineTicks.push(endX);
          this.hits.push({time: track.end, kind: 'end', word: undefined, x: endX, y: y - 5, height: 42, field, index});
        }
      }
      for (const [ticks, width] of [[lineTicks, SaltWaveform.TICK_WIDTH_LINE],
                                    [wordTicks, SaltWaveform.TICK_WIDTH_WORD]]) {
        if (!ticks.length) continue;
        ctx.lineWidth = width;
        ctx.beginPath();
        for (const x of ticks) { ctx.moveTo(x, y - 4); ctx.lineTo(x, y + 42); }
        ctx.stroke();
      }
      ctx.lineWidth = 1;
      // Where a line without an explicit end actually stops.
      //
      // Plain LRC carries no 句尾, so the duration is derived (the next line's
      // start) and there was nothing on screen to say where it lands. Drawn faint
      // and deliberately NOT pushed as a hit: it is a derived boundary, not
      // something the file states, and offering it as a drag handle would invent a
      // time the user never set.
      ctx.strokeStyle = Theme.token('--wave-cell-line');
      ctx.beginPath();
      for (let index = scanStart; index < groups.length; index++) {
        const g = groups[index], track = g[field];
        if (!track.text || track.end != null) continue;
        const trackStart = track.start ?? g.start;
        if (trackStart > viewEndMs) break;
        const stop = (groups[index + 1] ? groups[index + 1].start : trackStart + 2500);
        if (stop < viewStartMs) continue;
        const marker = xAt(stop) - this.blockGap(xAt(stop) - xAt(trackStart));
        if (marker < -4 || marker > layerWidth + 4) continue;
        ctx.moveTo(marker, y + 3);
        ctx.lineTo(marker, y + 29);
      }
      ctx.stroke();
      // Pass 3: labels last, so the text is never covered by a later fill.
      //
      // One label per CELL, fitted to that cell: with the line's text drawn once
      // across the whole block, the seams would cut straight through the text and
      // the cells would stop making sense. A cell too narrow for even one glyph
      // gets no label — the seam still shows the division.
      for (let index = scanStart; index < groups.length; index++) {
        const g = groups[index], track = g[field];
        if (!track.text) continue;
        const trackStart = track.start ?? g.start;
        if (trackStart > viewEndMs) break;
        const end = track.end ?? (groups[index + 1] ? groups[index + 1].start : trackStart + 2500);
        if (end < viewStartMs) continue;
        const right = xAt(end);
        if (right < 0 || xAt(trackStart) > layerWidth) continue;
        const cells = this.wordCells(track, trackStart, end, xAt);
        ctx.fillStyle = color;
        if (!cells.length) {
          const label = this.fitText(ctx, track.text,
                                     Math.min(layerWidth, right) - Math.max(0, xAt(trackStart)) - 12);
          if (label) ctx.fillText(label, xAt(trackStart) + 6, y + 21);
          continue;
        }
        // textAlign is shared context state, so it is set for these labels and put
        // back afterwards: the other passes of this layer assume left alignment.
        ctx.textAlign = 'center';
        for (const cell of cells) {
          if (cell.right < 0 || cell.left > layerWidth) continue;
          const width = Math.min(layerWidth, cell.right) - Math.max(0, cell.left) - 6;
          if (width < 4) continue;
          // Centred in its own cell, so a label reads as belonging to that cell
          // instead of floating at a boundary.
          const label = this.fitText(ctx, cell.text, width);
          if (label) ctx.fillText(label, Math.max(2, cell.left + (cell.right - cell.left) / 2), y + 21);
        }
        ctx.textAlign = 'left';
      }
    }
  }
  // Row captions for the two tracks.
  //
  // NOT part of the cached layer: a caption belongs to the row, not to the
  // content. Painted into the layer it would be blitted at an offset like
  // everything else, so it would slide sideways with the waveform and
  // eventually off the canvas entirely — leaving no caption at some viewports.
  //
  // And no language in the text. The main track is not necessarily Chinese and
  // the translation track is not necessarily Japanese (a Japanese song keeps
  // its original as the main track), so naming a language here would be wrong,
  // not merely redundant.
  /** 贴唱预览的叠加：虚线框 + 一个三角标记，画在**覆盖层**（不注册命中区）。
   *
   * 只在视窗内的行才画 —— 一首歌几百行时不值得为看不见的东西算坐标。 */
  paintFit(ctx, height) {
    const lines = this.fitLines;
    if (!lines || !lines.length) return;
    const width = this.canvasWidth();
    const viewport = this.viewportSeconds();
    ctx.save();
    ctx.strokeStyle = Theme.token('--wave-pick');
    ctx.fillStyle = Theme.token('--wave-pick');
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    for (const line of lines) {
      const start = line.start / 1000;
      const end = line.end == null ? start + 2 : line.end / 1000;
      if (end < this.offset || start > this.offset + viewport) continue;   // 视窗外的跳过
      const from = (start - this.offset) * this.pps;
      const to = (end - this.offset) * this.pps;
      ctx.strokeRect(Math.max(-2, from), 2, Math.max(2, Math.min(width, to) - Math.max(-2, from)),
                     height - 4);
      ctx.setLineDash([]);
      // 句首的那个小三角：一眼看出"它现在从哪儿开始"
      ctx.beginPath();
      ctx.moveTo(from - 4, 0);
      ctx.lineTo(from + 4, 0);
      ctx.lineTo(from, 6);
      ctx.closePath();
      ctx.fill();
      ctx.setLineDash([4, 3]);
    }
    ctx.restore();
  }

  paintTrackHints(ctx) {
    for (const [label, y] of [['主轨', 158], ['翻译轨', 225]]) {
      // A backing chip in the canvas colour: whatever scrolls underneath (a grid
      // label, a block edge) must not be able to make the caption unreadable.
      const chip = Math.round(ctx.measureText(label).width) + 10;
      ctx.fillStyle = Theme.token('--wave-scrim');
      ctx.fillRect(6, y - 25, chip, 13);
      ctx.fillStyle = Theme.token('--wave-scrim-ink');
      ctx.fillText(label, 11, y - 14);
    }
  }
  // The hover / playback outline. Deliberately NOT part of the static layer:
  // it changes on every hovered or clicked row, and painting it there forced a
  // full layer rebuild per interaction (~2150 canvas calls). As an overlay it
  // costs a few strokes and never invalidates anything.
  paintHighlight(ctx, height) {
    const groups = this.doc.groups;
    const highlighted = this.highlightIndex();
    if (highlighted == null || !groups[highlighted]) return;
    const xAt = ms => (ms / 1000 - this.offset) * this.pps;
    ctx.strokeStyle = Theme.token('--ink-highlight');
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (const [field, y] of [['main', 158], ['translation', 225]]) {
      const g = groups[highlighted], track = g[field];
      if (!track.text) continue;
      const start = track.start ?? g.start;
      const end = track.end ?? (groups[highlighted + 1] ? groups[highlighted + 1].start : start + 2500);
      const left = xAt(start), right = xAt(end);
      // Cheap reject: an outline scrolled out of view is not worth stroking.
      if (right < 0 || left > this.canvasWidth()) continue;
      ctx.rect(left - 1, y - 1, Math.max(3, right - left) + 2, 34);
    }
    ctx.stroke();
    ctx.lineWidth = 1;
  }
}
