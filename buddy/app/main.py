"""FastAPI app: kid chat (streamed), parent log + uploads, simple password login."""
import asyncio
import hmac
import json
import logging
import secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from buddy import fixes, logs, review
from buddy.books import BOOKS, SUBJECTS
from buddy.config import get_settings
from buddy.ingest.pdf import image_to_page
from buddy.llm import ollama
from buddy.logs import recent, summary
from buddy.rag import store
from buddy.router import Ask, answer

log = logging.getLogger("buddy")
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 15 * 1024 * 1024

secret = get_settings().session_secret
if not secret:
    log.warning("SESSION_SECRET not set; logins reset whenever the server restarts")
    secret = secrets.token_hex(32)

def next_review_at(now: datetime, hhmm: str, tz: str) -> datetime:
    """Next time the clock in `tz` shows hh:mm (returned in that zone)."""
    h, m = (int(x) for x in hhmm.split(":"))
    local = now.astimezone(ZoneInfo(tz))
    at = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return at if at > local else at + timedelta(days=1)


async def review_scheduler() -> None:
    while True:
        s = get_settings()
        now = datetime.now(ZoneInfo(s.review_tz))
        wait = (next_review_at(now, s.review_time, s.review_tz) - now).total_seconds()
        await asyncio.sleep(max(wait, 1))
        try:
            result = await asyncio.to_thread(review.run, "batch")
            log.info("nightly review: %s", result)
        except Exception:
            log.exception("nightly review failed")


@asynccontextmanager
async def lifespan(_app):
    s = get_settings()
    task = None
    if s.review_enabled and s.anthropic_api_key:
        task = asyncio.create_task(review_scheduler())
        log.info("nightly review scheduled daily at %s %s", s.review_time, s.review_tz)
    yield
    if task:
        task.cancel()


app = FastAPI(title="Buddy Teacher", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=secret, max_age=60 * 60 * 24 * 30,
                   same_site="strict", https_only=False)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def role(request: Request) -> str | None:
    return request.session.get("role")


def need_kid(request: Request) -> str:
    r = role(request)
    if r not in ("kid", "parent"):
        raise HTTPException(401, "Please log in")
    return r


def need_parent(request: Request) -> str:
    if role(request) != "parent":
        raise HTTPException(403, "Parent login needed")
    return "parent"


# ---------- pages ----------

@app.get("/")
def home(request: Request):
    if not role(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC / "index.html")


@app.get("/parent")
def parent_page(request: Request):
    if role(request) != "parent":
        return RedirectResponse("/login")
    return FileResponse(STATIC / "parent.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    s = get_settings()
    for r, pw in (("parent", s.parent_password), ("kid", s.kid_password)):
        if pw and hmac.compare_digest(password.encode(), pw.encode()):
            request.session["role"] = r
            return RedirectResponse("/parent" if r == "parent" else "/", status_code=303)
    await asyncio.sleep(1.0)  # slow down guessing
    return RedirectResponse("/login?bad=1", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- kid API ----------

@app.get("/api/me")
def me(r: str = Depends(need_kid)):
    return {"role": r, "subjects": [{"key": k, "label": v} for k, v in SUBJECTS.items()]}


@app.post("/api/ask")
async def ask(
    question: str = Form(""),
    subject: str = Form(""),
    explain_more_of: int | None = Form(None),
    image: UploadFile | None = File(None),
    _: str = Depends(need_kid),
):
    img = None
    if image is not None and image.filename:
        raw = await image.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413, "Photo is too big")
        img = (await asyncio.to_thread(image_to_page, raw, 1)).jpeg  # resized JPEG
    if not question.strip() and not img and not explain_more_of:
        raise HTTPException(400, "Ask a question or add a photo")
    a = Ask(question=question[:1000], subject=subject if subject in SUBJECTS else None,
            image=img, image_type="image/jpeg", explain_more_of=explain_more_of)

    async def events():
        try:
            async for ev in answer(a):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception:
            log.exception("answer failed")
            err = {"type": "done", "id": None, "route": "error", "reason": "server_error",
                   "hint": "", "answer": "Oops, something went wrong. Please try again!",
                   "source": ""}
            yield f"data: {json.dumps(err)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chapters")
def chapters(subject: str, _: str = Depends(need_kid)):
    """Ingested chapters for a subject, current class first."""
    got = store.get_by({"$and": [{"subject": subject}, {"kind": "explain"},
                                 {"source": "ncert"}]}, limit=2000)
    seen = {}
    for h in got:
        key = h.meta.get("book", subject)
        seen[(key, h.meta["chapter"])] = (h.meta.get("grade", 4), h.meta.get("chapter_title", ""))
    rows = [{"book": b, "chapter": c, "grade": g, "title": t,
             "label": BOOKS[b].label if b in BOOKS else b}
            for (b, c), (g, t) in seen.items()]
    return sorted(rows, key=lambda r: (-r["grade"], r["chapter"]))


@app.post("/api/quiz")
async def quiz(book: str = Form(...), chapter: int = Form(...), _: str = Depends(need_kid)):
    from buddy.app.quiz import make_quiz

    if book not in BOOKS:
        raise HTTPException(400, "Unknown book")
    return {"questions": await make_quiz(book, chapter)}


@app.post("/api/feedback")
def feedback(id: int = Form(...), vote: str = Form(...), _: str = Depends(need_kid)):
    if vote not in ("up", "down"):
        raise HTTPException(400, "vote must be up or down")
    try:
        fixes.feedback(id, 1 if vote == "up" else -1)
    except KeyError:
        raise HTTPException(404, "No such answer")
    return {"ok": True}


# ---------- parent API ----------

@app.get("/api/log")
def get_log(limit: int = 200, _: str = Depends(need_parent)):
    return {"summary": summary(), "rows": recent(limit)}


@app.get("/api/books")
def books(_: str = Depends(need_parent)):
    return [{"key": b.key, "label": b.label, "title": b.title} for b in BOOKS.values()]


@app.get("/api/review")
def review_state(_: str = Depends(need_parent)):
    s = get_settings()
    return {"flagged": logs.flagged_unfixed(), "fixes": logs.list_fixes(),
            "runs": logs.list_runs(),
            "schedule": {"enabled": s.review_enabled and bool(s.anthropic_api_key),
                         "time": s.review_time, "tz": s.review_tz,
                         "budget_usd": s.review_budget_usd}}


@app.post("/api/review/run")
async def review_now(_: str = Depends(need_parent)):
    """Review pending answers right away (normal API: full price, done in about a minute)."""
    if not review._lock.locked():
        asyncio.get_running_loop().run_in_executor(None, review.run, "direct")
        return {"started": True}
    return {"started": False, "detail": "A review is already running"}


@app.post("/api/fixes/{fix_id}/undo")
def undo_fix(fix_id: int, _: str = Depends(need_parent)):
    if not logs.get_fix(fix_id):
        raise HTTPException(404, "No such fix")
    fixes.undo(fix_id)
    return {"ok": True}


@app.post("/api/correct")
def correct(
    question_id: int = Form(...),
    answer: str = Form(""),
    hint: str = Form(""),
    book: str = Form(""),
    chapter: int = Form(0),
    page: int = Form(0),
    not_in_book: bool = Form(False),
    _: str = Depends(need_parent),
):
    if not not_in_book and not answer.strip():
        raise HTTPException(400, "Write the answer, or tick 'not in the book'")
    try:
        fix_id = fixes.parent_fix(question_id, answer, hint, book or None, chapter or None,
                                  page or None, not_in_book)
    except KeyError:
        raise HTTPException(404, "No such question")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"fix_id": fix_id}


@app.post("/api/upload")
async def upload(
    files: list[UploadFile] = File(...),
    subject: str = Form("unknown"),
    chapter: int = Form(0),
    kind: str = Form("worksheet"),
    _: str = Depends(need_parent),
):
    from buddy.ingest.uploads import ingest_upload

    tmp = Path(tempfile.mkdtemp(prefix="buddy-up-"))
    try:
        paths = []
        for i, f in enumerate(files):
            suffix = Path(f.filename or "").suffix.lower() or ".jpg"
            if suffix not in (".pdf", ".jpg", ".jpeg", ".png", ".webp"):
                raise HTTPException(400, f"Unsupported file type {suffix}")
            data = await f.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, f"{f.filename} is too big")
            p = tmp / f"{i:02d}{suffix}"
            p.write_bytes(data)
            paths.append(p)
        result = await asyncio.to_thread(ingest_upload, paths, subject, chapter, kind)
        dest = get_settings().uploads_dir / result["upload_id"]
        for p, f in zip(paths, files):
            shutil.copy(p, dest / f"{p.stem}-{Path(f.filename or 'file').name}")
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/api/health")
async def health():
    return JSONResponse({"ok": True, "ollama": await ollama.is_up(),
                         "chunks": store.get_collection().count()})
