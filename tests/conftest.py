import pymupdf
import pytest

from buddy import config
from buddy.llm import claude
from buddy.rag import embed, store


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EMBEDDER", "hash")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("KID_PASSWORD", "kid")
    monkeypatch.setenv("PARENT_PASSWORD", "parent")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("LOW_SCORE_THRESHOLD", "0.2")
    monkeypatch.setenv("REVIEW_ENABLED", "false")
    monkeypatch.setenv("LOCAL_MIN_SCORE", "0.2")
    store.reset_cache()
    from buddy import books
    for f in (config.get_settings, embed.get_embedder,
              claude.sync_client, claude.async_client):
        f.cache_clear()
    books.load_custom_books()  # fresh data dir: no school books
    yield
    store.reset_cache()
    for f in (config.get_settings,):
        f.cache_clear()


def make_pdf(path, pages):
    doc = pymupdf.open()
    for i, text in enumerate(pages, 1):
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12)
        page.draw_circle((300, 400), 60)  # a "diagram"
        page.insert_text((290, 780), str(i + 4), fontsize=10)  # printed page number
    doc.save(path)


SAMPLE_RESULT = {
    "chapter_title": "Nurturing Nature",
    "page_numbers": [{"pdf_page": 1, "printed_page": 5}, {"pdf_page": 2, "printed_page": 6}],
    "topics": [
        {
            "title": "Plants give us food",
            "pdf_pages": [1],
            "clean_text": "Plants make their own food using sunlight, water and air. "
                          "We get fruits, vegetables and grains from plants.",
            "diagrams": [{"pdf_page": 1, "description": "A mango tree with a child picking fruit."}],
            "kid_explanation": "Plants are like tiny kitchens! They use sunlight to cook food.",
            "qa": [{"kind": "short", "question": "What do plants need to make food?",
                    "answer": "Sunlight, water and air.", "hint": "Think of the sun.",
                    "pdf_pages": [1]}],
        },
        {
            "title": "Caring for trees",
            "pdf_pages": [2],
            "clean_text": "We should water saplings and never cut trees without reason. "
                          "Trees give shade and homes to birds.",
            "diagrams": [],
            "kid_explanation": "Trees are homes for birds, so we look after them.",
            "qa": [{"kind": "true_false", "question": "Trees give homes to birds. True or false?",
                    "answer": "True", "hint": "Where do birds build nests?", "pdf_pages": [2]}],
        },
    ],
    "vocabulary": [{"word": "sapling", "meaning": "a young tree", "pdf_page": 2}],
}
