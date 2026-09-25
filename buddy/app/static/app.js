const chat = document.getElementById("chat");
const form = document.getElementById("askForm");
const q = document.getElementById("q");
const photo = document.getElementById("photo");
const preview = document.getElementById("preview");
const sendBtn = document.getElementById("send");
let subject = "";
let busy = false;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function scroll() { chat.scrollTop = chat.scrollHeight; }

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
    const b = el("button", "chip" + (s.key === subject ? " on" : ""), s.label);
    b.type = "button";
    b.onclick = () => { subject = s.key; box.querySelectorAll(".chip").forEach(c => c.classList.remove("on")); b.classList.add("on"); };
    box.appendChild(b);
  }
}

// A Buddy bubble with hint shown first and the answer behind a button.
function buddyBubble(reveal = false) {
  const m = el("div", "msg buddy");
  const typing = el("div", "typing", "Buddy is thinking…");
  const hint = el("div", "hint"); hint.hidden = true;
  const answer = el("div", reveal ? "answer" : "answer hidden");
  const source = el("div", "source");
  const actions = el("div", "actions");
  const show = el("button", "secondary", "👀 Show answer"); show.type = "button"; show.hidden = true;
  show.onclick = () => { answer.classList.remove("hidden"); show.hidden = true; scroll(); };
  actions.appendChild(show);
  m.append(typing, hint, answer, source, actions);
  chat.appendChild(m); scroll();
  return {
    update(p, streaming) {
      typing.hidden = !!(p.hint || p.answer) || !streaming;
      if (p.hint) { hint.hidden = false; hint.textContent = "💡 " + p.hint; }
      answer.textContent = p.answer;
      const hidden = answer.classList.contains("hidden");
      if (!streaming && !p.hint) { answer.classList.remove("hidden"); show.hidden = true; }  // nothing to hide behind
      else if (hidden) show.hidden = !(p.hint && p.answer);
      source.textContent = p.source ? "📖 " + p.source : "";
      scroll();
    },
    addActions(id) {
      if (!id) return;
      const b = el("button", "ghost", "🤔 Explain more"); b.type = "button";
      b.onclick = () => { b.disabled = true; ask({ explainMoreOf: id }); };
      const up = el("button", "ghost thumb", "👍"); up.type = "button"; up.title = "This helped";
      const down = el("button", "ghost thumb", "👎"); down.type = "button"; down.title = "This didn't help";
      const vote = async (v) => {
        up.disabled = down.disabled = true;
        (v === "up" ? up : down).classList.add("on");
        const fd = new FormData(); fd.append("id", id); fd.append("vote", v);
        await fetch("/api/feedback", { method: "POST", body: fd });
        if (v === "down") {
          chat.appendChild(el("div", "msg buddy", "Thanks for telling me! 🦉 A grown-up teacher will check this answer. You can also tap 🤔 Explain more."));
          scroll();
        }
      };
      up.onclick = () => vote("up"); down.onclick = () => vote("down");
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
        else if (ev.type === "done") { bubble.update(ev, false); bubble.addActions(ev.id); }
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
    b.onclick = () => { acts.remove(); runQuiz(c.book, c.chapter); };
    acts.appendChild(b);
  }
  m.appendChild(acts); chat.appendChild(m); scroll();
};

async function runQuiz(book, chapter) {
  const fd = new FormData(); fd.append("book", book); fd.append("chapter", chapter);
  const wait = el("div", "msg buddy typing", "Making your quiz…"); chat.appendChild(wait); scroll();
  const { questions } = await (await fetch("/api/quiz", { method: "POST", body: fd })).json();
  wait.remove();
  let i = 0;
  const next = () => {
    if (i >= questions.length) { chat.appendChild(el("div", "msg buddy", "🎉 Quiz done! Great work!")); scroll(); return; }
    const qq = questions[i++];
    const m = el("div", "msg buddy", `Q${i}. ${qq.question}`);
    const hint = el("div", "hint", "💡 " + qq.hint); hint.hidden = true;
    const ans = el("div", "answer hidden", "✅ " + qq.answer);
    const src = el("div", "source", qq.source ? "📖 " + qq.source : "");
    const acts = el("div", "actions");
    const hb = el("button", "ghost", "💡 Hint"); hb.type = "button"; hb.onclick = () => { hint.hidden = false; hb.remove(); scroll(); };
    const ab = el("button", "secondary", "👀 Answer"); ab.type = "button"; ab.onclick = () => { ans.classList.remove("hidden"); ab.remove(); scroll(); };
    const nb = el("button", "", "Next ➡️"); nb.type = "button"; nb.onclick = () => { nb.remove(); next(); };
    acts.append(hb, ab, nb);
    m.append(hint, ans, src, acts); chat.appendChild(m); scroll();
  };
  next();
}

loadMe();
