"""Natural-sounding speech with Piper (runs locally, free). English and Hindi voices;
anything without a voice (Kannada) is left to the tablet's own speech."""
import hashlib
import io
import re
import threading
import wave
from functools import lru_cache
from pathlib import Path

from buddy.config import get_settings

_lock = threading.Lock()  # one synthesis at a time keeps the CPU free for answering


def lang_of(text: str) -> str:
    if re.search(r"[ಀ-೿]", text):
        return "kn"
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    return "en"


def voice_name(lang: str) -> str:
    s = get_settings()
    return {"en": s.piper_voice_en, "hi": s.piper_voice_hi, "kn": s.piper_voice_kn}.get(lang, "")


def voices_dir() -> Path:
    return get_settings().models_dir / "piper"


def _model_path(name: str) -> Path:
    return voices_dir() / f"{name}.onnx"


def installed() -> bool:
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return True


def available_langs() -> list[str]:
    if get_settings().tts_engine != "piper" or not installed():
        return []
    return [lang for lang in ("en", "hi", "kn")
            if voice_name(lang) and _model_path(voice_name(lang)).exists()]


@lru_cache(maxsize=4)
def _voice(name: str):
    from piper import PiperVoice
    return PiperVoice.load(_model_path(name))


def clean(text: str) -> str:
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", text)  # emoji
    text = text.replace("____", ", blank, ").replace("₹", " rupees ")
    text = re.sub(r"[*_#`>|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def synthesize(text: str, lang: str | None = None) -> bytes:
    """WAV bytes for text. Cached on disk, so repeated hints/answers are instant."""
    text = clean(text)
    lang = lang or lang_of(text)
    if lang not in available_langs():
        raise LookupError(f"no Piper voice for {lang}")
    s = get_settings()
    name = voice_name(lang)
    key = hashlib.sha1(f"{name}|{s.tts_speed}|{text}".encode()).hexdigest()
    cache = s.data_dir / "tts_cache" / f"{key}.wav"
    if cache.exists():
        return cache.read_bytes()
    from piper.config import SynthesisConfig

    buf = io.BytesIO()
    with _lock, wave.open(buf, "wb") as w:
        _voice(name).synthesize_wav(text, w, syn_config=SynthesisConfig(
            length_scale=s.tts_speed, noise_scale=0.6, noise_w_scale=0.8))
    data = buf.getvalue()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


def download_voices(log=print) -> None:
    from piper.download_voices import download_voice

    voices_dir().mkdir(parents=True, exist_ok=True)
    for lang in ("en", "hi", "kn"):
        name = voice_name(lang)
        if not name:
            continue
        if _model_path(name).exists():
            log(f"  {name}: already here")
            continue
        try:
            download_voice(name, voices_dir())
            log(f"  {name}: downloaded")
        except Exception as e:  # unknown voice name, network…
            log(f"  {name}: could not download ({e}); that language uses the tablet's voice")
