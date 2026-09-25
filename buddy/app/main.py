"""FastAPI app: kid chat (streamed), parent log + uploads, simple password login."""
import asyncio
import hmac
import json
import logging
import secrets
import shutil
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from buddy.books import BOOKS
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

app = FastAPI(title="Buddy Teacher", docs_url=None, redoc_url=None, openapi_url=None)
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
    return {"role": r, "subjects": [{"key": b.subject, "label": b.label} for b in BOOKS.values()]}


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
    a = Ask(question=question[:1000], subject=subject if subject in BOOKS else None,
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
    got = store.get_by({"$and": [{"subject": subject}, {"kind": "explain"}]}, limit=500)
    seen = {}
    for h in got:
        seen[h.meta["chapter"]] = h.meta.get("chapter_title", "")
    return [{"chapter": c, "title": t} for c, t in sorted(seen.items())]


@app.post("/api/quiz")
async def quiz(subject: str = Form(...), chapter: int = Form(...), _: str = Depends(need_kid)):
    from buddy.app.quiz import make_quiz

    return {"questions": await make_quiz(subject, chapter)}


# ---------- parent API ----------

@app.get("/api/log")
def get_log(limit: int = 200, _: str = Depends(need_parent)):
    return {"summary": summary(), "rows": recent(limit)}


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
