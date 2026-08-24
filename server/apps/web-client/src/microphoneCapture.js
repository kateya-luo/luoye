const TARGET_SAMPLE_RATE = 16000;
const FRAME_SAMPLES = 9600;

function appendInt16(left, right) {
  const merged = new Int16Array(left.length + right.length);
  merged.set(left);
  merged.set(right, left.length);
  return merged;
}

class StreamingResampler {
  constructor(inputRate, outputRate = TARGET_SAMPLE_RATE) {
    this.ratio = inputRate / outputRate;
    this.buffer = new Float32Array(0);
    this.position = 0;
  }

  push(input) {
    const combined = new Float32Array(this.buffer.length + input.length);
    combined.set(this.buffer);
    combined.set(input, this.buffer.length);
    const output = [];
    while (this.position + this.ratio <= combined.length) {
      const start = Math.floor(this.position);
      const end = Math.max(start + 1, Math.floor(this.position + this.ratio));
      let sum = 0;
      let count = 0;
      for (let index = start; index < end && index < combined.length; index += 1) {
        sum += combined[index];
        count += 1;
      }
      const sample = Math.max(-1, Math.min(1, sum / Math.max(1, count)));
      output.push(sample < 0 ? Math.round(sample * 0x8000) : Math.round(sample * 0x7fff));
      this.position += this.ratio;
    }
    const consumed = Math.floor(this.position);
    this.buffer = combined.slice(consumed);
    this.position -= consumed;
    return Int16Array.from(output);
  }
}

export async function listMicrophones() {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  return (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === 'audioinput');
}

export async function startMicrophoneCapture({deviceId = '', onFrame, onStats}) {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error('当前环境不支持麦克风采集');
  const stream = await navigator.mediaDevices.getUserMedia({audio: {
    ...(deviceId ? {deviceId: {exact: deviceId}} : {}),
    channelCount: {ideal: 1}, echoCancellation: false, noiseSuppression: false, autoGainControl: false,
  }});
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  const context = new AudioContextClass({latencyHint: 'interactive'});
  await context.resume();
  const source = context.createMediaStreamSource(stream);
  const mute = context.createGain();
  mute.gain.value = 0;
  mute.connect(context.destination);
  const resampler = new StreamingResampler(context.sampleRate);
  let pending = new Int16Array(0);
  let chunks = 0;
  let bytes = 0;
  let peak = 0;
  let processor;
  let usingWorklet = false;

  const publishStats = () => onStats?.({chunks, bytes, level: Math.min(1, peak)});
  const statsTimer = window.setInterval(() => { publishStats(); peak *= 0.45; }, 150);
  const emit = (flush = false) => {
    while (pending.length >= FRAME_SAMPLES || (flush && pending.length)) {
      const size = pending.length >= FRAME_SAMPLES ? FRAME_SAMPLES : pending.length;
      const frame = pending.slice(0, size);
      pending = pending.slice(size);
      onFrame(frame.buffer);
      chunks += 1;
      bytes += frame.byteLength;
    }
    publishStats();
  };
  const consume = (samples) => {
    for (let index = 0; index < samples.length; index += 1) peak = Math.max(peak, Math.abs(samples[index]));
    pending = appendInt16(pending, resampler.push(samples));
    emit(false);
  };

  try {
    await context.audioWorklet.addModule(new URL('pcm-capture-worklet.js', window.location.href).toString());
    processor = new AudioWorkletNode(context, 'pcm-capture-processor');
    processor.port.onmessage = (event) => consume(event.data);
    source.connect(processor);
    processor.connect(mute);
    usingWorklet = true;
  } catch {
    processor = context.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = (event) => consume(event.inputBuffer.getChannelData(0));
    source.connect(processor);
    processor.connect(mute);
  }

  return {usingWorklet, async stop({flush = true} = {}) {
    window.clearInterval(statsTimer);
    if (usingWorklet) processor.port.onmessage = null;
    else processor.onaudioprocess = null;
    source.disconnect(); processor.disconnect(); mute.disconnect();
    stream.getTracks().forEach((track) => track.stop());
    if (flush) emit(true);
    await context.close();
  }};
}
