"""Prompts and output schema for the one-time chapter processing step."""
import base64

from buddy.books import Book
from buddy.ingest.pdf import Page

CHAPTER_SYSTEM = """You prepare textbook chapters for a study helper used by a 9-year-old \
in Class 4 (CBSE, Karnataka, India). Books from Classes 1-3 are included for basics. You receive every page of one chapter as an \
image, sometimes with the PDF text layer. Your output is stored and later used to \
answer the child's questions, so it must be faithful to the book: never add facts \
that are not on the pages.

Produce:
- page_numbers: for every PDF page, the page number printed on the page (0 if none \
is printed). The child's citations use printed numbers.
- topics: split the chapter into its natural topics (usually 3-8). For each topic:
  - clean_text: the book's own text for that topic, cleaned of headers, footers, \
page numbers and broken line wraps. Keep the book's language and script exactly \
(Hindi in Devanagari, Kannada in Kannada script). Include text found inside \
pictures, speech bubbles and tables.
  - diagrams: every picture, diagram, map, table or chart that carries information, \
described in 1-3 plain sentences so someone who cannot see it understands what it \
shows. Skip purely decorative art.
  - kid_explanation: a warm, simple explanation of the topic for a 9-year-old, in \
short sentences and everyday words, in English. For Hindi or Kannada chapters, keep \
key words in the original script with their English meaning.
  - qa: 3-8 question/answer pairs a Class 4 teacher would ask on this topic, mixing \
kinds (short answer, fill in the blank, true/false, MCQ with options written in the \
question, long answer, activity). Questions and answers are in the book's language; \
answers must be supported by the pages. hint is one short English nudge that helps \
the child think without giving the answer away. Include the book's own exercise \
questions (\"Let us think\", \"Let us do\" and similar) with answers where the pages \
allow; for open-ended ones, give what a good answer covers.
- vocabulary: new or hard words in the chapter with a simple English meaning.

The PDF text layer can be wrong or garbled (especially for Indian-language fonts); \
when it disagrees with the image, the image is right."""


def _schema() -> dict:
    pages_list = {"type": "array", "items": {"type": "integer"}}
    return {
        "type": "object",
        "properties": {
            "chapter_title": {"type": "string"},
            "page_numbers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pdf_page": {"type": "integer"},
                        "printed_page": {"type": "integer"},
                    },
                    "required": ["pdf_page", "printed_page"],
                    "additionalProperties": False,
                },
            },
            "topics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "pdf_pages": pages_list,
                        "clean_text": {"type": "string"},
                        "diagrams": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "pdf_page": {"type": "integer"},
                                    "description": {"type": "string"},
                                },
                                "required": ["pdf_page", "description"],
                                "additionalProperties": False,
                            },
                        },
                        "kid_explanation": {"type": "string"},
                        "qa": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": ["short", "long", "mcq", "fill_blank",
                                                 "true_false", "activity"],
                                    },
                                    "question": {"type": "string"},
                                    "answer": {"type": "string"},
                                    "hint": {"type": "string"},
                                    "pdf_pages": pages_list,
                                },
                                "required": ["kind", "question", "answer", "hint", "pdf_pages"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["title", "pdf_pages", "clean_text", "diagrams",
                                 "kid_explanation", "qa"],
                    "additionalProperties": False,
                },
            },
            "vocabulary": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "word": {"type": "string"},
                        "meaning": {"type": "string"},
                        "pdf_page": {"type": "integer"},
                    },
                    "required": ["word", "meaning", "pdf_page"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["chapter_title", "page_numbers", "topics", "vocabulary"],
        "additionalProperties": False,
    }


CHAPTER_SCHEMA = _schema()


def image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg).decode("ascii"),
        },
    }


def chapter_content(book: Book, chapter: int, pages: list[Page]) -> list[dict]:
    content: list[dict] = [{
        "type": "text",
        "text": f"Book: {book.label} - \"{book.title}\" (Class {book.grade}, language: {book.language}). "
                f"Chapter {chapter}. {len(pages)} pages follow.",
    }]
    for p in pages:
        header = f"=== PDF page {p.index} ==="
        if book.text_mode == "text" and p.text:
            header += f"\nPDF text layer:\n{p.text}"
        content.append({"type": "text", "text": header})
        content.append(image_block(p.jpeg))
    content.append({"type": "text", "text": "Process this chapter now."})
    return content


UPLOAD_SYSTEM = """You read school material (worksheets, class notes, test papers) \
for a Class 4 student (CBSE, Karnataka) so a study helper can follow the school's \
style. Transcribe faithfully; never invent content. Photos may be tilted or \
handwritten - do your best and mark unreadable parts with [unclear]."""


def upload_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "subject_guess": {"type": "string",
                              "enum": ["evs", "english", "maths", "hindi", "kannada", "unknown"]},
            "chapter_guess": {"type": "integer"},
            "text": {"type": "string"},
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "question": {"type": "string"},
                        "answer": {"type": "string"},
                        "marks": {"type": "string"},
                        "page": {"type": "integer"},
                    },
                    "required": ["kind", "question", "answer", "marks", "page"],
                    "additionalProperties": False,
                },
            },
            "pattern_notes": {"type": "string"},
        },
        "required": ["title", "subject_guess", "chapter_guess", "text", "questions",
                     "pattern_notes"],
        "additionalProperties": False,
    }


UPLOAD_INSTRUCTIONS = """Read this {kind}. Return:
- title: a short name for it.
- subject_guess / chapter_guess (0 if unknown). The parent said: subject={subject}, chapter={chapter}.
- text: all printed and written text, in reading order, original language/script.
- questions: every question, with its kind (e.g. "one word", "fill in the blanks", \
"match the following", "MCQ", "true/false", "short answer (2 marks)", "picture based"), \
the answer if written or clearly marked (else ""), marks if shown (else ""), and the \
page number within this upload.
- pattern_notes: how this school asks questions: section layout, question kinds and \
their order, marks, answer length expected, instruction wording, any answer-format \
rules (e.g. "answer in one sentence", "underline the answer"). This is used to make \
practice quizzes that look like the school's."""
