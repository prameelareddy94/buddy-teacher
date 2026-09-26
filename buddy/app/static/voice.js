// Voice for Buddy: speech-to-text for questions, text-to-speech for answers.
// Uses the browser's own speech engines (Safari/iPad, Chrome/Android), so there is no
// server cost. Recognition needs HTTPS or localhost (Tailscale Serve gives HTTPS).
// Note: browsers may send the recorded audio to Apple/Google to recognise it.
const Voice = (() => {
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;
  const canListen = !!Rec;
  const canSpeak = !!synth;
  let rec = null;
  let unlocked = false;

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { return null; }
  }
  let autoRead = store("buddy.autoRead") !== "off";

  // Language for listening, from the subject she picked.
  function listenLang(subject) {
    return subject === "hindi" ? "hi-IN" : subject === "kannada" ? "kn-IN" : "en-IN";
  }

  // Language for speaking, from the script of the text itself.
  function textLang(text) {
    if (/[ಀ-೿]/.test(text)) return "kn-IN";
    if (/[ऀ-ॿ]/.test(text)) return "hi-IN";
    return "en-IN";
  }

  function pickVoice(lang) {
    const voices = synth.getVoices();
    const base = lang.split("-")[0];
    return voices.find(v => v.lang === lang) ||
           voices.find(v => v.lang.replace("_", "-").startsWith(base + "-")) ||
           (base === "en" ? voices.find(v => v.lang.startsWith("en")) : null) || null;
  }

  function clean(text) {
    return (text || "")
      .replace(/\p{Extended_Pictographic}|️/gu, "")
      .replace(/[*_#`>]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  // iOS only lets pages speak after a tap; speak an empty line on the first tap.
  function unlock() {
    if (unlocked || !canSpeak) return;
    unlocked = true;
    try { synth.speak(new SpeechSynthesisUtterance("")); } catch (e) { /* ignore */ }
  }
  ["pointerdown", "keydown"].forEach(ev => document.addEventListener(ev, unlock, { once: true }));

  // Speak a list of parts in order ([hint], [answer, source] ...). Stops anything playing.
  function speak(parts) {
    if (!canSpeak) return;
    synth.cancel();
    for (const part of [].concat(parts)) {
      const t = clean(part);
      if (!t) continue;
      const u = new SpeechSynthesisUtterance(t);
      u.lang = textLang(t);
      const v = pickVoice(u.lang);
      if (v) u.voice = v;
      u.rate = 0.92;   // a little slower for a young listener
      u.pitch = 1.05;
      synth.speak(u);
    }
  }

  function stopSpeaking() { if (canSpeak) synth.cancel(); }

  // Listen once. onText(text, isFinal) gets live words; onEnd(finalText) when she stops.
  function listen(subject, onText, onEnd) {
    if (!canListen) return false;
    stopSpeaking();
    if (rec) { rec.abort(); rec = null; }
    rec = new Rec();
    rec.lang = listenLang(subject);
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    let finalText = "";
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finalText += r[0].transcript;
        else interim += r[0].transcript;
      }
      onText((finalText + " " + interim).trim(), !interim);
    };
    rec.onerror = (e) => { rec._error = e.error; };
    rec.onend = () => { const err = rec && rec._error; rec = null; onEnd(finalText.trim(), err); };
    rec.start();
    return true;
  }

  function stopListening() { if (rec) rec.stop(); }
  function listening() { return !!rec; }

  function setAutoRead(on) { autoRead = on; store("buddy.autoRead", on ? "on" : "off"); if (!on) stopSpeaking(); }

  if (canSpeak && synth.onvoiceschanged !== undefined) synth.onvoiceschanged = () => synth.getVoices();

  return {
    canListen, canSpeak, listen, stopListening, listening, speak, stopSpeaking,
    get autoRead() { return autoRead; }, setAutoRead, listenLang, textLang, clean,
  };
})();
