"""Feedback and fixes.

👎 on an answer flags the question: it is stored as a "flagged" chunk so similar
questions skip the local model, and the nightly review picks it up. A fix (from the
review or typed by a parent) is stored as a "verified" chunk that is searched before
the books. Undo removes it again.
"""
import re
import time

from buddy import logs
from buddy.books import BOOKS, CURRENT_GRADE, SUBJECTS
from buddy.kid_rules import NOT_IN_BOOK
from buddy.rag import store

PARENT_NOTE = "Note from your parent"


def _flag_id(qid: int) -> str:
    return f"flagged-{qid}"


def feedback(qid: int, vote: int) -> None:
    q = logs.get_question(qid)
    if not q:
        raise KeyError(qid)
    logs.update_question(qid, feedback=vote, feedback_ts=time.time())
    if vote < 0 and not q["fix_id"]:
        store.add_chunks([_flag_id(qid)], [q["question"]], [{
            "kind": "flagged", "source": "flagged", "question_id": qid,
            "subject": q["subject"] or "", "cite": "", "page": 0, "pages": "",
        }])
    elif vote > 0:
        store.delete_ids([_flag_id(qid)])


def _meta_for_source(source: str, subject: str | None) -> dict:
    """Book/grade/chapter/page parsed back out of a citation like
    'EVS (Class 3), Chapter 2, page 14'."""
    meta = {"subject": subject or "", "grade": CURRENT_GRADE, "book": subject or "",
            "chapter": 0, "page": 0, "pages": ""}
    for b in BOOKS.values():
        if source.startswith(b.label + ","):
            meta.update(subject=b.subject, grade=b.grade, book=b.key)
            break
    if m := re.search(r"Chapter (\d+)", source):
        meta["chapter"] = int(m.group(1))
    if m := re.search(r"page (\d+)", source):
        meta["page"] = int(m.group(1))
        meta["pages"] = m.group(1)
    return meta


def apply_fix(*, question: str, answer: str, hint: str, source: str, verdict: str,
              origin: str, subject: str | None, question_id: int | None = None,
              note: str = "", run_id: int | None = None) -> int:
    old = logs.get_question(question_id) if question_id else None
    if old and old["fix_id"]:  # replace an earlier fix for the same question
        prev = logs.get_fix(old["fix_id"])
        if prev and prev["status"] == "active":
            undo(prev["id"], reflag=False)
    if verdict == "not_in_book":
        answer, source = NOT_IN_BOOK, ""
    fix_id = logs.add_fix(question_id=question_id, question=question, subject=subject,
                          verdict=verdict, origin=origin, hint=hint, answer=answer,
                          source=source, note=note, run_id=run_id,
                          old_answer=old["answer"] if old else None)
    chunk_id = f"fix-{fix_id}"
    meta = {**_meta_for_source(source, subject), "kind": "verified", "source": "verified",
            "cite": source, "fix_id": fix_id, "hint": hint, "answer": answer,
            "verdict": verdict, "topic": "verified answer", "question": question}
    # Embed the question only, so a new question is matched against it; the answer
    # rides along in metadata (see kid_rules.format_passages).
    store.add_chunks([chunk_id], [question], [meta])
    logs.update_fix(fix_id, chunk_id=chunk_id)
    if question_id:
        logs.update_question(question_id, fix_id=fix_id, reviewed=1, review_verdict=verdict)
        store.delete_ids([_flag_id(question_id)])
    return fix_id


def undo(fix_id: int, reflag: bool = True) -> None:
    fix = logs.get_fix(fix_id)
    if not fix or fix["status"] != "active":
        return
    store.delete_ids([fix["chunk_id"]] if fix["chunk_id"] else [])
    logs.update_fix(fix_id, status="undone")
    if fix["question_id"]:
        # Don't let the nightly review redo the same fix; a parent can still correct it.
        logs.update_question(fix["question_id"], fix_id=None, reviewed=2,
                             review_verdict="undone")
        q = logs.get_question(fix["question_id"])
        if reflag and q and q["feedback"] == -1:
            feedback(q["id"], -1)


def parent_fix(question_id: int, answer: str, hint: str, book: str | None,
               chapter: int | None, page: int | None, not_in_book: bool) -> int:
    from buddy.books import citation

    q = logs.get_question(question_id)
    if not q:
        raise KeyError(question_id)
    if book and book not in BOOKS:
        raise ValueError(f"unknown book {book}")
    subject = BOOKS[book].subject if book else (q["subject"] if q["subject"] in SUBJECTS else None)
    if not_in_book:
        source, verdict = "", "not_in_book"
    else:
        source = citation(book, chapter, page) if book and page else PARENT_NOTE
        verdict = "parent"
    return apply_fix(question=q["question"], answer=answer.strip(), hint=hint.strip(),
                     source=source, verdict=verdict, origin="parent", subject=subject,
                     question_id=question_id)
