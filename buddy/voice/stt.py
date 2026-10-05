"""Speech-to-text with Whisper (faster-whisper, runs locally, free).

Children's speech is hard for generic recognisers. Two things help a lot here:
- the language follows the subject chip (English / Hindi);
- Whisper gets an "initial prompt" with the words from her own books for that subject
  (chapter titles and vocabulary), so book words come out right.
Kannada stays with the tablet's recogniser: Whisper is weak at Kannada.
"""
import re
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

from buddy.config import get_settings

WHISPER_LANGS = {"en", "hi"}
_lock = threading.Lock()
_vocab_cache: dict[str, tuple[float, str]] = {}


def installed() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def enabled() -> bool:
    return get_settings().stt_engine == "whisper" and installed()


def lang_for(subject: str | None) -> str:
    return {"hindi": "hi", "kannada": "kn"}.get(subject or "", "en")


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    s = get_settings()
    return WhisperModel(s.whisper_model, device="cpu", compute_type="int8",
                        download_root=str(s.models_dir / "whisper"))


def vocabulary(subject: str | None, limit: int = 60) -> str:
    """Words from her books for this subject: chapter titles + vocabulary lists."""
    key = subject or "*"
    hit = _vocab_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    from buddy.rag import store

    words: list[str] = []
    where = {"kind": "vocab"} if not subject else {"$and": [{"subject": subject},
                                                            {"kind": "vocab"}]}
    for h in store.get_by(where, limit=200):
        for line in h.text.splitlines()[2:]:  # "<header>", "Word meanings:", "word: meaning"
            w = line.split(":", 1)[0].strip()
            if 1 < len(w) < 30:
                words.append(w)
    where = {"kind": "explain"} if not subject else {"$and": [{"subject": subject},
                                                              {"kind": "explain"}]}
    titles = {h.meta.get("chapter_title", "") for h in store.get_by(where, limit=300)}
    seen, out = set(), []
    for w in [t for t in titles if t] + words:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    text = ", ".join(out[:limit])
    _vocab_cache[key] = (time.time(), text)
    return text


def prompt_for(subject: str | None) -> str:
    from buddy.books import SUBJECTS

    label = SUBJECTS.get(subject or "", "her school subjects")
    vocab = vocabulary(subject)
    p = f"A 9-year-old child in Class 4 asks a question about {label}."
    return f"{p} Words from her books: {vocab}." if vocab else p


def transcribe(audio: bytes, filename: str = "speech.webm", subject: str | None = None) -> str:
    lang = lang_for(subject)
    if lang not in WHISPER_LANGS:
        raise LookupError(f"Whisper not used for {lang}")
    suffix = Path(filename).suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(audio)
        f.flush()
        with _lock:
            segments, _info = _model().transcribe(
                f.name, language=lang, initial_prompt=prompt_for(subject),
                beam_size=5, vad_filter=True, condition_on_previous_text=False,
                temperature=0.0)
            text = " ".join(s.text.strip() for s in segments)
    text = re.sub(r"\s+", " ", text).strip()
    # Whisper sometimes echoes the prompt on silence.
    if text.lower().startswith("a 9-year-old child") or text.lower().startswith("words from"):
        return ""
    return text


def preload(log=print) -> None:
    """Download the Whisper model now (first use would otherwise wait for it)."""
    s = get_settings()
    log(f"  Whisper '{s.whisper_model}' -> {s.models_dir / 'whisper'}")
    _model()
