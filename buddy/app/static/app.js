const chat = document.getElementById("chat");
const form = document.getElementById("askForm");
const q = document.getElementById("q");
const photo = document.getElementById("photo");
const preview = document.getElementById("preview");
const sendBtn = document.getElementById("send");
let subject = "";
let busy = false;
let via = "typed";   // "voice" when the question came from the 🎤

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function scroll() { chat.scrollTop = chat.scrollHeight; }

const SUBJECT_LOOK = {
  "": { ic: "✨", c: "#ffd76a" }, evs: { ic: "🌱", c: "#4fe0b6" }, english: { ic: "📘", c: "#52c7ff" },
  maths: { ic: "🔢", c: "#ff9f45" }, hindi: { ic: "अ", c: "#ff5d8f" }, kannada: { ic: "ಕ", c: "#a78bff" },
};

function dots() {
  const t = el("div", "typing");
  t.append(el("span"), el("span"), el("span"));
  return t;
}

// A little burst of stars and confetti from an element (👍, quiz finished).
function confetti(from) {
  const r = from.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const bits = ["⭐", "✨", "🎉", "🌟", "💫", "🎈"];
  for (let i = 0; i < 16; i++) {
    const c = el("span", "confetti", bits[i % bits.length]);
    const a = Math.random() * Math.PI * 2, d = 60 + Math.random() * 120;
    c.style.left = x + "px"; c.style.top = y + "px";
    c.style.setProperty("--dx", Math.cos(a) * d + "px");
    c.style.setProperty("--dy", Math.sin(a) * d - 40 + "px");
    c.style.setProperty("--rot", (Math.random() * 720 - 360) + "deg");
    document.body.appendChild(c);
    setTimeout(() => c.remove(), 1200);
  }
}

// Buddy blinks now and then.
const owl = document.getElementById("owl");
if (owl) setInterval(() => { owl.classList.add("blink"); setTimeout(() => owl.classList.remove("blink"), 300); }, 5000);

// Same parsing as the server: HINT: / ANSWER: / SOURCE:
function parseSections(text) {
  const out = { hint: "", answer: "", source: "" };
  let key = null;
  for (const raw of text.split("\n")) {
    let line = raw.trim();
    for (const k of Object.keys(out)) {
      if (line.toUpperCase().startsWith(k.toUpperCase() + ":")) { key = k; line = line.slice(k.length + 1).trim(); break; }
    }
    if (key) out[key] += (out[key] ? "\n" : "") + line;
    else if (line) out.answer += (out.answer ? "\n" : "") + line;
  }
  return out;
}

async function loadMe() {
  const r = await fetch("/api/me");
  if (r.status === 401) { location.href = "/login"; return; }
  const me = await r.json();
  if (me.role === "parent") document.getElementById("parentLink").hidden = false;
  const box = document.getElementById("subjects");
  const all = [{ key: "", label: "All" }, ...me.subjects];
  for (const s of all) {
    const look = SUBJECT_LOOK[s.key] || { ic: "⭐", c: "#a78bff" };
    const b = el("button", "chip" + (s.key === subject ? " on" : ""));
    b.style.setProperty("--c", look.c);
    b.append(el("span", "ic", look.ic), el("span", "", s.label));
    b.type = "button";
    b.onclick = () => { subject = s.key; box.querySelectorAll(".chip").forEach(c => c.classList.remove("on")); b.classList.add("on"); };
    box.appendChild(b);
  }
}

// A Buddy bubble with hint shown first and the answer behind a button.
function buddyBubble(reveal = false, mount = null) {
  const m = el("div", "msg buddy");
  const typing = dots();
  const hint = el("div", "hint"); hint.hidden = true;
  const answer = el("div", reveal ? "answer" : "answer hidden");
  const source = el("div", "source");
  const actions = el("div", "actions");
  const show = el("button", "secondary", "👀 Show answer"); show.type = "button"; show.hidden = true;
  let last = { hint: "", answer: "", source: "" };
  const answerParts = () => [last.answer, last.source ? "You can find this in " + last.source : ""];
  show.onclick = () => {
    answer.classList.remove("hidden"); show.hidden = true; scroll();
    if (Voice.autoRead) Voice.speak(answerParts());
  };
  actions.appendChild(show);
  m.append(typing, hint, answer, source, actions);
  if (mount) mount(m); else { chat.appendChild(m); scroll(); }
  return {
    update(p, streaming) {
      last = p;
      typing.hidden = !!(p.hint || p.answer) || !streaming;
      if (p.hint) { hint.hidden = false; hint.textContent = "💡 " + p.hint; }
      answer.textContent = p.answer;
      const hidden = answer.classList.contains("hidden");
      if (!streaming && !p.hint) { answer.classList.remove("hidden"); show.hidden = true; }  // nothing to hide behind
      else if (hidden) show.hidden = !(p.hint && p.answer);
      source.textContent = p.source ? "📖 " + p.source : "";
      scroll();
    },
    readAloud() {
      const shown = !answer.classList.contains("hidden");
      Voice.speak(shown ? [last.hint, ...answerParts()] : [last.hint || last.answer]);
    },
    addActions(id, feedback = null) {
      if (Voice.canSpeak) {
        const say = el("button", "ghost thumb", "🔊"); say.type = "button"; say.title = "Read it to me";
        say.onclick = () => this.readAloud();
        actions.appendChild(say);
      }
      if (!id) return;
      const b = el("button", "ghost", "🤔 Explain more"); b.type = "button";
      b.onclick = () => { b.disabled = true; ask({ explainMoreOf: id }); };
      const up = el("button", "ghost thumb", "👍"); up.type = "button"; up.title = "This helped";
      const down = el("button", "ghost thumb", "👎"); down.type = "button"; down.title = "This didn't help";
      const vote = async (v) => {
        up.disabled = down.disabled = true;
        (v === "up" ? up : down).classList.add("on");
        if (v === "up") confetti(up);
        const fd = new FormData(); fd.append("id", id); fd.append("vote", v);
        await fetch("/api/feedback", { method: "POST", body: fd });
        if (v === "down") {
          chat.appendChild(el("div", "msg buddy", "Thanks for telling me! 🦉 A grown-up teacher will check this answer. You can also tap 🤔 Explain more."));
          scroll();
        }
      };
      up.onclick = () => vote("up"); down.onclick = () => vote("down");
      if (feedback) {  // already voted (from history)
        up.disabled = down.disabled = true;
        (feedback > 0 ? up : down).classList.add("on");
      }
      actions.append(b, up, down);
    },
  };
}

async function ask({ text = "", file = null, explainMoreOf = null } = {}) {
  if (busy) return;
  busy = true; sendBtn.disabled = true;
  if (!explainMoreOf) {
    const me = el("div", "msg me", text);
    if (file) { const img = el("img"); img.src = URL.createObjectURL(file); me.appendChild(img); }
    chat.appendChild(me);
  } else {
    chat.appendChild(el("div", "msg me", "Explain more, please 🙏"));
  }
  const bubble = buddyBubble(!!explainMoreOf);
  const fd = new FormData();
  fd.append("question", text);
  fd.append("subject", subject);
  fd.append("via", via);
  via = "typed";
  if (explainMoreOf) fd.append("explain_more_of", explainMoreOf);
  if (file) fd.append("image", file);
  let raw = "";
  try {
    const r = await fetch("/api/ask", { method: "POST", body: fd });
    if (r.status === 401) { location.href = "/login"; return; }
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!line.startsWith("data: ")) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.type === "meta") raw = "";           // (re)started, e.g. after a local fallback
        else if (ev.type === "delta") { raw += ev.text; bubble.update(parseSections(raw), true); }
        else if (ev.type === "done") {
          bubble.update(ev, false); bubble.addActions(ev.id);
          if (Voice.autoRead) bubble.readAloud();
        }
      }
    }
  } catch (e) {
    bubble.update({ answer: "Oops! " + e.message, hint: "", source: "" }, false);
  } finally {
    busy = false; sendBtn.disabled = false;
  }
}

form.onsubmit = (e) => {
  e.preventDefault();
  const text = q.value.trim();
  const file = photo.files[0] || null;
  if (!text && !file) return;
  q.value = ""; photo.value = ""; preview.innerHTML = "";
  ask({ text, file });
};
q.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
photo.onchange = () => {
  preview.innerHTML = "";
  if (photo.files[0]) { const img = el("img"); img.src = URL.createObjectURL(photo.files[0]); preview.appendChild(img); }
};

// ---- Quiz ----
document.getElementById("quizBtn").onclick = async () => {
  if (!subject) { chat.appendChild(el("div", "msg buddy", "Pick a subject first (tap one above) 🙂")); scroll(); return; }
  const chs = await (await fetch("/api/chapters?subject=" + subject)).json();
  if (!chs.length) { chat.appendChild(el("div", "msg buddy", "I don't have any chapters for this subject yet.")); scroll(); return; }
  const m = el("div", "msg buddy", "Which chapter? ");
  const acts = el("div", "actions");
  for (const c of chs) {
    const prefix = c.grade === 4 ? "" : `Class ${c.grade} · `;
    const b = el("button", "ghost", `${prefix}${c.chapter}. ${c.title}`); b.type = "button";
    b.onclick = () => { acts.remove(); m.textContent = `🎯 Quiz time: ${prefix}${c.chapter}. ${c.title}`; runQuiz(c.book, c.chapter); };
    acts.appendChild(b);
  }
  m.appendChild(acts); chat.appendChild(m); scroll();
};

async function runQuiz(book, chapter) {
  const fd = new FormData(); fd.append("book", book); fd.append("chapter", chapter);
  const wait = el("div", "msg buddy typing-msg", "Making your quiz… "); wait.appendChild(dots()); chat.appendChild(wait); scroll();
  let questions = [];
  try { ({ questions } = await (await fetch("/api/quiz", { method: "POST", body: fd })).json()); } catch (e) { /* below */ }
  wait.remove();
  if (!questions || !questions.length) { chat.appendChild(el("div", "msg buddy", "I couldn't make a quiz for this chapter yet 😕")); scroll(); return; }
  const say = (t) => { if (Voice.autoRead) Voice.speak(t); };
  let i = 0, score = 0, stars = 0;

  async function check(qq, given, tryNo) {
    const f = new FormData();
    Object.entries({ book, chapter, kind: qq.kind, question: qq.question, expected: qq.answer, given, try_no: tryNo })
      .forEach(([k, v]) => f.append(k, v));
    try { return await (await fetch("/api/quiz/answer", { method: "POST", body: f })).json(); }
    catch (e) { return { verdict: "no", feedback: "Hmm, I couldn't check that. Try again?" }; }
  }

  const next = () => {
    if (i >= questions.length) return finish();
    const qq = questions[i++];
    const m = el("div", "msg buddy quiz-card");
    m.append(el("div", "quiz-progress", `Question ${i} of ${questions.length}`), el("div", "quiz-q", qq.question));
    const hint = el("div", "hint", "💡 " + (qq.hint || "Think about what you read in the chapter.")); hint.hidden = true;
    const feedback = el("div", "quiz-feedback"); feedback.hidden = true;
    const src = el("div", "source", qq.source ? "📖 " + qq.source : ""); src.hidden = true;
    const acts = el("div", "actions");
    let tries = 0, done = false;

    const showResult = (ok, partly, text) => {
      feedback.hidden = false;
      feedback.className = "quiz-feedback " + (ok ? "good" : partly ? "partly" : "bad");
      feedback.textContent = text;
      scroll();
    };
    const finishQuestion = (ok) => {
      done = true;
      if (ok) { score++; if (tries === 1) stars++; }
      src.hidden = false;
      const nb = el("button", "", i < questions.length ? "Next ➡️" : "See my score 🏆"); nb.type = "button";
      nb.onclick = () => { nb.remove(); next(); };
      acts.replaceChildren(nb);
      scroll();
    };
    const handle = async (given, btn) => {
      if (done) return;
      tries++;
      const r = await check(qq, given, tries);
      if (r.verdict === "yes") {
        if (btn) btn.classList.add("right");
        showResult(true, false, "✅ " + (r.feedback || "Great job!") + (qq.explanation ? " " + qq.explanation : ""));
        confetti(btn || feedback); say(r.feedback || "Great job!");
        finishQuestion(true);
      } else if (tries < 2) {
        if (btn) { btn.classList.add("wrong"); btn.disabled = true; }
        hint.hidden = false;
        showResult(false, r.verdict === "partly", (r.verdict === "partly" ? "🙂 " : "🤔 ") +
          (r.feedback || "Almost!") + " Try again!");
        say((r.feedback || "Almost!") + " Here is a hint. " + (qq.hint || ""));
      } else {
        if (btn) btn.classList.add("wrong");
        const right = m.querySelectorAll(".opt");
        right.forEach(o => { if (o.dataset.value === qq.answer) o.classList.add("right"); o.disabled = true; });
        showResult(false, false, `💛 Good try! The answer is: ${qq.answer}.` + (qq.explanation ? " " + qq.explanation : ""));
        say(`Good try! The answer is ${qq.answer}. ${qq.explanation || ""}`);
        finishQuestion(false);
      }
    };

    if (qq.options && qq.options.length) {
      const grid = el("div", "opts");
      qq.options.forEach((o, k) => {
        const b = el("button", "opt", o); b.type = "button"; b.dataset.value = o;
        b.style.setProperty("--k", k);
        b.onclick = () => handle(o, b);
        grid.appendChild(b);
      });
      m.append(grid);
    } else {
      const row = el("form", "quiz-answer");
      const input = el("input"); input.placeholder = "Type or tap 🎤 to say your answer"; input.enterKeyHint = "done";
      const micB = el("button", "mic-mini", "🎤"); micB.type = "button"; micB.title = "Say it";
      const go = el("button", "secondary", "Check ✔"); go.type = "submit";
      micB.hidden = !Voice.canListen;
      micB.onclick = () => {
        if (Voice.listening()) { Voice.stopListening(); return; }
        Voice.listen(subject, (t) => { input.value = t; }, (t) => {
          micB.classList.remove("on");
          if (t) { input.value = t; row.requestSubmit(); }
        }, (st) => { micB.classList.toggle("on", st === "listening"); });
      };
      row.onsubmit = async (e) => { e.preventDefault(); if (!input.value.trim()) return; go.disabled = true; await handle(input.value); go.disabled = false; if (!done) { input.value = ""; input.focus(); } };
      row.append(input, micB, go);
      m.append(row);
    }
    const hb = el("button", "ghost", "💡 Hint"); hb.type = "button";
    hb.onclick = () => { hint.hidden = false; hb.remove(); scroll(); say(qq.hint); };
    acts.append(hb);
    m.append(hint, feedback, src, acts);
    chat.appendChild(m); scroll();
    say([`Question ${i}. ${qq.question}`, ...(qq.options || [])]);
  };

  const finish = () => {
    const n = questions.length;
    const card = el("div", "msg buddy quiz-score");
    const starRow = "⭐".repeat(stars) + "☆".repeat(Math.max(0, n - stars));
    const cheer = score === n ? "Perfect! You're a superstar! 🌟" : score >= n / 2 ? "Well done! Keep it up! 💪" : "Good practice! Let's read the chapter again and try once more 📖";
    card.append(el("div", "big-score", `${score} / ${n}`), el("div", "stars", starRow), el("div", "", cheer));
    const again = el("button", "secondary", "🔁 Another quiz"); again.type = "button";
    again.onclick = () => { again.remove(); runQuiz(book, chapter); };
    const acts = el("div", "actions"); acts.append(again); card.append(acts);
    chat.appendChild(card); scroll();
    if (score >= n / 2) confetti(card);
    say(`You got ${score} out of ${n}! ${cheer}`);
  };
  next();
}

// ---- Voice ----
const mic = document.getElementById("mic");
const readBtn = document.getElementById("readBtn");
const placeholder = q.placeholder;

function setupVoice() {
  if (Voice.canListen) {
    mic.hidden = false;
    mic.onclick = () => {
      if (Voice.listening()) { Voice.stopListening(); return; }
      if (busy) return;
      const before = q.value;
      const started = Voice.listen(subject, (text) => {
        q.value = text;
      }, (finalText, err) => {
        mic.classList.remove("on", "thinking");
        q.placeholder = placeholder;
        if (finalText) {
          q.value = finalText;
          via = "voice";
          form.requestSubmit();
          return;
        }
        q.value = before;
        let msg = null;
        if (err === "not-allowed" || err === "service-not-allowed") {
          msg = "I can't hear you yet. Ask a grown-up to allow the microphone for this page 🎤";
        } else if (err === "no-speech") {
          msg = "I didn't hear anything. Tap 🎤 and speak a little louder 🙂";
        } else if (err === "network") {
          msg = "I couldn't understand that recording. Please try again 🎤";
        }
        if (msg) { chat.appendChild(el("div", "msg buddy", msg)); scroll(); }
      }, (status) => {
        if (status === "listening") { mic.classList.add("on"); q.placeholder = "Listening… 👂"; }
        if (status === "thinking") { mic.classList.remove("on"); mic.classList.add("thinking"); q.placeholder = "Writing down what you said…"; }
      });
      if (started) mic.classList.add("on");
    };
  }
  if (Voice.canSpeak) {
    readBtn.hidden = false;
    const paint = () => { readBtn.textContent = Voice.autoRead ? "🔊" : "🔇"; readBtn.title = Voice.autoRead ? "Reading answers aloud (tap to stop)" : "Tap to read answers aloud"; };
    readBtn.onclick = () => { Voice.setAutoRead(!Voice.autoRead); paint(); };
    paint();
  }
}
Voice.init().then(setupVoice);

// ---- History: her earlier questions stay on screen ----
const historyBox = el("div", "history");
chat.insertBefore(historyBox, chat.firstElementChild ? chat.firstElementChild.nextSibling : null);
let oldestId = null;

function dayLabel(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" });
}

function renderHistoryItem(r, mount) {
  const me = el("div", "msg me past", (r.had_image ? "📷 " : "") + (r.via === "voice" ? "🎤 " : "") + r.question);
  mount(me);
  const bubble = buddyBubble(true, mount);
  bubble.update({ hint: r.hint || "", answer: r.answer || "", source: r.source || "" }, false);
  bubble.addActions(r.id, r.feedback);
}

async function loadHistory() {
  const url = "/api/history?limit=20" + (oldestId ? "&before_id=" + oldestId : "");
  let data;
  try { data = await (await fetch(url)).json(); } catch (e) { return; }
  if (!data.items || !data.items.length) return;
  const items = data.items.slice().reverse();          // oldest first
  oldestId = data.items[data.items.length - 1].id;
  const frag = document.createDocumentFragment();
  let lastDay = null;
  for (const r of items) {
    const day = dayLabel(r.ts);
    if (day !== lastDay) { frag.appendChild(el("div", "day", day)); lastDay = day; }
    renderHistoryItem(r, (node) => frag.appendChild(node));
  }
  const firstOld = !historyBox.firstChild;
  const keep = chat.scrollHeight - chat.scrollTop;    // stay in place when adding older ones
  const moreBtn = historyBox.querySelector(".more");
  if (moreBtn) moreBtn.remove();
  // the newer block starts with the same day the older block ends with: keep one label
  const firstLabel = historyBox.querySelector(".day");
  if (firstLabel && firstLabel === historyBox.firstElementChild && firstLabel.textContent === lastDay) {
    firstLabel.remove();
  }
  historyBox.insertBefore(frag, historyBox.firstChild);
  if (data.more) {
    const b = el("button", "ghost more", "⬆️ Show earlier questions"); b.type = "button";
    b.onclick = () => loadHistory();
    historyBox.insertBefore(b, historyBox.firstChild);
  }
  chat.style.scrollBehavior = "auto";               // jump, don't glide through history
  if (firstOld) {
    historyBox.appendChild(el("div", "day now", "Now"));
    scroll();
  } else {
    chat.scrollTop = chat.scrollHeight - keep;
  }
  requestAnimationFrame(() => { chat.style.scrollBehavior = ""; });
}

loadMe();
loadHistory();
