import json
from types import SimpleNamespace

from buddy.books import get_book
from buddy.config import get_settings
from buddy.ingest import batch
from buddy.ingest.download import chapter_pdf_path
from buddy.ingest.index import index_chapter, split_text
from buddy.ingest.pdf import load_pages
from buddy.rag import store
from tests.conftest import SAMPLE_RESULT, make_pdf


def put_sample_pdf():
    p = chapter_pdf_path("evs", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    make_pdf(p, ["Plants make their own food.", "Trees give shade and homes to birds."])
    return p


def test_load_pages_text_and_image():
    pages = load_pages(put_sample_pdf())
    assert len(pages) == 2
    assert "Plants make" in pages[0].text
    assert pages[0].jpeg[:2] == b"\xff\xd8"


def test_chapter_request_shape():
    put_sample_pdf()
    params = batch.chapter_params(get_book("evs"), 1)
    assert params["model"] == "claude-sonnet-5"
    content = params["messages"][0]["content"]
    assert sum(1 for b in content if b["type"] == "image") == 2
    assert any("PDF text layer" in b.get("text", "") for b in content)
    fmt = params["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False


def test_image_mode_skips_text_layer():
    p = chapter_pdf_path("hindi", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    make_pdf(p, ["garbled legacy font text"])
    content = batch.chapter_params(get_book("hindi"), 1)["messages"][0]["content"]
    assert not any("PDF text layer" in b.get("text", "") for b in content)


def fake_batch_client(result: dict):
    msg = SimpleNamespace(
        stop_reason="end_turn", model="claude-sonnet-5",
        content=[SimpleNamespace(type="text", text=json.dumps(result))],
        usage=SimpleNamespace(input_tokens=20_000, output_tokens=9_000),
    )
    item = SimpleNamespace(custom_id="evs-ch01",
                           result=SimpleNamespace(type="succeeded", message=msg))
    return SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(
        results=lambda _id: [item])))


def test_collect_index_search_end_to_end():
    put_sample_pdf()
    reports = batch.collect(fake_batch_client(SAMPLE_RESULT), "msgbatch_test")
    assert reports[0]["status"] == "ok"
    # Sonnet 5 batch price: (20k*$2 + 9k*$10)/1M * 0.5
    assert reports[0]["cost_usd"] == round((20_000 * 2 + 9_000 * 10) / 1e6 * 0.5, 4)
    assert batch.cost_history()[0]["id"] == "evs-ch01"
    assert batch.processed_path("evs", 1).exists()

    n = index_chapter("evs", 1)
    assert n == 2 + 2 + 1 + 2 + 1  # text, explain, diagram, qa, vocab
    hits = store.search("what do plants need to make food", subject="evs")
    assert hits and hits[0].meta["subject"] == "evs"
    assert hits[0].meta["cite"].startswith("EVS, Chapter 1, page ")
    # printed page numbers (5, 6) are used, not PDF indexes
    assert {h.meta["page"] for h in hits} <= {5, 6}

    # re-indexing replaces rather than duplicates
    assert index_chapter("evs", 1) == n
    assert store.get_collection().count() == n


def test_gate_requires_first_measurement(capsys):
    import pytest
    from buddy.ingest.__main__ import gate

    with pytest.raises(SystemExit):
        gate([("evs", 1), ("evs", 2)], force=False)
    gate([("evs", 1)], force=False)  # one chapter is always allowed
    gate([("evs", 1), ("evs", 2)], force=True)


def test_split_text_limits():
    chunks = split_text("word " * 1000)
    assert all(len(c) <= 1400 for c in chunks) and len(chunks) > 3


def test_settings_paths():
    s = get_settings()
    assert s.raw_dir.parent == s.data_dir


def test_photo_upload_is_resized_to_jpeg():
    import pymupdf

    from buddy.ingest.pdf import MAX_SIDE, image_to_page

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 3000, 4000), False)
    pix.clear_with(200)
    page = image_to_page(pix.tobytes("jpg"), 1)
    out = pymupdf.Pixmap(page.jpeg)
    assert page.jpeg[:2] == b"\xff\xd8" and max(out.width, out.height) <= MAX_SIDE + 1


def test_add_pdf_and_zip(tmp_path):
    import zipfile

    import pytest

    from buddy.ingest.download import add_pdf, add_zip

    src = tmp_path / "deev103.pdf"
    make_pdf(src, ["hello"])
    assert add_pdf(get_book("evs"), 3, src) == chapter_pdf_path("evs", 3)
    bad = tmp_path / "x.pdf"
    bad.write_text("<html>blocked</html>")
    with pytest.raises(SystemExit):
        add_pdf(get_book("evs"), 4, bad)

    z = tmp_path / "deev1dd.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("deev1ps.pdf", src.read_bytes())       # prelims: skipped
        zf.writestr("deev1dd/deev101.pdf", src.read_bytes())
        zf.writestr("deev102.pdf", src.read_bytes())
    assert add_zip(get_book("evs"), z) == [chapter_pdf_path("evs", 1), chapter_pdf_path("evs", 2)]


def test_download_failure_explains_manual_route(monkeypatch):
    import httpx
    import pytest

    from buddy.ingest import download

    def timeout(*a, **k):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(download.httpx, "stream", timeout)
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as e:
        download.download_chapter(get_book("evs"), 1)
    assert "add-pdf evs 1" in str(e.value) and "deev1dd.zip" in str(e.value)


def test_workspace_header(monkeypatch):
    from buddy import config
    from buddy.llm import claude

    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    config.get_settings.cache_clear()
    claude.sync_client.cache_clear()
    claude.async_client.cache_clear()
    assert claude.sync_client().default_headers["anthropic-workspace-id"] == "wrkspc_test"
    assert claude.async_client().default_headers["anthropic-workspace-id"] == "wrkspc_test"


def test_printed_pages_infers_unnumbered_pages():
    from buddy.ingest.index import printed_pages

    ch1 = [{"pdf_page": i, "printed_page": 0 if i <= 3 else i} for i in range(1, 6)]
    assert printed_pages(ch1) == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}
    ch2 = [{"pdf_page": 1, "printed_page": 0}, {"pdf_page": 2, "printed_page": 0},
           {"pdf_page": 3, "printed_page": 19}, {"pdf_page": 4, "printed_page": 20},
           {"pdf_page": 5, "printed_page": 0}]
    assert printed_pages(ch2) == {1: 17, 2: 18, 3: 19, 4: 20, 5: 21}
    assert printed_pages([{"pdf_page": 1, "printed_page": 0}]) == {1: 1}
