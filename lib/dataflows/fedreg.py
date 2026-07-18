"""Federal Register fetcher — the REGULATION arm of the strategy-intelligence service.

The regulatory pipeline is the same machinery pointed at a different stream: a final rule has
sections and CFR citations exactly as a bill has sections and U.S. Code citations, and it names its
issuing AGENCY (the regulatory analog of a bill's sponsor / the key player). The Federal Register
API (federalregister.gov/developers) is free and KEYLESS.

Stdlib urllib only (no new heavy dependency), injectable ``opener`` seam identical to
lib/dataflows/congress.py, and fail-SAFE: a fetch that returns nothing raises DataUnavailableError
so the caller skips the document rather than fabricating one. Analysis-side of the wall — never
imports the sizing/limit/broker modules.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional

from lib.dataflows.errors import DataUnavailableError

FEDREG_API = "https://www.federalregister.gov/api/v1"

# The fields we pull per document. published_date is the as-of anchor; agency_names is the
# key-player analog; the CFR references map a rule to an industry the way U.S. Code cites map a bill.
_DOC_FIELDS = ("document_number", "title", "type", "publication_date", "agencies",
               "agency_names", "cfr_references", "regulation_id_numbers", "significant",
               "abstract", "html_url", "body_html_url")


def _default_opener(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "quiver-intel/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed api host)
            if resp.status != 200:
                raise DataUnavailableError(f"federalregister HTTP {resp.status} for {url}")
            ctype = resp.headers.get("Content-Type", "")
            if "json" not in ctype.lower():
                # the FR API fails OPEN on some paths (HTML error at HTTP 200) — assert content-type
                raise DataUnavailableError(f"federalregister non-JSON ({ctype}) for {url}")
            return resp.read()
    except urllib.error.HTTPError as e:
        raise DataUnavailableError(f"federalregister HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DataUnavailableError(f"federalregister unreachable: {e}") from e


def _get_json(url: str, *, timeout: int, opener: Optional[Callable[[str, int], bytes]]) -> dict:
    raw = (opener or _default_opener)(url, timeout)
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise DataUnavailableError(f"federalregister bad JSON: {e}") from e
    if not isinstance(obj, dict):
        raise DataUnavailableError("federalregister: response not an object")
    return obj


def poll_rules(since: str, until: str, *, doc_type: str = "RULE", per_page: int = 100,
               max_pages: int = 5, timeout: int = 30,
               opener: Optional[Callable[[str, int], bytes]] = None) -> List[dict]:
    """List Federal Register documents published in [since, until] (ISO dates). ``doc_type``:
    RULE (final) | PRORULE (proposed) | NOTICE. Returns normalized dicts with the fields the
    section/impact layer reads (agency_names, cfr_references, publication_date). Deduped by
    document_number. Fail-SAFE: a page error stops paging and returns what was collected."""
    out: List[dict] = []
    seen = set()
    for page in range(1, int(max_pages) + 1):
        params = [
            ("conditions[publication_date][gte]", since),
            ("conditions[publication_date][lte]", until),
            ("conditions[type][]", doc_type),
            ("per_page", str(per_page)),
            ("page", str(page)),
            ("order", "newest"),
        ]
        for f in _DOC_FIELDS:
            params.append(("fields[]", f))
        url = f"{FEDREG_API}/documents.json?{urllib.parse.urlencode(params)}"
        obj = _get_json(url, timeout=timeout, opener=opener)
        results = obj.get("results") or []
        if not results:
            break
        for d in results:
            dn = d.get("document_number")
            if not dn or dn in seen:
                continue
            seen.add(dn)
            out.append(_normalize(d))
        if len(results) < per_page:
            break
    return out


def _normalize(d: dict) -> dict:
    """Congress-side shape parity: doc_id + published + agency + cfr codes."""
    agencies = d.get("agency_names") or [a.get("name") for a in (d.get("agencies") or [])
                                         if isinstance(a, dict)]
    cfr = []
    for ref in (d.get("cfr_references") or []):
        if isinstance(ref, dict):
            title, part = ref.get("title"), ref.get("part")
            if title is not None:
                cfr.append(f"{title} CFR {part}" if part is not None else f"{title} CFR")
    return {
        "doc_id": f"FR-{d.get('document_number')}",
        "kind": "rule",
        "title": d.get("title", ""),
        "published": d.get("publication_date", ""),
        "agency": (agencies[0] if agencies else ""),
        "agencies": agencies,
        "cfr_references": cfr,
        "rin": d.get("regulation_id_numbers") or [],
        "significant": bool(d.get("significant")),
        "abstract": d.get("abstract", ""),
        "url": d.get("html_url", ""),
        "body_url": d.get("body_html_url", ""),
    }
