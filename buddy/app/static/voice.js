// Voice for Buddy.
// Listening: records her voice and sends it to Whisper on the server, which knows the
// words in her books (much better for a child's speech). Falls back to the browser's
// recogniser (Kannada, or when the server has no Whisper).
// Speaking: natural Piper voices from the server (English, Hindi); otherwise the best
// voice this device has. Needs HTTPS or localhost for the microphone.
const Voice = (() => {
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;
  const canRecord = !!(navigator.mediaDevices && window.MediaRecorder);
  let config = { stt: "browser", stt_langs: [], tts_langs: [] };
  let session = null;        // current listening session
  let speakToken = 0;        // bumps to cancel what is playing
  const player = new Audio();
  let unlocked = false;

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { return null; }
  }
  let autoRead = store("buddy.autoRead") !== "off";

  async function init() {
    try {
      const r = await fetch("/api/voice");
      if (r.ok) config = await r.json();
    } catch (e) { /* browser engines only */ }
  }

  function listenLang(subject) {
    return subject === "hindi" ? "hi" : subject === "kannada" ? "kn" : "en";
  }
  function textLang(text) {
    if (/[ಀ-೿]/.test(text)) return "kn";
    if (/[ऀ-ॿ]/.test(text)) return "hi";
    return "en";
  }
  const BCP = { en: "en-IN", hi: "hi-IN", kn: "kn-IN" };

  function clean(text) {
    return (text || "")
      .replace(/\p{Extended_Pictographic}|️/gu, "")
      .replace(/_{2,}/g, ", blank, ")
      .replace(/[*_#`>|]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  // ---------- speaking ----------

  // Best device voice: right language, then the natural-sounding ones.
  function pickVoice(lang) {
    const voices = synth ? synth.getVoices() : [];
    const want = BCP[lang], base = lang;
    let best = null, bestScore = -1;
    for (const v of voices) {
      const vl = v.lang.replace("_", "-");
      let s = 0;
      if (vl === want) s += 10;
      else if (vl.startsWith(base + "-")) s += lang === "en" ? (vl === "en-GB" ? 7 : 6) : 5;
      else continue;
      if (/premium|enhanced|natural|neural/i.test(v.name)) s += 5;
      if (/google|siri|samantha|veena|rishi|lekha|isha/i.test(v.name)) s += 3;
      if (/compact|espeak|novelty|bad news|bells|whisper|zarvox/i.test(v.name)) s -= 8;
      if (s > bestScore) { best = v; bestScore = s; }
    }
    return best;
  }

  function speakBrowser(text, lang, token) {
    return new Promise((resolve) => {
      if (!synth || token !== speakToken) return resolve();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = BCP[lang];
      const v = pickVoice(lang);
      if (v) u.voice = v;
      u.rate = 0.9; u.pitch = 1.05;
      u.onend = u.onerror = () => resolve();
      synth.speak(u);
    });
  }

  async function speakServer(text, lang, token) {
    const fd = new FormData(); fd.append("text", text); fd.append("lang", lang);
    const r = await fetch("/api/tts", { method: "POST", body: fd });
    if (!r.ok) throw new Error("tts " + r.status);
    const url = URL.createObjectURL(await r.blob());
    if (token !== speakToken) { URL.revokeObjectURL(url); return; }
    await new Promise((resolve) => {
      player.onended = player.onerror = () => resolve();
      player.src = url;
      player.play().catch(() => resolve());
    });
    URL.revokeObjectURL(url);
  }

  // Speak parts in order ([hint], [answer, source] ...). Stops anything playing first.
  async function speak(parts) {
    stopSpeaking();
    const token = ++speakToken;
    for (const part of [].concat(parts)) {
      const text = clean(part);
      if (!text || token !== speakToken) continue;
      const lang = textLang(text);
      if (config.tts_langs.includes(lang)) {
        try { await speakServer(text, lang, token); continue; } catch (e) { /* fall back */ }
      }
      await speakBrowser(text, lang, token);
    }
  }

  function stopSpeaking() {
    speakToken++;
    try { player.pause(); } catch (e) { /* ignore */ }
    if (synth) synth.cancel();
  }

  // iPad only lets pages play sound after a tap: unlock both players on the first tap.
  function unlock() {
    if (unlocked) return;
    unlocked = true;
    try {
      player.src = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
      player.play().catch(() => {});
    } catch (e) { /* ignore */ }
    try { if (synth) synth.speak(new SpeechSynthesisUtterance("")); } catch (e) { /* ignore */ }
  }
  ["pointerdown", "keydown"].forEach(ev => document.addEventListener(ev, unlock, { once: true }));

  // ---------- listening ----------

  function useWhisper(subject) {
    return canRecord && config.stt === "whisper" && config.stt_langs.includes(listenLang(subject));
  }

  function pickMime() {
    for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) return m;
    }
    return "";
  }

  // Record until she stops talking (about 1.3 s of quiet), then ask Whisper.
  async function listenWhisper(subject, onText, onEnd, onStatus) {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) {
      onEnd("", "not-allowed");
      return;
    }
    const mime = pickMime();
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    const start = performance.now();
    // Background noise = the quietest level seen so far (she may start talking at once,
    // so the first moments can't be assumed quiet). Speech must be clearly above it.
    let heard = false, lastLoud = start, floor = 1, timer = null;

    const s = session = { stop: () => { if (rec.state !== "inactive") rec.stop(); }, cancelled: false };
    const tick = () => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const now = performance.now();
      floor = Math.min(floor, Math.max(rms, 0.002));
      const threshold = Math.min(0.08, Math.max(0.02, floor * 3));
      if (rms > threshold) { heard = true; lastLoud = now; }
      if ((heard && now - lastLoud > 1300) || now - start > 15000 || (!heard && now - start > 7000)) {
        s.stop();
        return;
      }
      timer = requestAnimationFrame(tick);
    };
    rec.onstop = async () => {
      cancelAnimationFrame(timer);
      stream.getTracks().forEach(t => t.stop());
      ctx.close().catch(() => {});
      if (session === s) session = null;
      if (s.cancelled) return onEnd("", "aborted");
      if (!heard || !chunks.length) return onEnd("", "no-speech");
      onStatus && onStatus("thinking");
      const type = rec.mimeType || mime || "audio/webm";
      const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      const fd = new FormData();
      fd.append("audio", new Blob(chunks, { type }), "speech." + ext);
      fd.append("subject", subject || "");
      try {
        const r = await fetch("/api/transcribe", { method: "POST", body: fd });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || r.statusText);
        onText(j.text, true);
        onEnd(j.text, j.text ? null : "no-speech");
      } catch (e) {
        onEnd("", "network");
      }
    };
    rec.start(250);
    onStatus && onStatus("listening");
    timer = requestAnimationFrame(tick);
  }

  function listenBrowser(subject, onText, onEnd, onStatus) {
    const rec = new Rec();
    rec.lang = BCP[listenLang(subject)];
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    let finalText = "", error = null;
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finalText += r[0].transcript;
        else interim += r[0].transcript;
      }
      onText((finalText + " " + interim).trim(), !interim);
    };
    rec.onerror = (e) => { error = e.error; };
    rec.onend = () => { if (session === s) session = null; onEnd(finalText.trim(), error); };
    const s = session = { stop: () => rec.stop(), cancelled: false };
    rec.start();
    onStatus && onStatus("listening");
  }

  // Listen once. onText(text) shows words; onEnd(finalText, error) when done.
  function listen(subject, onText, onEnd, onStatus) {
    stopSpeaking();
    if (session) { session.cancelled = true; session.stop(); session = null; }
    if (useWhisper(subject)) { listenWhisper(subject, onText, onEnd, onStatus); return true; }
    if (Rec) { listenBrowser(subject, onText, onEnd, onStatus); return true; }
    return false;
  }

  function stopListening() { if (session) session.stop(); }
  function listening() { return !!session; }
  function setAutoRead(on) { autoRead = on; store("buddy.autoRead", on ? "on" : "off"); if (!on) stopSpeaking(); }
  if (synth && synth.onvoiceschanged !== undefined) synth.onvoiceschanged = () => synth.getVoices();

  return {
    get canListen() { return canRecord || !!Rec; },
    get canSpeak() { return !!synth || config.tts_langs.length > 0; },
    init, listen, stopListening, listening, speak, stopSpeaking,
    get autoRead() { return autoRead; }, setAutoRead, listenLang, textLang, clean, pickVoice,
    get config() { return config; },
  };
})();
