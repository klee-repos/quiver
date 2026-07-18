"""One scheduled intelligence pass: fetch new documents -> split -> prefilter -> chain -> ingest.

This is the orchestration the intel timer runs (analysis side, wall-clean). It ties the pure
pieces together and is the ONE place the LLM chain fans over sections. Every I/O seam is injectable
(``poll_bills``/``fetch_text``/``poll_rules``/``llm``) so the offline test drives the whole pass with
fakes and never touches the network or a model.

Best-effort by contract: a fetch/parse/LLM failure on one document is logged into the result counts
and skipped — the pass proceeds. It writes only intel tables (documents/sections/section_impact); it
never trades and never mutates the trading book. Rendering profiles and emitting proposals are
SEPARATE subcommands run after this, so a chain failure can't block them.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from lib.intel import chain as _chain
from lib.intel import prefilter as _prefilter
from lib.intel import sections as _sections
from lib.intel import store as _store


def run_refresh(conn, *, documents: List[dict], allow: List[str], book_desc: str,
                fetch_text: Callable[[dict], bytes], llm: Optional[Callable] = None,
                now: str = "", max_sections_per_doc: int = 200, min_section_chars: int = 200,
                chain_timeout: int = 300) -> dict:
    """Process a batch of already-listed documents into section_impact rows.

    ``documents``: list of normalized doc dicts (from congress poll or fedreg.poll_rules) carrying
      doc_id/kind/title/published/sponsor/congress/agency.
    ``fetch_text(doc) -> bytes``: returns the document's USLM/section XML (injectable).
    Returns counts: {documents, sections_seen, kept, impacts, errors}."""
    n_docs = n_seen = n_kept = n_imp = n_err = 0
    for doc in documents:
        kind = doc.get("kind", "bill")
        agency = doc.get("agency", "")
        try:
            raw = fetch_text(doc)
            # dispatch the splitter by document kind: bills are USLM <section>, Federal Register
            # rules are the FR schema (<SECTION>/<SECTNO>/<SUBJECT> + the SUMMARY block).
            if kind == "rule":
                secs = _sections.parse_fr_sections(raw, min_chars=min_section_chars)
            else:
                secs = _sections.parse_sections(raw, min_chars=min_section_chars)
        except Exception:  # noqa: BLE001 — one bad doc never stops the pass
            n_err += 1
            continue
        _store.upsert_document(
            conn, doc_id=doc["doc_id"], kind=kind, title=doc.get("title", ""),
            published=doc["published"], sponsor=doc.get("sponsor"), congress=doc.get("congress"),
            agency=agency, url=doc.get("url", ""), fetched_at=now)
        n_docs += 1
        # substantive sections only, then the cost-gate prefilter. For a rule from a book-relevant
        # regulator, keep its operative sections regardless of vocab (CFR-by-agency crosswalk);
        # otherwise the keyword gate decides.
        subst = [s for s in secs[:max_sections_per_doc] if not s.is_boilerplate]
        n_seen += len(subst)
        if kind == "rule" and _prefilter.agency_is_relevant(agency):
            kept = subst
        else:
            kept, _dropped = _prefilter.keep(subst)
        for s in kept:
            _store.upsert_section(conn, doc_id=doc["doc_id"], sec=s.enum, header=s.header,
                                  usc_cites=s.usc_cites, deadline=s.deadline, kept=True)
            n_kept += 1
            try:
                res = _chain.analyze_section(s, allow=allow, book=book_desc, llm=llm,
                                             timeout=chain_timeout)
            except Exception:  # noqa: BLE001
                n_err += 1
                continue
            for hit in res.get("book_hits", []):
                _store.record_impact(
                    conn, doc_id=doc["doc_id"], sec=s.enum, ticker=hit["ticker"],
                    direction=hit["direction"], confidence=hit.get("confidence", "low"),
                    step1_what_changes=res.get("step1_what_changes", ""),
                    step1_span=res.get("step1_span", ""), step2_mechanism=res.get("step2_mechanism", ""),
                    reasoning=hit.get("reasoning", ""), verify_verdict="",
                    span_verbatim=bool(res.get("span_verbatim")), model="claude",
                    prompt_version=_chain.PROMPT_VERSION, scored_at=now)
                n_imp += 1
    return {"documents": n_docs, "sections_seen": n_seen, "kept": n_kept,
            "impacts": n_imp, "errors": n_err}
