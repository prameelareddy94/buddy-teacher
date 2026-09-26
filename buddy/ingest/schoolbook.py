"""Her school's own books (e.g. Orchids), added chapter by chapter from photos or scans.

Parent view or CLI: photos -> data/raw/<book>/chNN.pdf -> Claude Sonnet 5 reads the
pages right away (normal API, ~$0.15/chapter) -> indexed. School books rank above NCERT.
"""
import time
from pathlib import Path

from buddy import logs
from buddy.books import get_book
from buddy.ingest.batch import process_now
from buddy.ingest.download import chapter_pdf_path
from buddy.ingest.index import index_chapter
from buddy.ingest.pdf import files_to_pdf


def save_chapter_files(book_key: str, chapter: int, files: list[Path]) -> int:
    """Store the photos/PDFs of one chapter as its chapter PDF. Returns page count."""
    book = get_book(book_key)
    if chapter < 1:
        raise ValueError("chapter must be 1 or more")
    return files_to_pdf(files, chapter_pdf_path(book.key, chapter))


def ingest_now(book_key: str, chapter: int, client=None, job_id: int | None = None) -> dict:
    """Read and index one chapter now. Records progress in the jobs table."""
    if client is None:
        from buddy.llm.claude import sync_client
        client = sync_client()
    if job_id is None:
        job_id = logs.add_job(kind="school_chapter", book=book_key, chapter=chapter)
    logs.update_job(job_id, status="running", detail="Claude is reading the pages…")
    try:
        report = process_now(client, book_key, chapter)
        if report["status"] != "ok":
            raise RuntimeError(report["status"])
        n = index_chapter(book_key, chapter)
    except Exception as e:
        logs.update_job(job_id, status="failed", detail=f"{type(e).__name__}: {e}"[:400],
                        finished=time.time())
        raise
    logs.update_job(job_id, status="done", cost_usd=report["cost_usd"], finished=time.time(),
                    detail=f"{report['topics']} topics, {report['qa_pairs']} Q&A, {n} chunks")
    return {**report, "chunks": n, "job_id": job_id}
