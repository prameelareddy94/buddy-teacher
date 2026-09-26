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
