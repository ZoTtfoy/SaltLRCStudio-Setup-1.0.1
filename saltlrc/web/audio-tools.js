"use strict";
// Native Web Audio decoding; audio never leaves the loopback service.
const SaltAudio = (() => {
  const cache = new WeakMap();
  async function decode(file) {
    if (!cache.has(file)) {
      const task = (async () => {
        if (file.size > 150 * 1024 * 1024) throw Error('音频超过 150MB，请先分段');
        const context = new AudioContext();
        try {
          const buffer = await context.decodeAudioData(await file.arrayBuffer());
          if (buffer.duration > 1200) throw Error('单曲分析最多 20 分钟');
          return buffer;
        } finally { await context.close(); }
      })();
      cache.set(file, task);
      task.catch(() => cache.delete(file));
    }
    return cache.get(file);
  }
  async function pcm16(buffer) {
    const context = new OfflineAudioContext(1, Math.ceil(buffer.duration * 16000), 16000);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.start();
    const mono = (await context.startRendering()).getChannelData(0);
    const raw = new ArrayBuffer(44 + mono.length * 2), view = new DataView(raw);
    const text = (at, value) => [...value].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
    text(0, 'RIFF'); view.setUint32(4, raw.byteLength - 8, true); text(8, 'WAVE');
    text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data');
    view.setUint32(40, mono.length * 2, true);
    for (let i = 0; i < mono.length; i++) {
      const sample = Math.max(-1, Math.min(1, mono[i]));
      view.setInt16(44 + 2 * i, sample < 0 ? sample * 32768 : sample * 32767, true);
    }
    return new Uint8Array(raw);
  }
  function base64(bytes) {
    let result = '';
    for (let i = 0; i < bytes.length; i += 8192) result += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return btoa(result);
  }
  return {decode, pcm16, base64};
})();
