"""Ingestion CLI.

  python -m buddy.ingest run evs 1          # end-to-end for one chapter (download,
                                            # batch, wait, save, index, cost report)
  python -m buddy.ingest estimate evs 1     # token count + projected cost, no spend
  python -m buddy.ingest costs              # measured costs + projection for all books
  python -m buddy.ingest submit evs 2-10    # rest of a book (after the first is measured)
  python -m buddy.ingest collect <batch_id> [--wait]
  python -m buddy.ingest download evs all
  python -m buddy.ingest add-pdf evs 1 ~/Downloads/deev101.pdf   # if ncert.nic.in is unreachable
  python -m buddy.ingest add-zip evs ~/Downloads/deev1dd.zip     # whole book at once
  python -m buddy.ingest index evs 1-10
  python -m buddy.ingest upload file.pdf --subject maths --chapter 3 --kind worksheet

Books are named by key: the Class 4 book is the bare subject (evs, english, maths,
hindi, kannada); older classes add the class: evs-c3, english-c1, maths-c2 ...

Her school's own books (e.g. Orchids), from phone photos:
  python -m buddy.ingest add-book orchids-evs --subject evs --name "EVS (school book)"
  python -m buddy.ingest add-photos orchids-evs 3 ~/Pictures/evs-ch3/*.jpg --now
  python -m buddy.ingest remove evs          # take a book out of search (e.g. NCERT EVS)

Orchids e-books from the portal's e-book listing (save the JSON per subject):
  python -m buddy.ingest import-orchids english.json          # shows the plan
  python -m buddy.ingest import-orchids english.json --go     # download + split
  python -m buddy.ingest submit-all                           # read all new chapters

Her school's e-books (page images by e-book id), whole book at once:
  python -m buddy.ingest fetch-ebook orchids-evs 1749
  python -m buddy.ingest submit orchids-evs all   # then collect as usual
"""
import argparse
import sys
from pathlib import Path

from buddy.books import BOOKS, add_school_book, get_book, load_custom_books
from buddy.config import INGEST_MODEL, cost_usd, get_settings
from buddy.ingest import batch
from buddy.ingest.download import add_pdf, add_zip, available_chapters, download_chapter
from buddy.ingest.index import index_chapter


def client():
    from buddy.llm.claude import sync_client

    try:
        return sync_client()
    except RuntimeError as e:
        sys.exit(str(e))


def parse_chapters(book_key: str, spec: str) -> list[int]:
    if spec == "all":
        chapters = available_chapters(get_book(book_key), fetch=True)
        if not chapters:
            sys.exit(f"No chapters found for {book_key}. Add PDFs with add-pdf / add-zip.")
        return chapters
    out: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def print_reports(reports: list[dict]) -> None:
    for r in reports:
        if r["status"] != "ok":
            print(f"  {r['id']}: FAILED ({r['status']}) {r.get('detail', '')}")
            continue
        print(f"  {r['id']}: {r['topics']} topics, {r['qa_pairs']} Q&A, "
              f"in={r['input_tokens']:,} out={r['output_tokens']:,} tok, ${r['cost_usd']:.4f}")


def cmd_download(a):
    for ch in parse_chapters(a.book, a.chapters):
        print(f"{a.book} ch{ch:02d} -> {download_chapter(get_book(a.book), ch, a.force)}")


def cmd_add_pdf(a):
    print(f"{a.book} ch{int(a.chapter):02d} -> "
          f"{add_pdf(get_book(a.book), int(a.chapter), Path(a.file))}")


def cmd_add_zip(a):
    for p in add_zip(get_book(a.book), Path(a.file)):
        print(f"{a.book} -> {p}")


def cmd_estimate(a):
    c = client()
    for ch in parse_chapters(a.book, a.chapters):
        download_chapter(get_book(a.book), ch)
        n = batch.count_input_tokens(c, batch.chapter_params(get_book(a.book), ch))
        guess_out = 12_000  # rough; replaced by the measured number after the first run
        print(f"{a.book} ch{ch:02d}: {n:,} input tokens -> "
              f"~${cost_usd(INGEST_MODEL, n, guess_out, batch=True):.3f} "
              f"(assuming ~{guess_out:,} output tokens incl. thinking, batch price)")


def gate(items: list[tuple[str, int]], force: bool) -> None:
    if len(items) > 1 and not batch.cost_history() and not force:
        sys.exit("No chapter has been measured yet. Run ONE chapter first "
                 "(python -m buddy.ingest run evs 1), check `costs`, then submit the rest. "
                 "Use --force to skip this check.")


def cmd_submit(a):
    items = [(a.book, ch) for ch in parse_chapters(a.book, a.chapters)]
    gate(items, a.force)
    ready, failed = [], []
    for s, ch in items:
        try:
            download_chapter(get_book(s), ch)
            ready.append((s, ch))
        except SystemExit as e:  # keep going; report at the end
            failed.append((ch, str(e).splitlines()[0]))
    for ch, why in failed:
        print(f"  skipped ch{ch:02d}: {why}")
    if not ready:
        book = get_book(a.book)
        sys.exit("Nothing downloaded. ncert.nic.in isn't reachable right now: try again later, "
                 "or download the whole book in a browser and import it:\n"
                 f"  https://ncert.nic.in/textbook/pdf/{book.ncert_code}dd.zip\n"
                 f"  python -m buddy.ingest add-zip {book.key} ~/Downloads/{book.ncert_code}dd.zip")
    bid = batch.submit(client(), ready)
    print(f"Submitted batch {bid} with {len(ready)} chapter(s).")
    print(f"Collect later with: python -m buddy.ingest collect {bid} --wait")
    if failed:
        chs = ",".join(str(ch) for ch, _ in failed)
        print(f"Re-run for the skipped ones later: python -m buddy.ingest submit {a.book} {chs}")


def cmd_collect(a):
    c = client()
    if a.wait:
        batch.wait(c, a.batch_id)
    reports = batch.collect(c, a.batch_id)
    print_reports(reports)
    for r in reports:
        if r["status"] == "ok" and not a.no_index:
            n = index_chapter(r["book"], r["chapter"])
            print(f"  indexed {r['id']}: {n} chunks")


def cmd_add_book(a):
    try:
        b = add_school_book(a.key, a.subject, a.name, grade=a.grade, title=a.title or "")
    except ValueError as e:
        sys.exit(str(e))
    print(f"Added school book {b.key}: {b.label} ({b.subject}, Class {b.grade}). "
          f"Now add chapters: python -m buddy.ingest add-photos {b.key} 1 PHOTOS... --now")


def cmd_add_photos(a):
    from buddy.ingest.schoolbook import ingest_now, save_chapter_files

    files = sorted(Path(f).expanduser() for f in a.files) if a.sort else \
        [Path(f).expanduser() for f in a.files]
    n = save_chapter_files(a.book, int(a.chapter), files)
    print(f"{a.book} ch{int(a.chapter):02d}: {n} pages saved")
    if a.now:
        client()  # check the key first
        r = ingest_now(a.book, int(a.chapter))
        print(f"  {r['topics']} topics, {r['qa_pairs']} Q&A, {r['chunks']} chunks, "
              f"${r['cost_usd']:.4f}")
    else:
        print(f"  Process it with: python -m buddy.ingest run {a.book} {a.chapter} [--now]")


def cmd_fetch_ebook(a):
    from buddy.ingest.ebook import PAGE_URL, import_ebook

    if a.split and len(a.ebook_ids) > 1:
        sys.exit("--split works with one e-book id at a time")
    book = get_book(a.book)
    if not book.is_school:
        sys.exit(f"{a.book} is an NCERT book. Add a school book first, e.g.\n"
                 f"  python -m buddy.ingest add-book orchids-{book.subject} "
                 f"--subject {book.subject} --name \"{book.label} book\"")
    taken: set[int] = set()
    for eid in a.ebook_ids:
        rows = import_ebook(a.book, eid, split=a.split, url_template=a.url or PAGE_URL,
                            taken=taken)
        print(f"E-book {eid} -> {len(rows)} chapters:")
        for r in rows:
            print(f"  ch{r['chapter']:02d}  pages {r['pages']:>9}  {r['title']}")
    print("Check the split above (the page images are in data/raw/"
          f"{a.book}/ebooks/). If it's wrong, re-run with --split \"1:5,2:17,...\".")
    print(f"Then: python -m buddy.ingest submit {a.book} all   (batch, half price)")


def cmd_import_orchids(a):
    from buddy.ingest import orchids

    rows, used = [], {}
    for f in a.listing:
        rows += orchids.plan(orchids.load_listing(Path(f)), used)
    if a.only:
        rows = [r for r in rows if r.get("key") in a.only]
    total = sum(r["entry"].get("file_size") or 0 for r in rows if not r["skip"])
    print(f"{'id':>6}  {'size':>7}  {'book key':28} name   (newest copy of each title)")
    for r in rows:
        e = r["entry"]
        size = f"{(e.get('file_size') or 0) / 1e6:.0f} MB"
        if r["skip"]:
            print(f"{e.get('id', ''):>6}  {size:>7}  SKIP ({r['skip']}): {e.get('book_name')}")
        else:
            print(f"{e['id']:>6}  {size:>7}  {r['key']:28} {r['name']}")
    print(f"{sum(1 for r in rows if not r['skip'])} books, {total / 1e6:.0f} MB to download.")
    if not a.go:
        print("Nothing downloaded yet. Re-run with --go to download and split into chapters "
              "(chapter finding costs about $0.05 a book).")
        return
    failed = []
    for r in rows:
        if r["skip"]:
            continue
        print(f"{r['name']} ({r['key']}):")
        try:
            chapters = orchids.import_one(r)
        except (SystemExit, Exception) as e:  # one bad book shouldn't stop the rest
            print(f"  FAILED: {e}")
            failed.append(r["key"])
            continue
        for c in chapters:
            print(f"  ch{c['chapter']:02d}  pages {c['pages']:>9}  {c['title']}")
    if failed:
        print(f"Failed: {', '.join(failed)}. Fix the cause and re-run with --go "
              f"--only {' '.join(failed)} (finished books are skipped quickly).")
    print("Check the chapter tables. To fix one book's split, edit "
          "data/raw/<book>/ebook-<id>.chapters.json and re-run with --go --only <book>.")
    print("Then: python -m buddy.ingest submit-all   (batch, half price)")


def cmd_submit_all(a):
    """Submit every school-book chapter that hasn't been processed yet, in one batch."""
    import pymupdf

    from buddy.books import school_book_keys
    from buddy.ingest.download import local_chapters

    items, pages = [], 0
    for key in sorted(school_book_keys()):
        for ch in local_chapters(get_book(key)):
            if not batch.processed_path(key, ch).exists():
                items.append((key, ch))
                with pymupdf.open(batch.chapter_pdf_path(key, ch)) as d:
                    pages += d.page_count
    if not items:
        print("Nothing new to read: every school-book chapter is processed.")
        return
    est = pages * 0.005  # measured: ~$0.079 for a 16-page chapter at batch price
    print(f"{len(items)} chapters, {pages} pages -> about ${est:.2f} at batch price.")
    if not a.yes:
        print("Re-run with --yes to submit.")
        return
    gate(items, a.force)
    bid = batch.submit(client(), items)
    print(f"Submitted batch {bid}. Collect with: python -m buddy.ingest collect {bid} --wait")


def cmd_remove(a):
    from buddy.rag import store

    store.delete_where({"$and": [{"book": a.book}, {"source": "ncert"}]})
    print(f"Removed {a.book} from search. (Files are kept; `index {a.book} all` adds it back.)")


def cmd_run(a):
    book = get_book(a.book)
    ch = int(a.chapter)
    c = client()  # fail on a missing key before doing any work
    if a.now:
        from buddy.ingest.schoolbook import ingest_now

        if book.ncert_code:
            download_chapter(book, ch)
        r = ingest_now(a.book, ch, client=c)
        print(f"  {r['topics']} topics, {r['qa_pairs']} Q&A, {r['chunks']} chunks, "
              f"${r['cost_usd']:.4f} (normal API price)")
        return
    print(f"1/4 download {book.label} ch{ch:02d}")
    download_chapter(book, ch)
    print(f"2/4 submit batch ({INGEST_MODEL}, 50% batch price)")
    bid = batch.submit(c, [(a.book, ch)])
    print(f"    batch id {bid} (safe to Ctrl-C; resume with `collect {bid} --wait`)")
    print("3/4 wait for results")
    batch.wait(c, bid)
    reports = batch.collect(c, bid)
    print_reports(reports)
    ok = [r for r in reports if r["status"] == "ok"]
    if not ok:
        sys.exit("Chapter failed; nothing indexed.")
    print(f"4/4 index into ChromaDB: {index_chapter(a.book, ch)} chunks")
    cmd_costs(a)


def cmd_index(a):
    for ch in parse_chapters(a.book, a.chapters):
        print(f"{a.book} ch{ch:02d}: {index_chapter(a.book, ch)} chunks")


def cmd_costs(_a):
    hist = batch.cost_history()
    if not hist:
        print("No chapters processed yet.")
        return
    total = sum(r["cost_usd"] for r in hist)
    avg = total / len(hist)
    done = {(r.get("book", r.get("subject")), r["chapter"]) for r in hist}
    remaining = sum(
        1 for b in BOOKS.values() if b.chapters
        for ch in range(1, b.chapters + 1) if (b.key, ch) not in done
    )
    unknown = [b.key for b in BOOKS.values() if not b.chapters]
    print(f"Measured: {len(hist)} chapter(s), ${total:.4f} total, ${avg:.4f} per chapter "
          f"(avg in={sum(r['input_tokens'] for r in hist)//len(hist):,} "
          f"out={sum(r['output_tokens'] for r in hist)//len(hist):,} tokens)")
    print(f"Projection: {remaining} NCERT chapters left -> ~${avg * remaining:.2f} "
          "(Hindi pages are image-only, so expect those to differ a little)")
    print(f"Not counted (chapter count unknown until downloaded): {', '.join(unknown)}")


def cmd_upload(a):
    from buddy.ingest.uploads import ingest_upload

    r = ingest_upload(Path(a.file), a.subject, a.chapter, a.kind)
    print(r)


def main(argv=None):
    load_custom_books()  # school books become valid book keys below
    p = argparse.ArgumentParser(prog="python -m buddy.ingest")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_sc(name, fn, chapter_arg="chapters"):
        sp = sub.add_parser(name)
        sp.add_argument("book", choices=list(BOOKS), help="book key, e.g. evs or evs-c3")
        sp.add_argument(chapter_arg)
        sp.set_defaults(fn=fn)
        return sp

    with_sc("download", cmd_download).add_argument("--force", action="store_true")
    sp = with_sc("add-pdf", cmd_add_pdf, "chapter")
    sp.add_argument("file")
    sp = sub.add_parser("add-zip")
    sp.add_argument("book", choices=list(BOOKS), help="book key, e.g. evs or evs-c3")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_add_zip)
    with_sc("estimate", cmd_estimate)
    with_sc("submit", cmd_submit).add_argument("--force", action="store_true")
    with_sc("index", cmd_index)
    with_sc("run", cmd_run, "chapter").add_argument(
        "--now", action="store_true", help="normal API now (full price) instead of a batch")
    sp = sub.add_parser("add-book", help="register one of her school's own books")
    sp.add_argument("key", help="e.g. orchids-evs")
    sp.add_argument("--subject", required=True, choices=["evs", "english", "maths", "hindi",
                                                         "kannada"])
    sp.add_argument("--name", required=True, help='shown in citations, e.g. "EVS (school book)"')
    sp.add_argument("--grade", type=int, default=4)
    sp.add_argument("--title", default="")
    sp.set_defaults(fn=cmd_add_book)
    sp = sub.add_parser("add-photos", help="photos/PDFs of one chapter of a book")
    sp.add_argument("book")
    sp.add_argument("chapter")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--now", action="store_true", help="read and index it right away")
    sp.add_argument("--sort", action="store_true", help="order files by name")
    sp.set_defaults(fn=cmd_add_photos)
    sp = sub.add_parser("fetch-ebook", help="download a school e-book's pages and split "
                                            "them into chapters")
    sp.add_argument("book", help="a school book key (add-book first), e.g. orchids-evs")
    sp.add_argument("ebook_ids", nargs="+", help="e-book id(s), e.g. 1749")
    sp.add_argument("--split", help='chapter start pages, e.g. "1:5,2:17,3:30" '
                                    "(skips automatic detection)")
    sp.add_argument("--url", help="page URL template with {ebook} and {page}")
    sp.set_defaults(fn=cmd_fetch_ebook)
    sp = sub.add_parser("import-orchids", help="Orchids e-books from the portal listing")
    sp.add_argument("listing", nargs="+", help="saved JSON listing file(s)")
    sp.add_argument("--go", action="store_true", help="download and split (else just plan)")
    sp.add_argument("--only", nargs="+", help="only these book keys")
    sp.set_defaults(fn=cmd_import_orchids)
    sp = sub.add_parser("submit-all", help="read all new school-book chapters (one batch)")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_submit_all)
    sp = sub.add_parser("remove", help="take a book out of search")
    sp.add_argument("book")
    sp.set_defaults(fn=cmd_remove)
    sp = sub.add_parser("collect")
    sp.add_argument("batch_id")
    sp.add_argument("--wait", action="store_true")
    sp.add_argument("--no-index", action="store_true")
    sp.set_defaults(fn=cmd_collect)
    sub.add_parser("costs").set_defaults(fn=cmd_costs)
    sp = sub.add_parser("upload")
    sp.add_argument("file")
    sp.add_argument("--subject", default="unknown")
    sp.add_argument("--chapter", type=int, default=0)
    sp.add_argument("--kind", default="worksheet",
                    choices=["worksheet", "notes", "test paper"])
    sp.set_defaults(fn=cmd_upload)

    a = p.parse_args(argv)
    import anthropic

    try:
        a.fn(a)
    except anthropic.BadRequestError as e:
        if "workspace" in str(e) and not get_settings().anthropic_workspace_id:
            sys.exit("Your API key isn't scoped to a workspace. Add ANTHROPIC_WORKSPACE_ID=wrkspc_... "
                     "to .env (Claude Console -> Settings -> Workspaces), or create a key "
                     "inside a workspace.")
        raise


if __name__ == "__main__":
    main()
