# Buddy Teacher

A private study helper for a Class 4 CBSE student. It answers only from her own
textbooks and school papers, in simple words, gives a hint first, and always says where
the answer is ("EVS, Chapter 3, page 27"). Personal use only.

| Subject | Book | Source |
|---|---|---|
| English | Santoor (NCERT) | `desa1`, 12 chapters |
| Maths | Maths Mela (NCERT) | `demm1`, 14 chapters |
| EVS | Our Wondrous World (NCERT) | `deev1`, 10 chapters |
| Hindi (3rd lang) | Veena (NCERT) | `dhve1`, 13 chapters, read from page images |
| Kannada (2nd lang) | TBD (likely Karnataka Textbook Society) | drop PDFs in `data/raw/kannada/`, read from page images |

Her **Class 1–3 books** are included too, because questions often go back to basics. These
answers are cited as "EVS (Class 3), Chapter 2, page 14".

| Book key | Class | Book | NCERT code |
|---|---|---|---|
| `evs-c3`, `english-c3`, `maths-c3`, `hindi-c3` | 3 | Our Wondrous World, Santoor, Maths Mela, Veena | `ceev1`, `cesa1`, `cemm1`, `chve1` |
| `english-c2`, `maths-c2`, `hindi-c2` | 2 | Mridang, Joyful Mathematics, Sarangi | `bemr1`, `bejm1`, `bhsr1` |
| `english-c1`, `maths-c1`, `hindi-c1` | 1 | Mridang, Joyful Mathematics, Sarangi | `aemr1`, `aejm1`, `ahsr1` |

Classes 1–2 have no EVS book in the new NCERT syllabus. For most older books the chapter
count isn't recorded, so `download <book> all` fetches chapters until one is missing
(`add-zip` works too). Class 4 books use the bare subject as their key (`evs`, `maths`, ...).

The school's own worksheets, notes and test papers (PDF or photos) can be uploaded from
the parent page. Buddy then answers and makes quizzes in the school's style.

**If her school uses its own books** (many Orchids branches use in-house books alongside
or instead of NCERT), add them from photos. Go to Parent view → 📚 Her school books:
1. **Add book**, e.g. EVS, "EVS book".
2. **Upload chapter:** photograph the chapter's pages in order, then upload them.
3. Claude Sonnet 5 reads the chapter right away, taking a few minutes and costing about
   $0.15 per chapter at the normal API price.
4. School books rank above NCERT in search. **Hide from search** removes an NCERT book she
   doesn't use.

From the command line:
`add-book orchids-evs --subject evs --name "EVS book"`, then
`add-photos orchids-evs 3 ~/Pictures/ch3/*.jpg --sort --now`, and `remove evs`.
iPhone photos must be JPEG: set Settings → Camera → Formats → Most Compatible, or export
them as JPEG.

## How it works

```
 one-time, per chapter                          every question
 ─────────────────────                          ──────────────
 NCERT PDF ─► PyMuPDF (text + page images)      tablet ─► FastAPI ─► bge-m3 search (ChromaDB)
          ─► Claude Sonnet 5, Batch API (50%)                    │
             cleans text, splits topics,                         ▼
             describes diagrams, writes kid                  rules first
             explanations + Q&A (JSON schema)     photo ───────────────► Sonnet 5 (streamed)
          ─► data/processed/<subj>/chNN.json      explain more / Kannada /
          ─► bge-m3 embeddings ─► ChromaDB          multi-topic why/how /
             tagged subject/chapter/page/source     low retrieval score ─► Haiku 4.5 (streamed)
                                                   otherwise ─► Ollama Qwen3 4B, ONE call → JSON
                                                     {hint, answer, confident, pages}
                                                     not confident / no valid page ─► Haiku 4.5
                                                   every question logged with its path + reason
```

A fixed answer that closely matches a new question is used before any of this. Above
`VERIFIED_DIRECT` similarity it's returned directly with no model call, so it's free.

- **Fill in the blanks:** `___`, `...`, or a spoken "dash", "blank", "खाली (स्थान)" or
  "रिक्त स्थान" become a real blank (`____`). The search ignores it, and Buddy answers with
  the missing word and the whole sentence.
- **Hindi and Kannada questions go to Claude Haiku**; small local models are weak in both.
  With `ANSWER_MODE=claude_only`, every question does, at about $0.002 each. Otherwise the
  local model answers only strong book matches (`LOCAL_MIN_SCORE`).
- **Kid rules** (`buddy/kid_rules.py`): answer only from the retrieved passages; simple
  words for a 9-year-old; hint first (the answer sits behind a "Show answer" button); always
  cite; say "This is not in your book. Please ask your teacher!" when the book doesn't
  cover it; stay on study topics.
- **Citations use the printed page numbers.** During ingestion Claude reads the number
  printed on each page, which is not the PDF page index. The server checks every citation
  against the passages it actually retrieved. If the model cites something that wasn't
  retrieved, the server replaces it with a real one.
- **Local JSON:** I added `hint` to the `{answer, confident, pages}` you specified, so the
  local model still answers in a single call.
- **Photos:** Haiku 4.5 first reads the text in the photo (a cheap, fast step) so the books
  can be searched for it. Sonnet 5 then answers with the photo and the passages.
- **The Claude API key** lives only in the server's `.env`. The browser never sees it, and a
  test checks that the served pages don't contain it.

## Run on a laptop

```bash
# 1. Ollama (https://ollama.com/download), then:
ollama pull qwen3:4b
# 2. First run creates .env: add ANTHROPIC_API_KEY and set KID_PASSWORD / PARENT_PASSWORD
./scripts/run_local.sh
./scripts/run_local.sh          # second run starts the server on http://127.0.0.1:8000
```

The first question downloads bge-m3 (~2.3 GB) into the Hugging Face cache.

## Ingest: first chapter, then the rest

```bash
. .venv/bin/activate
python -m buddy.ingest estimate evs 1   # free token count + rough cost, no spend
python -m buddy.ingest run evs 1        # download → batch → wait → save → index → cost report
python -m buddy.ingest costs            # measured $/chapter and projection for all books
```

**If the download times out**, ncert.nic.in is probably unreachable from your network. It
is often slow or blocked outside India. Download the PDF in a browser (a VPN helps) and
import it. `run` then skips the download step:

```bash
python -m buddy.ingest add-pdf evs 1 ~/Downloads/deev101.pdf    # one chapter
python -m buddy.ingest add-zip evs ~/Downloads/deev1dd.zip      # whole book (ncert.nic.in/textbook/pdf/deev1dd.zip)
```

`run` prints the real input/output tokens and dollars for the chapter. It also appends
them to `data/ingest_costs.jsonl`. **Multi-chapter submits are refused until one chapter
has been measured** (`--force` skips this). After checking the cost:

```bash
python -m buddy.ingest submit evs 2-10
python -m buddy.ingest submit english all      # etc.
python -m buddy.ingest collect <batch_id> --wait   # saves, logs cost, indexes
```

Batches usually finish within minutes to an hour (24 h max). You can close the laptop
between `submit` and `collect`. A running server picks up newly indexed chapters
automatically; no restart needed.

Older classes work the same way: `python -m buddy.ingest run evs-c3 1`, then
`submit maths-c1 all`, and so on.

Kannada: put chapter PDFs at `data/raw/kannada/ch01.pdf`, `ch02.pdf`, … Photos can be
combined into a PDF first. Then run the same commands. Hindi and Kannada are read from page
images because their PDF text layers often use legacy font encodings.

School papers: upload them on the parent page, or run
`python -m buddy.ingest upload paper.pdf --subject maths --chapter 3 --kind "test paper"`.

**Rough cost (my estimate, not measured):** an EVS chapter is about 10–12 page images plus
text, roughly 15–20k input tokens and 8–15k output tokens (thinking included). At Sonnet 5
batch prices ($1 / $5 per million) that's about $0.05–0.15 per chapter, or roughly $3–7 for
all ~49 NCERT chapters. The first `run` gives the real number. For questions, a Haiku
answer costs about $0.002 and a photo answer about $0.01. Local answers are free.

## Deploy on Oracle Always Free (Ubuntu 24.04 ARM, A1.Flex 4 OCPU / 24 GB)

```bash
git clone <this repo> && cd buddy-teacher
sudo TS_AUTHKEY=tskey-auth-... bash deploy/setup_oracle.sh   # TS_AUTHKEY optional
sudo nano /opt/buddy-teacher/.env                            # add ANTHROPIC_API_KEY
sudo systemctl restart buddy-teacher
```

The script installs Python deps (CPU PyTorch), Ollama with `qwen3:4b`, and bge-m3. It runs
the app as a systemd service bound to `127.0.0.1:8000` and exposes it **only on your
tailnet** through `tailscale serve`, so no public ports are opened. Install the Tailscale
app on the tablet, sign in, and open the `https://buddy-teacher.<tailnet>.ts.net` URL the
script prints. It's safe to re-run; `data/` and `.env` are kept.

## Parent view

Log in with the parent password to reach `/parent`. It shows every question with its
path (`local`, `claude_haiku`, `claude_sonnet`, `verified`), 👍/👎, the reason, the retrieval score, the
answer, the citation, the latency and the Claude cost, plus totals per path. When the
local model was overruled, hover over the answer to see what it had said.

## Voice

- **Talk instead of typing.** She taps the red 🎤, asks, and the question sends when she
  stops talking. It listens in English, Hindi or Kannada based on the subject chip (English
  under "All").
- **Buddy reads aloud.** It reads the hint first, then the answer and where to find it
  ("You can find this in EVS, Chapter 1, page 5") when she taps "Show answer". Quiz
  questions are read too. 🔊 in the header turns auto-reading on or off, and each answer
  has its own 🔊 button.
- **It uses the browser's own speech engines,** so there's no extra server or cost. It
  works in Safari on iPad/iPhone and in Chrome on Android. The microphone needs HTTPS,
  which Tailscale Serve provides, or `localhost` when testing on the laptop. The browser
  asks for microphone permission the first time. iPad and Chrome may send the audio to
  Apple or Google to recognise it.
- **Voices vary by device.** Kannada text-to-speech may be missing on some tablets; add
  the voice in the tablet's settings (iPad: Settings → Accessibility → Spoken Content →
  Voices).
- The parent view marks spoken questions with 🎤.

## Fixing answers that didn't help

1. **👍 / 👎 on every answer.** A 👎 flags the question. Until it's fixed, similar
   questions skip the local model and go to Claude.
2. **Nightly review (auto-applied).** Every night at `REVIEW_TIME` (02:30 India time by
   default), the server sends the weak answers to Claude Sonnet 5 through the Batch API
   (half price). Weak answers are:
   - ones she marked 👎
   - ones where the local model was overruled
   - ones with a low retrieval score
   - "not in your book" replies

   For each one, Claude sees the question, Buddy's answer and a wider slice of the book,
   and returns one of three verdicts: **fixed**, **correct** or **not_in_book**. A fix is
   accepted only if its citation matches a passage Claude was actually shown.
3. **What an accepted fix does.** It becomes a *verified answer*. The next similar question
   gets it straight away, usually free. Each run stops at `REVIEW_BUDGET_USD` (default $0.50).
4. **Parent view → ✅ Fixes** lists every fix, what Buddy said before, and the reviewer's
   note. **Undo** removes a fix immediately, and the review won't redo that question.
   **Run review now** runs a review straight away with the normal API (full price, about a
   minute).
5. **Parent view → 👎 Needs a look** lets you type the right answer yourself. You can pick a
   book page, leave it as "Note from your parent", or tick "not in her books".

The review runs inside the server, so there's nothing extra to schedule. By hand:
`python -m buddy.review list` shows what's pending, and `python -m buddy.review run
[--direct]` runs it.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
EMBEDDER=hash python -m pytest -q
```

Tests run fully offline. They use a hash embedder, a generated sample PDF, a fake batch
result, a fake Ollama and a fake Claude stream.

## Known limits / things to watch

- **Local model speed on ARM:** Qwen3 4B on 4 Ampere cores should take a few seconds to
  generate, but reading a ~2k-token prompt of passages can take 10–30 s. Watch the `ms`
  column in the parent view. If it's too slow, try `OLLAMA_MODEL=qwen3:1.7b`, lower
  `TOP_K`, or raise `LOW_SCORE_THRESHOLD` so more questions go to Haiku.
- `LOW_SCORE_THRESHOLD=0.45` is a starting guess for bge-m3 cosine scores. Tune it once
  real chapters are in: the parent view shows each question's score.
- `VERIFIED_THRESHOLD=0.80` and `VERIFIED_DIRECT=0.92` decide how close a question must be
  to a fixed one. They're starting guesses for bge-m3. If a fix shows up for a different
  question, raise `VERIFIED_DIRECT`.
- NCERT codes come from ncert.nic.in (2025–26 books). If NCERT renumbers them, edit
  `buddy/books.py`.
