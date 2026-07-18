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


def run_refresh(open_conn, *, documents: List[dict], allow: List[str], book_desc: str,
                fetch_text: Callable[[dict], bytes], llm: Optional[Callable] = None,
                now: str = "", max_sections_per_doc: int = 200, min_section_chars: int = 200,
                chain_timeout: int = 300, max_chained_sections: int = 120) -> dict:
    """Process a batch of already-listed documents into section_impact rows.

    ``open_conn``: a ZERO-ARG callable returning a fresh ledger connection CONTEXT MANAGER (e.g.
      ``led.intel_conn``). Each document's writes commit in their OWN short transaction, so the slow
      per-section network + LLM I/O holds NO database lock, and a run that hits its outer timeout
      keeps every document completed so far instead of rolling the whole pass back.
    ``documents``: normalized doc dicts (from the congress/fedreg pollers) carrying
      doc_id/kind/title/published/sponsor/congress/agency.
    ``fetch_text(doc) -> bytes``: returns the document's section XML (injectable).
    ``max_chained_sections``: a HARD per-run budget on how many sections are sent to the LLM chain,
      so one large omnibus rule (whose sections the agency crosswalk keeps wholesale) can't fan out
      an unbounded number of ``claude -p`` calls. Once hit, remaining sections are stored kept=1 but
      not chained (a later run picks them up).
    Returns counts: {documents, sections_seen, kept, chained, impacts, errors, budget_hit}."""
    n_docs = n_seen = n_kept = n_chained = n_imp = n_err = 0
    budget_hit = False
    for doc in documents:
        kind = doc.get("kind", "bill")
        agency = doc.get("agency", "")
        # ---- I/O phase: fetch + split, NO db lock held ----
        try:
            raw = fetch_text(doc)
            if kind == "rule":
                secs = _sections.parse_fr_sections(raw, min_chars=min_section_chars)
            else:
                secs = _sections.parse_sections(raw, min_chars=min_section_chars)
        except Exception:  # noqa: BLE001 — one bad doc never stops the pass
            n_err += 1
            continue
        subst = [s for s in secs[:max_sections_per_doc] if not s.is_boilerplate]
        n_seen += len(subst)
        # For a rule from a book-relevant regulator keep its operative sections regardless of vocab
        # (CFR-by-agency crosswalk); otherwise the keyword gate decides.
        if kind == "rule" and _prefilter.agency_is_relevant(agency):
            kept = subst
        else:
            kept, _dropped = _prefilter.keep(subst)

        # ---- chain phase: LLM per kept section, NO db lock held; collect rows in memory ----
        section_rows = []  # (Section, chain_result_or_None)
        for s in kept:
            if n_chained >= max_chained_sections:
                budget_hit = True
                section_rows.append((s, None))  # stored kept but not chained this run
                continue
            n_chained += 1
            try:
                res = _chain.analyze_section(s, allow=allow, book=book_desc, llm=llm,
                                             timeout=chain_timeout)
            except Exception:  # noqa: BLE001
                n_err += 1
                res = None
            section_rows.append((s, res))

        # ---- write phase: ONE short transaction for this document (commits per doc) ----
        try:
            with open_conn() as conn:
                _store.upsert_document(
                    conn, doc_id=doc["doc_id"], kind=kind, title=doc.get("title", ""),
                    published=doc["published"], sponsor=doc.get("sponsor"),
                    congress=doc.get("congress"), agency=agency, url=doc.get("url", ""),
                    fetched_at=now)
                for s, res in section_rows:
                    _store.upsert_section(conn, doc_id=doc["doc_id"], sec=s.enum, header=s.header,
                                          usc_cites=s.usc_cites, deadline=s.deadline, kept=True)
                    n_kept += 1
                    if not res:
                        continue
                    for hit in res.get("book_hits", []):
                        _store.record_impact(
                            conn, doc_id=doc["doc_id"], sec=s.enum, ticker=hit["ticker"],
                            direction=hit["direction"], confidence=hit.get("confidence", "low"),
                            step1_what_changes=res.get("step1_what_changes", ""),
                            step1_span=res.get("step1_span", ""),
                            step2_mechanism=res.get("step2_mechanism", ""),
                            reasoning=hit.get("reasoning", ""), verify_verdict="",
                            span_verbatim=bool(res.get("span_verbatim")), model="claude",
                            prompt_version=_chain.PROMPT_VERSION, scored_at=now)
                        n_imp += 1
            n_docs += 1
        except Exception:  # noqa: BLE001 — a write failure on one doc never stops the pass
            n_err += 1
    return {"documents": n_docs, "sections_seen": n_seen, "kept": n_kept, "chained": n_chained,
            "budget_hit": budget_hit,
            "impacts": n_imp, "errors": n_err}
