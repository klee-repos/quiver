"""Congress.gov bill fetcher for the legislative-catalyst feature (analysis side).

Polls the Library of Congress API (https://api.congress.gov/v3) for bills whose
``updateDate`` advanced since the last cursor, fetches each candidate's structured
detail + full text, and computes a PYTHON-owned passage probability. No new heavy
dependency — stdlib ``urllib`` only.

WALL / fail-safe (enforced by the wall tests in tests/test_units.py):
- Imports only stdlib + ``lib.dataflows.errors``. NEVER imports lib.signals/risk/config
  or reads the broker or a trading limit.
- RAISES ``DataUnavailableError`` on a missing key / non-200 / unparseable body rather
  than returning junk, so the caller degrades to "no bills" and never fabricates a
  confident impact (mirrors lib/dataflows/y_finance.py).
- ``heuristic_passage_prob`` is PURE (no network) — the passage number the gate reads is
  Python's, never the LLM's.
- The passage number is derived from the bill's OWN action timeline / bill-type — fields
  the bill author cannot inflate at will (unlike title/summary free text).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional

from .errors import DataUnavailableError

CONGRESS_API = "https://api.congress.gov/v3"

# Ordered progress ladder -> base passage probability. Monotonic in how far a bill has
# advanced (a chamber-passed bill is likelier to become law than one still in committee).
_STATUS_BASE = {
    "became_law": 1.0,
    "to_president": 0.90,
    "passed_both": 0.75,
    "passed_one": 0.35,
    "reported": 0.15,
    "committee": 0.05,
    "introduced": 0.03,
    "failed": 0.0,
}
_MUST_PASS_RE = re.compile(r"\b(appropriat|national defense authorization|ndaa|continuing resolution)\b", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- #
# HTTP (injectable opener so the unit tests run fully offline)                 #
# --------------------------------------------------------------------------- #

def _default_opener(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "quiver-legislative/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed api host)
            if resp.status != 200:
                raise DataUnavailableError(f"congress.gov HTTP {resp.status} for {url}")
            return resp.read()
    except urllib.error.HTTPError as e:
        raise DataUnavailableError(f"congress.gov HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DataUnavailableError(f"congress.gov unreachable: {e}") from e


def _get_json(url: str, *, timeout: int, opener: Optional[Callable[[str, int], bytes]]) -> dict:
    raw = (opener or _default_opener)(url, timeout)
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise DataUnavailableError(f"congress.gov returned unparseable JSON: {e}") from e
    if not isinstance(obj, dict):
        raise DataUnavailableError("congress.gov returned a non-object body")
    return obj


def _api_url(path: str, api_key: str, **params) -> str:
    if not api_key:
        raise DataUnavailableError("CONGRESS_API_KEY is not set")
    q = {"api_key": api_key, "format": "json", **{k: v for k, v in params.items() if v is not None}}
    return f"{CONGRESS_API}/{path.lstrip('/')}?{urllib.parse.urlencode(q)}"


# --------------------------------------------------------------------------- #
# Poll + detail + text                                                        #
# --------------------------------------------------------------------------- #

def poll_changed_bills(since_iso: str, until_iso: str, api_key: str, *,
                       page_limit: int = 250, max_pages: int = 8, timeout: int = 30,
                       opener: Optional[Callable[[str, int], bytes]] = None) -> List[dict]:
    """Bills whose ``updateDate`` advanced in (since, until]. Paginates offset/limit and
    dedups by ``(bill_id, updateDate)``. RAISES DataUnavailableError on a hard API failure
    (missing key / non-200 / unparseable) so the caller degrades to no bills."""
    out: List[dict] = []
    seen = set()
    offset = 0
    for _ in range(max_pages):
        url = _api_url("bill", api_key, fromDateTime=since_iso, toDateTime=until_iso,
                       sort="updateDate+desc", limit=page_limit, offset=offset)
        obj = _get_json(url, timeout=timeout, opener=opener)
        bills = obj.get("bills") or []
        if not isinstance(bills, list) or not bills:
            break
        for b in bills:
            if not isinstance(b, dict):
                continue
            congress = b.get("congress")
            btype = str(b.get("type", "") or "").strip().lower()
            number = b.get("number")
            if congress is None or not btype or number is None:
                continue
            bill_id = f"{congress}-{btype}-{number}"
            update_date = b.get("updateDate") or ""
            key = (bill_id, update_date)
            if key in seen:
                continue
            seen.add(key)
            la = b.get("latestAction") or {}
            out.append({
                "bill_id": bill_id, "congress": int(congress), "bill_type": btype,
                "number": number, "title": str(b.get("title", "") or "")[:500],
                "latest_action": str(la.get("text", "") or "")[:500],
                "latest_action_date": la.get("actionDate"),
                "update_date": update_date,
                "update_date_including_text": b.get("updateDateIncludingText") or update_date,
            })
        if len(bills) < page_limit:
            break
        offset += page_limit
    return out


def _names_from(container, key: str) -> List[str]:
    """Pull the ``name`` fields out of a Congress.gov sub-collection (policyArea /
    legislativeSubjects), tolerating the API's nested shapes."""
    names: List[str] = []
    node = container.get(key) if isinstance(container, dict) else None
    if isinstance(node, dict):
        if "name" in node and isinstance(node["name"], str):
            names.append(node["name"])
        for v in node.values():
            if isinstance(v, list):
                names.extend(str(x.get("name")) for x in v if isinstance(x, dict) and x.get("name"))
    elif isinstance(node, list):
        names.extend(str(x.get("name")) for x in node if isinstance(x, dict) and x.get("name"))
    return [n for n in names if n]


def fetch_bill_detail(congress, bill_type, number, api_key, *, timeout: int = 30,
                      opener: Optional[Callable[[str, int], bytes]] = None) -> dict:
    """Fetch the bill's structured detail + subjects. Returns a normalized dict:
    ``{policy_codes, sponsors_n, cosponsors_n, actions_n, latest_action(_date), status,
    text_versions}``. ``policy_codes`` are the CRS controlled-vocabulary names (policyArea +
    legislativeSubjects) — the ONLY bill-side fields used for the REMOVE relevance match
    (never the attacker-influenced title/text)."""
    base = f"bill/{congress}/{str(bill_type).lower()}/{number}"
    obj = _get_json(_api_url(base, api_key), timeout=timeout, opener=opener)
    bill = obj.get("bill") or {}
    if not isinstance(bill, dict):
        raise DataUnavailableError(f"congress.gov: no bill body for {base}")
    policy_codes: List[str] = []
    pa = bill.get("policyArea")
    if isinstance(pa, dict) and pa.get("name"):
        policy_codes.append(str(pa["name"]))
    # legislativeSubjects live under /subjects; fetch best-effort (missing -> policyArea only)
    try:
        subj = _get_json(_api_url(f"{base}/subjects", api_key), timeout=timeout, opener=opener)
        policy_codes.extend(_names_from(subj.get("subjects", {}), "legislativeSubjects"))
    except DataUnavailableError:
        pass
    cosponsors = bill.get("cosponsors") or {}
    sponsors = bill.get("sponsors") or []
    actions = bill.get("actions") or {}
    la = bill.get("latestAction") or {}
    tv = bill.get("textVersions") or {}
    # sponsor bioguide (the KEY-PLAYER attribution key) — additive; sponsors_n stays for
    # existing consumers. A bill has one sponsor; take the first sponsor's bioguideId.
    sponsor_bio = ""
    if isinstance(sponsors, list) and sponsors and isinstance(sponsors[0], dict):
        sponsor_bio = str(sponsors[0].get("bioguideId", "") or "")
    detail = {
        "policy_codes": sorted(set(policy_codes)),
        "sponsors_n": len(sponsors) if isinstance(sponsors, list) else 0,
        "sponsor_bioguide": sponsor_bio,
        "introduced_date": str(bill.get("introducedDate", "") or ""),
        "cosponsors_n": int(cosponsors.get("count", 0) or 0) if isinstance(cosponsors, dict) else 0,
        "actions_n": int(actions.get("count", 0) or 0) if isinstance(actions, dict) else 0,
        "latest_action": str(la.get("text", "") or ""),
        "latest_action_date": la.get("actionDate"),
        "bill_type": str(bill_type).lower(),
        "title": str(bill.get("title", "") or ""),
        "text_versions_url": tv.get("url") if isinstance(tv, dict) else None,
    }
    detail["status"] = derive_status(detail["latest_action"])
    return detail


def fetch_text_versions(text_versions_url: str, api_key: str, *, timeout: int = 30,
                        opener: Optional[Callable[[str, int], bytes]] = None) -> List[dict]:
    """List the available text versions (each has formats -> GovInfo URLs)."""
    if not text_versions_url:
        return []
    # the url already carries a key/format in most responses; re-key defensively
    sep = "&" if "?" in text_versions_url else "?"
    url = f"{text_versions_url}{sep}api_key={urllib.parse.quote(api_key)}&format=json"
    obj = _get_json(url, timeout=timeout, opener=opener)
    versions = obj.get("textVersions") or []
    return versions if isinstance(versions, list) else []


def fetch_bill_text(text_versions: List[dict], *, max_chars: int = 96_000, timeout: int = 30,
                    opener: Optional[Callable[[str, int], bytes]] = None) -> str:
    """Follow the newest text version's Formatted-Text/HTML link (GovInfo) and return plain
    text, truncated to ``max_chars`` (< 128KB so the analysis prompt is argv/stdin-safe).
    RAISES DataUnavailableError when no usable version exists."""
    if not text_versions:
        raise DataUnavailableError("no text versions available")
    # newest first if a date is present
    def _date(v):
        return str(v.get("date") or "")
    for v in sorted(text_versions, key=_date, reverse=True):
        for fmt in (v.get("formats") or []):
            if not isinstance(fmt, dict):
                continue
            ftype = str(fmt.get("type", "") or "").lower()
            url = fmt.get("url")
            if not url or "pdf" in ftype:
                continue  # prefer text/html/xml over pdf
            raw = (opener or _default_opener)(url, timeout)
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            text = _TAG_RE.sub(" ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                return text[:max_chars]
    raise DataUnavailableError("no usable (non-pdf) text version")


# --------------------------------------------------------------------------- #
# Pure Python passage probability (the gated number)                          #
# --------------------------------------------------------------------------- #

def derive_status(latest_action_text: str) -> str:
    """Map a latest-action string to a coarse progress status. Pure/keyword-based."""
    t = (latest_action_text or "").lower()
    if "became public law" in t or "became private law" in t or "signed by president" in t:
        return "became_law"
    if "presented to president" in t or "cleared for white house" in t \
            or "sent to president" in t:
        return "to_president"
    if ("passed senate" in t and "passed house" in t) or "resolving differences" in t \
            or "passed congress" in t:
        return "passed_both"
    # Chamber passage detection. Beyond the literal "Passed House/Senate", the House passes
    # most non-controversial bills by SUSPENSION OF THE RULES ("On motion to suspend the rules
    # and pass the bill ... Agreed to") — the dominant path for commemorative/naming/minor
    # bills, and a real blind spot the calibration backtest surfaced. A bare "Received in the
    # Senate/House" also implies the originating chamber already passed it. (A MOTION to suspend
    # without "agreed to" is not passage, so it stays below.)
    if "passed/agreed to in house" in t or "passed house" in t \
            or "passed/agreed to in senate" in t or "passed senate" in t \
            or ("suspend the rules and pass" in t and "agreed to" in t) \
            or ("suspend the rules and agree" in t and "agreed to" in t) \
            or "received in the senate" in t or "received in the house" in t:
        return "passed_one"
    if "reported" in t or "ordered to be reported" in t or "placed on" in t and "calendar" in t:
        return "reported"
    if "failed" in t or "withdrawn" in t or "motion to table agreed" in t \
            or "vetoed" in t or "tabled" in t:
        return "failed"
    if "referred to" in t or "committee" in t:
        return "committee"
    return "introduced"


def heuristic_passage_prob(detail: Optional[dict]) -> float:
    """PURE estimate of P(becomes law) in [0,1] from the bill's own progress + type.

    Reproduces the idea behind GovTrack's prognosis with signals we own: the action-timeline
    status (dominant), a small bump for a must-pass bill type and for cosponsor breadth. No
    network, no LLM — this is the number the Python gate reads."""
    if not isinstance(detail, dict):
        return 0.0
    status = str(detail.get("status") or "introduced")
    base = _STATUS_BASE.get(status, 0.03)
    if status == "failed":
        return 0.0
    # must-pass vehicles (appropriations / NDAA / CR) are likelier to be enacted in some form
    title = str(detail.get("title") or "")
    if _MUST_PASS_RE.search(title) or _MUST_PASS_RE.search(str(detail.get("latest_action") or "")):
        base += 0.05
    # cosponsor breadth: up to +0.10, saturating (a proxy for cross-caucus support)
    cosp = int(detail.get("cosponsors_n", 0) or 0)
    base += min(cosp, 100) / 100.0 * 0.10
    return round(max(0.0, min(1.0, base)), 4)
