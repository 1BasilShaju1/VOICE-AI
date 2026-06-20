/* app.js — Sky Assistant web frontend
 *
 * Owns:
 *  - Mic capture + speech-to-text (Web Speech API SpeechRecognition)
 *  - Text-to-speech (Web Speech API SpeechSynthesis)
 *  - Canvas orb (idle/listening/thinking/speaking states)
 *  - Canvas waveform driven by mic volume (listening) or synthetic pulse (speaking)
 *  - Talks to the backend via POST /api/process — backend has no audio code at all
 *
 * Browser support: Chrome and Edge have full SpeechRecognition support.
 * Firefox/Safari do not (or only partially) — we detect this and fall back
 * to typed input with a visible notice, rather than silently failing.
 */

(() => {
  "use strict";

  const statusEl = document.getElementById("status");
  const captionEl = document.getElementById("caption");
  const logEl = document.getElementById("log");
  const textInput = document.getElementById("textInput");
  const sendBtn = document.getElementById("sendBtn");
  const micBtn = document.getElementById("micBtn");
  const hintEl = document.getElementById("browserHint");
  const orbCanvas = document.getElementById("orbCanvas");
  const waveCanvas = document.getElementById("waveCanvas");

  const STATE_COLORS = {
    idle: "#4f7d96",
    listening: "#4fd6c4",
    thinking: "#d6a84f",
    speaking: "#5fd97a",
  };

  let state = "idle"; // idle | listening | thinking | speaking
  let micLevel = 0;   // 0..1, from analyser while listening
  let speakLevel = 0; // 0..1, synthetic pulse while speaking

  // ---------------- logging ----------------
  function log(msg) {
    const ts = new Date().toLocaleTimeString();
    const div = document.createElement("div");
    div.textContent = `[${ts}] ${msg}`;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function setState(newState) {
    state = newState;
    statusEl.textContent = {
      idle: "Idle — click the orb or type",
      listening: "Listening…",
      thinking: "Thinking…",
      speaking: "Speaking…",
    }[newState] || newState;
  }

  // ---------------- backend call ----------------
  async function sendToBackend(text) {
    setState("thinking");
    try {
      const res = await fetch("/api/process", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const data = await res.json();
      return data.reply || "";
    } catch (err) {
      log(`Network error: ${err}`);
      return "Sorry, I couldn't reach the server.";
    }
  }

  async function handleUserText(text, source) {
    if (!text || !text.trim()) return;
    captionEl.textContent = `You: ${text}`;
    log(`(${source}) ${text}`);

    const reply = await sendToBackend(text);

    // Special protocol: core.py returns "TIMER:<seconds>:<message>" for
    // timers, since the server can't push a notification into this specific
    // tab later — the browser owns the wait instead.
    if (reply.startsWith("TIMER:")) {
      const parts = reply.split(":");
      const seconds = parseInt(parts[1], 10);
      const message = parts.slice(2).join(":");
      speak(message);
      captionEl.textContent = `${window.AI_NAME}: ${message}`;
      log(`Timer set for ${seconds}s`);
      setTimeout(() => {
        const done = "Your timer is done!";
        speak(done);
        captionEl.textContent = `${window.AI_NAME}: ${done}`;
        log("Timer fired");
      }, seconds * 1000);
      setState("idle");
      return;
    }

    captionEl.textContent = `${window.AI_NAME}: ${reply}`;
    log(`Reply: ${reply}`);
    speak(reply);
  }

  // ---------------- text-to-speech ----------------
  function speak(text) {
    if (!("speechSynthesis" in window)) {
      log("speechSynthesis not supported in this browser.");
      setState("idle");
      return;
    }
    window.speechSynthesis.cancel(); // stop anything currently queued
    const utter = new SpeechSynthesisUtterance(text);
    utter.rate = 1.0;
    utter.pitch = 1.0;

    utter.onstart = () => setState("speaking");
    utter.onend = () => {
      setState("idle");
      speakLevel = 0;
    };
    utter.onerror = () => setState("idle");

    window.speechSynthesis.speak(utter);
  }

  // ---------------- speech-to-text ----------------
  const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognizer = null;
  let recognizing = false;

  function initRecognition() {
    if (!SpeechRecognitionImpl) {
      hintEl.textContent =
        "Voice input needs Chrome or Edge. You can still type commands below.";
      micBtn.disabled = true;
      micBtn.title = "Voice not supported in this browser";
      return;
    }
    recognizer = new SpeechRecognitionImpl();
    recognizer.continuous = false;
    recognizer.interimResults = false;
    recognizer.lang = "en-US";

    recognizer.onstart = () => {
      recognizing = true;
      micBtn.classList.add("recording");
      setState("listening");
      startMicMeter();
    };

    recognizer.onresult = (event) => {
      const text = event.results[0][0].transcript;
      handleUserText(text, "voice");
    };

    recognizer.onerror = (event) => {
      log(`Speech recognition error: ${event.error}`);
      if (event.error === "not-allowed") {
        hintEl.textContent = "Microphone permission denied — allow mic access to use voice.";
      }
    };

    recognizer.onend = () => {
      recognizing = false;
      micBtn.classList.remove("recording");
      stopMicMeter();
      if (state === "listening") setState("idle");
    };
  }

  function toggleListening() {
    if (!recognizer) return;
    if (recognizing) {
      recognizer.stop();
    } else {
      try {
        recognizer.start();
      } catch (e) {
        log(`Could not start microphone: ${e}`);
      }
    }
  }

  // ---------------- mic volume meter (drives the waveform while listening) ----------------
  let audioCtx = null;
  let analyser = null;
  let micStream = null;
  let meterRAF = null;

  async function startMicMeter() {
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const source = audioCtx.createMediaStreamSource(micStream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);

      const data = new Uint8Array(analyser.frequencyBinCount);
      const tick = () => {
        analyser.getByteFrequencyData(data);
        const avg = data.reduce((a, b) => a + b, 0) / data.length;
        micLevel = Math.min(1, avg / 100);
        meterRAF = requestAnimationFrame(tick);
      };
      tick();
    } catch (e) {
      log(`Mic meter unavailable: ${e}`);
    }
  }

  function stopMicMeter() {
    if (meterRAF) cancelAnimationFrame(meterRAF);
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    if (audioCtx) audioCtx.close();
    micLevel = 0;
  }

  // ---------------- orb canvas ----------------
  const octx = orbCanvas.getContext("2d");
  let t = 0;

  function drawOrb() {
    const w = orbCanvas.width, h = orbCanvas.height;
    const cx = w / 2, cy = h / 2;
    octx.clearRect(0, 0, w, h);

    const color = STATE_COLORS[state];
    let radius = w * 0.22;
    let glowStrength = 0.35;

    if (state === "listening") {
      radius += micLevel * w * 0.06;
      glowStrength = 0.4 + micLevel * 0.4;
    } else if (state === "speaking") {
      speakLevel = 0.5 + 0.5 * Math.sin(t * 10);
      radius += speakLevel * w * 0.05;
      glowStrength = 0.4 + speakLevel * 0.3;
    } else if (state === "thinking") {
      radius += Math.sin(t * 4) * w * 0.015;
      glowStrength = 0.45;
    } else {
      radius += Math.sin(t * 1.2) * w * 0.01; // idle breathing
    }

    // outer glow
    const grad = octx.createRadialGradient(cx, cy, radius * 0.2, cx, cy, radius * 2.2);
    grad.addColorStop(0, hexWithAlpha(color, glowStrength));
    grad.addColorStop(1, hexWithAlpha(color, 0));
    octx.fillStyle = grad;
    octx.beginPath();
    octx.arc(cx, cy, radius * 2.2, 0, Math.PI * 2);
    octx.fill();

    // thinking ring (rotating arc)
    if (state === "thinking") {
      octx.strokeStyle = color;
      octx.lineWidth = 3;
      octx.beginPath();
      octx.arc(cx, cy, radius * 1.35, t * 3, t * 3 + Math.PI * 1.2);
      octx.stroke();
    }

    // core orb
    const coreGrad = octx.createRadialGradient(cx, cy, 0, cx, cy, radius);
    coreGrad.addColorStop(0, hexWithAlpha(color, 0.9));
    coreGrad.addColorStop(1, hexWithAlpha(color, 0.25));
    octx.fillStyle = coreGrad;
    octx.beginPath();
    octx.arc(cx, cy, radius, 0, Math.PI * 2);
    octx.fill();

    octx.strokeStyle = hexWithAlpha(color, 0.8);
    octx.lineWidth = 1.5;
    octx.stroke();
  }

  function hexWithAlpha(hex, alpha) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  // ---------------- waveform canvas ----------------
  const wctx = waveCanvas.getContext("2d");
  const BAR_COUNT = 48;
  let bars = new Array(BAR_COUNT).fill(0.04);

  function drawWave() {
    const w = waveCanvas.width, h = waveCanvas.height;
    wctx.clearRect(0, 0, w, h);
    const color = STATE_COLORS[state];
    const gap = w / BAR_COUNT;
    const mid = h / 2;

    let target = 0.04;
    if (state === "listening") target = 0.15 + micLevel * 0.8;
    else if (state === "speaking") target = 0.15 + speakLevel * 0.7;

    for (let i = 0; i < BAR_COUNT; i++) {
      const falloff = 1 - Math.abs(i - BAR_COUNT / 2) / (BAR_COUNT / 2) * 0.5;
      const wobble = state === "idle" ? 0 : Math.sin(t * 5 + i * 0.5) * 0.15;
      const goal = Math.max(0.04, target * falloff + wobble);
      bars[i] += (goal - bars[i]) * 0.3;

      const x = gap * i + gap / 2;
      const barH = Math.max(2, bars[i] * h * 0.85);
      wctx.strokeStyle = color;
      wctx.lineWidth = Math.max(2, gap * 0.4);
      wctx.lineCap = "round";
      wctx.beginPath();
      wctx.moveTo(x, mid - barH / 2);
      wctx.lineTo(x, mid + barH / 2);
      wctx.stroke();
    }
  }

  // ---------------- animation loop ----------------
  function loop() {
    t += 0.033;
    drawOrb();
    drawWave();
    requestAnimationFrame(loop);
  }

  // ---------------- wiring ----------------
  orbCanvas.addEventListener("click", toggleListening);
  micBtn.addEventListener("click", toggleListening);

  sendBtn.addEventListener("click", () => {
    const text = textInput.value;
    textInput.value = "";
    handleUserText(text, "typed");
  });

  textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const text = textInput.value;
      textInput.value = "";
      handleUserText(text, "typed");
    }
  });

  // ---------------- init ----------------
  initRecognition();
  setState("idle");
  loop();
  log(`${window.AI_NAME || "Assistant"} ready. Click the orb or type to begin.`);
})();
