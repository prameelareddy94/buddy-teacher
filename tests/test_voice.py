import io
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from buddy.voice import stt, tts
from tests.test_router import seed


@pytest.fixture
def piper_voices(monkeypatch):
    """Pretend the English Piper voice is downloaded."""
    from buddy.config import get_settings

    d = tts.voices_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{get_settings().piper_voice_en}.onnx").write_bytes(b"x")
    calls = []

    class FakeVoice:
        def synthesize_wav(self, text, w, syn_config=None):
            calls.append((text, syn_config.length_scale))
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
            w.writeframes(b"\0\0" * 2205)

    tts._voice.cache_clear()
    monkeypatch.setattr(tts, "_voice", lambda name: FakeVoice())
    return calls


def test_tts_languages_and_cache(piper_voices):
    assert tts.available_langs() == ["en"]  # no Hindi/Kannada model files
    wav = tts.synthesize("💡 Plants need ____ to make food.")
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getnframes() == 2205
    assert piper_voices[0] == ("Plants need , blank, to make food.", 1.12)
    tts.synthesize("💡 Plants need ____ to make food.")
    assert len(piper_voices) == 1  # second time from the cache
    with pytest.raises(LookupError):
        tts.synthesize("पेड़ हमें छाया देते हैं")  # Hindi voice not downloaded


def test_tts_off(monkeypatch, piper_voices):
    from buddy import config

    monkeypatch.setenv("TTS_ENGINE", "browser")
    config.get_settings.cache_clear()
    assert tts.available_langs() == []


def test_whisper_prompt_uses_book_words():
    seed()  # EVS chapter "Nurturing Nature" with vocabulary "sapling"
    p = stt.prompt_for("evs")
    assert "Nurturing Nature" in p and "sapling" in p and "EVS" in p
    assert stt.lang_for("hindi") == "hi" and stt.lang_for("kannada") == "kn"
    assert stt.lang_for(None) == "en"


def test_transcribe_passes_language_and_prompt(monkeypatch):
    seed()
    seen = {}

    class FakeModel:
        def transcribe(self, path, **kw):
            seen.update(kw)
            seen["data"] = open(path, "rb").read()
            return iter([SimpleNamespace(text=" What is a  sapling? ")]), None

    stt._model.cache_clear()
    monkeypatch.setattr(stt, "_model", lambda: FakeModel())
    assert stt.transcribe(b"webm-bytes", "speech.webm", "evs") == "What is a sapling?"
    assert seen["language"] == "en" and "sapling" in seen["initial_prompt"]
    assert seen["vad_filter"] and seen["data"] == b"webm-bytes"
    with pytest.raises(LookupError):
        stt.transcribe(b"x", "a.webm", "kannada")  # Kannada stays with the browser


def test_prompt_echo_on_silence_is_dropped(monkeypatch):
    class Echo:
        def transcribe(self, path, **kw):
            return iter([SimpleNamespace(text=kw["initial_prompt"])]), None

    monkeypatch.setattr(stt, "_model", lambda: Echo())
    assert stt.transcribe(b"x", "a.webm", None) == ""


def test_voice_endpoints(monkeypatch, piper_voices):
    from buddy.app.main import app

    seed()
    monkeypatch.setattr(stt, "transcribe", lambda data, name, subject: f"heard {subject}")
    c = TestClient(app)
    assert c.get("/api/voice").status_code == 401
    c.post("/login", data={"password": "kid"}, follow_redirects=False)
    cfg = c.get("/api/voice").json()
    assert cfg == {"stt": "whisper", "stt_langs": ["en", "hi"], "tts_langs": ["en"]}
    r = c.post("/api/transcribe", data={"subject": "evs"},
               files={"audio": ("speech.webm", b"abc", "audio/webm")})
    assert r.json() == {"text": "heard evs"}
    r = c.post("/api/tts", data={"text": "Hello there"})
    assert r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    assert c.post("/api/tts", data={"text": "ನಮಸ್ಕಾರ"}).status_code == 404  # browser does Kannada
