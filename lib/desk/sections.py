"""Filing text: HTML to text, sections by form, the index page, risk diffs, ingest (plan §7.4).

``html_to_text`` uses BeautifulSoup with lxml. It drops ``script``, ``style`` and the hidden
``ix:header``, writes one line per block element and one tab-separated line per table row.
Then it removes the lines that hold only a page number, only "Table of Contents", or a
running header or footer at the edge of many pages. These lines break exact quotes and
make the risk diff larger.

``split_sections`` cuts the text by form. For a 10-K or 10-Q the body starts at the second
``PART I`` line, so the table of contents makes no section. One key gets one section: the
first match wins, and a later match of the same key stays inside the section above it.
A text with no heading match becomes ``<form>:part-N`` parts of at most 60,000 characters.

``ingest_filing`` fetches one filing, stores each section as a ``filing_section`` document,
and stores the risk-factor diff against the previous filing of the same form.
"""

from __future__ import annotations

import difflib
import re
import warnings
from collections import Counter
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString
from bs4.element import Comment, Declaration, Doctype, ProcessingInstruction

from lib.desk import edgar, store

try:  # bs4 >= 4.11
    from bs4 import XMLParsedAsHTMLWarning
except ImportError:  # pragma: no cover
    XMLParsedAsHTMLWarning = UserWarning

PART_MAX = 60_000

# --- HTML to text ------------------------------------------------------------------

_BLOCK = frozenset((
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "html", "li", "main", "nav", "ol", "p", "section", "table", "tbody", "tfoot",
    "thead", "ul"))
_DROP = frozenset(("script", "style", "ix:header", "head", "noscript", "template"))
_SKIP_STRINGS = (Comment, Declaration, Doctype, ProcessingInstruction)
_WS = re.compile(r"\s+")
_SPACES = re.compile(r"[ \t]*\t[ \t]*|  +")
_SGML_HEAD = re.compile(r"^\s*<DOCUMENT>.*?<TEXT>", re.S | re.I)
_SGML_TAIL = re.compile(r"</TEXT>\s*</DOCUMENT>\s*$", re.I)
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_HAS_TAGS = re.compile(r"<(?:html|body|div|p|table|pre|font|br|span)\b", re.I)

_PAGE_NO = (
    re.compile(r"(?:[Pp]age\s+)?(?:[A-Z]{1,2}-)?\d{1,3}"),
    re.compile(r"-\s*\d{1,3}\s*-"),
    re.compile(r"(?=[ivxlc])c{0,3}(?:x[cl]|l?x{0,3})(?:i[xv]|v?i{0,3})"),   # lowercase roman
)
_TOC_LINE = re.compile(r"table\s+of\s+contents", re.I)
RUNNING_MIN_PAGES = 5
RUNNING_SHARE = 0.3


def _inline(node, out: list) -> None:
    """The text of a table cell on one line."""
    for c in node.children:
        if isinstance(c, NavigableString):
            if not isinstance(c, _SKIP_STRINGS):
                out.append(str(c))
            continue
        if c.name in _DROP:
            continue
        if c.name == "br":
            out.append(" ")
            continue
        _inline(c, out)
        if c.name in _BLOCK or c.name in ("td", "th", "tr"):
            out.append(" ")


def _walk(node, out: list) -> None:
    for c in node.children:
        if isinstance(c, NavigableString):
            if not isinstance(c, _SKIP_STRINGS):
                out.append(_WS.sub(" ", str(c)))
            continue
        name = c.name
        if name in _DROP:
            continue
        if name == "br":
            out.append("\n")
        elif name == "pre":
            out.append("\n" + c.get_text() + "\n")
        elif name == "tr":
            cells = []
            for td in c.find_all(("td", "th"), recursive=False):
                buf: list = []
                _inline(td, buf)
                s = _WS.sub(" ", "".join(buf)).strip()
                if s:
                    cells.append(s)
            if cells:
                out.append("\n" + "\t".join(cells) + "\n")
        else:
            block = name in _BLOCK
            if block:
                out.append("\n")
            _walk(c, out)
            if block:
                out.append("\n")


def _is_page_number(line: str) -> bool:
    return any(p.fullmatch(line) for p in _PAGE_NO)


def clean_lines(lines: list) -> list:
    """Drop empty lines, page-number lines, "Table of Contents" lines, and running headers.

    A running header or footer is a line of at most 120 characters that is the first or the
    last line of at least 30% of the pages (and of 5 pages or more). Only its copies at a page
    edge go; the same words inside a page stay. Pages end at the page-number lines."""
    keep: list = []
    breaks: list = []
    for raw in lines:
        line = _SPACES.sub(lambda m: "\t" if "\t" in m.group(0) else " ",
                           _ZERO_WIDTH.sub("", raw)).strip(" \t")
        if not line:
            continue
        if _is_page_number(line):
            breaks.append(len(keep))
            continue
        if _TOC_LINE.fullmatch(line):
            continue
        keep.append(line)
    if len(breaks) < RUNNING_MIN_PAGES:
        return keep
    bounds = [0] + breaks + [len(keep)]
    pages = [(a, b) for a, b in zip(bounds, bounds[1:]) if a < b]
    edges: Counter = Counter()
    for a, b in pages:
        for i in {a, b - 1}:
            edges[keep[i]] += 1
    need = max(RUNNING_MIN_PAGES, RUNNING_SHARE * len(pages))
    running = {line for line, n in edges.items() if n >= need and len(line) <= 120}
    if not running:
        return keep
    drop = {i for a, b in pages for i in {a, b - 1} if keep[i] in running}
    return [line for i, line in enumerate(keep) if i not in drop]


def html_to_text(html: str) -> str:
    """Filing HTML (or plain text) as clean text: one line per block, tab-separated table rows."""
    html = str(html or "")
    html = _SGML_TAIL.sub("", _SGML_HEAD.sub("", html, count=1))
    if not _HAS_TAGS.search(html):
        return "\n".join(clean_lines(html.replace("\r\n", "\n").split("\n")))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    out: list = []
    _walk(soup, out)
    return "\n".join(clean_lines("".join(out).split("\n")))


# --- sections ----------------------------------------------------------------------

_PART = re.compile(r"^\s*PART\s+(IV|III|II|I)\b(?!,)", re.I)
# "Item 1A. Risk Factors", "ITEM 7 MANAGEMENT'S ...". Not "Item 7 of ..." and not an 8-K code ("Item 5.02").
_ITEM = re.compile(r"^\s*[Ii][Tt][Ee][Mm][Ss]?\s+(\d{1,2}[A-Za-z]?)(?!\.\d)"
                   r"(?=$|\s*[.:—–\-\t]|\s+[A-Z\[(“\"])")
_TOC_ROW = re.compile(r"\t\s*(?:[A-Z]{1,2}-)?\d{1,4}\s*$")
_RISK_3D = re.compile(r"^\s*(?:Item\s+)?(?:3\.?\s*)?D\.?\s+Risk\s+Factors\b", re.I)
HEADING_MAX = 300

KEYS_10K = ("1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8", "9", "9A", "9B", "9C",
            "10", "11", "12", "13", "14", "15", "16")
KEYS_10Q = {"I": ("1", "2", "3", "4"), "II": ("1", "1A", "2", "3", "4", "5", "6")}
S1_HEADINGS = ("PROSPECTUS SUMMARY", "RISK FACTORS", "USE OF PROCEEDS",
               "MANAGEMENT'S DISCUSSION AND ANALYSIS", "BUSINESS", "MARKET OPPORTUNITY", "INDUSTRY",
               "MANAGEMENT", "PRINCIPAL STOCKHOLDERS", "DESCRIPTION OF CAPITAL STOCK", "UNDERWRITING")
S1_PREFIX = "MANAGEMENT'S DISCUSSION AND ANALYSIS"   # the full heading runs on ("... OF FINANCIAL ...")
ITEM_FORMS = ("10-K", "10-Q", "20-F")
HEADING_FORMS = ("S-1", "F-1", "424B4")
BODY_FORMS = ("8-K", "6-K", "40-F")
RISK_KEYS = {"10-K": "1A", "10-Q": "II-1A", "20-F": "3D"}


def base_form(form: str) -> str:
    f = str(form or "").strip().upper()
    return f[:-2] if f.endswith("/A") else f


def _line_starts(text: str) -> tuple:
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    return lines, starts


def _cut(text: str, starts: list, marks: list, form: str) -> list:
    """``marks`` = [(line index, key, title)] in order -> [(key, title, text)]. A mark with
    key None ends the section above it and starts none."""
    out = []
    for n, (i, key, title) in enumerate(marks):
        if key is None:
            continue
        end = starts[marks[n + 1][0]] if n + 1 < len(marks) else len(text)
        out.append((f"{form}:{key}", title, text[starts[i]:end].rstrip("\n")))
    return out


def _item_key(base: str, part: Optional[str], num: str, title: str) -> Optional[str]:
    num = num.upper()
    if base == "10-K":
        return num if num in KEYS_10K else None
    if base == "10-Q":
        # Some issuers (CEG) print Part I "Item 4. Controls and Procedures" after Part II.
        if num == "4" and "CONTROLS AND PROCEDURES" in title.upper():
            part = "I"
        return f"{part}-{num}" if part in KEYS_10Q and num in KEYS_10Q[part] else None
    m = re.fullmatch(r"(\d{1,2})([A-Z]?)", num)            # 20-F: items 1 to 19, 4A, 16A ...
    return num if m and 1 <= int(m.group(1)) <= 19 else None


def _split_items(text: str, form: str, base: str) -> list:
    lines, starts = _line_starts(text)
    part_i = [i for i, line in enumerate(lines)
              if len(line) <= 100 and (m := _PART.match(line)) and m.group(1).upper() == "I"]
    body = part_i[1] if len(part_i) >= 2 else (part_i[0] if part_i else 0)
    marks, seen = [], set()
    part: Optional[str] = None
    current: Optional[str] = None
    for i in range(body, len(lines)):
        line = lines[i]
        if len(line) > HEADING_MAX:
            continue
        pm = _PART.match(line) if len(line) <= 100 else None
        if pm:
            roman = pm.group(1).upper()
            if roman != part:                      # a repeated PART line inside a part is text
                part = roman
                marks.append((i, None, line))
            continue
        if _TOC_ROW.search(line):
            continue
        if base == "20-F" and current == "3" and "3D" not in seen and _RISK_3D.match(line):
            seen.add("3D")
            current = "3D"
            marks.append((i, "3D", line.replace("\t", " ")))
            continue
        im = _ITEM.match(line)
        if not im:
            continue
        key = _item_key(base, part, im.group(1), line)
        if key is None or key in seen:
            continue
        seen.add(key)
        current = key
        marks.append((i, key, line.replace("\t", " ")))
    return _cut(text, starts, marks, form)


def _s1_key(line: str) -> Optional[str]:
    n = " ".join(line.replace("’", "'").split())
    if not n or len(n) > 120 or n != n.upper():
        return None
    if n in S1_HEADINGS:
        return n
    if n.startswith(S1_PREFIX + " "):
        return S1_PREFIX
    return None


def _split_headings(text: str, form: str) -> list:
    lines, starts = _line_starts(text)
    marks, seen = [], set()
    for i, line in enumerate(lines):
        key = _s1_key(line)
        if key and key not in seen:
            seen.add(key)
            marks.append((i, key, line.replace("\t", " ")))
    return _cut(text, starts, marks, form)


def split_parts(text: str, form: str, size: int = PART_MAX) -> list:
    """Cut ``text`` into ``<form>:part-N`` parts of at most ``size`` characters. A cut falls
    after a newline when one is in the second half of the window. The parts join to ``text``."""
    parts, i, n = [], 0, len(text)
    while i < n:
        j = min(i + size, n)
        if j < n:
            k = text.rfind("\n", i, j)
            if k >= i + size // 2:
                j = k + 1
        parts.append(text[i:j])
        i = j
    return [(f"{form}:part-{x}", f"{form} part {x}", p) for x, p in enumerate(parts, 1)]


def split_sections(text: str, form: str) -> list:
    """``[(key, title, text)]`` for one document by form (§7.4 rule 4). 8-K, 6-K and 40-F
    give one ``<form>:body``. A form with no heading match gives ``<form>:part-N``."""
    text = str(text or "")
    form = str(form or "").strip().upper()
    base = base_form(form)
    if not text.strip():
        return []
    if base in BODY_FORMS:
        return [(f"{form}:body", f"{form} body", text)]
    secs: list = []
    if base in ITEM_FORMS:
        secs = _split_items(text, form, base)
    elif base in HEADING_FORMS:
        secs = _split_headings(text, form)
    return secs or split_parts(text, form)


# --- the -index.htm page -----------------------------------------------------------

_IX = re.compile(r"^(?:https?://[^/]+)?/ix\?doc=", re.I)


def _cell(node) -> str:
    return " ".join(node.get_text(" ").split()) if node is not None else ""


def parse_index(html: str, base_url: str = edgar.SEC_WWW + "/") -> list:
    """The "Document Format Files" rows of a filing's ``-index.htm`` page as
    ``{seq, description, document, type, url}``. An inline-XBRL viewer link
    (``/ix?doc=/Archives/...``) becomes the plain archive URL. A row with no type (the
    complete submission text file) is left out."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(str(html or ""), "lxml")
    tables = soup.find_all("table", class_="tableFile")
    docs = [t for t in tables if str(t.get("summary") or "").lower().startswith("document format")]
    out = []
    for table in docs or tables[:1]:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            link = tds[2].find("a")
            href = (link.get("href") if link else "") or ""
            typ = _cell(tds[3])
            if not typ or not href:
                continue
            seq = _cell(tds[0])
            out.append({"seq": int(seq) if seq.isdigit() else None, "description": _cell(tds[1]),
                        "document": _cell(link), "type": typ,
                        "url": urljoin(base_url, _IX.sub("", href.strip()))})
    return out


# --- risk-factor diff --------------------------------------------------------------

_SENT = re.compile(r"(?<=[.!?])\s+(?=[“\"(\[A-Z0-9•])|(?<=[.!?][”\"’)])\s+(?=[“\"(\[A-Z0-9•])")


_SENT_END = re.compile(r"[.!?:;][\"\u201d\u2019)]?$")


def _paragraphs(text: str) -> list:
    """Lines joined back into paragraphs. A paragraph that runs over a page break comes
    out as two lines; the next line continues it when it starts in lowercase, or when the
    line above ends with a comma. Without the join, a moved page break makes a false diff."""
    out: list = []
    for line in str(text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if (out and "\t" not in line and "\t" not in out[-1] and not _SENT_END.search(out[-1])
                and (line[0].islower() or out[-1].endswith(","))):
            out[-1] = out[-1] + " " + line
        else:
            out.append(line)
    return out


def split_sentences(text: str) -> list:
    """One item per sentence. A table row (a line with a tab) stays one item."""
    out = []
    for para in _paragraphs(text):
        if "\t" in para:
            out.append(para)
            continue
        out.extend(s.strip() for s in _SENT.split(para) if s.strip())
    return out


def risk_diff(old_text: str, new_text: str) -> tuple:
    """``(added_sentences, removed_sentences)`` from new against old, by ``difflib``."""
    a, b = split_sentences(old_text), split_sentences(new_text)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(a[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(b[j1:j2])
    return added, removed


# --- ingest ------------------------------------------------------------------------

_TEXT_DOC = re.compile(r"\.(?:htm|html|txt|xml)$", re.I)


def exhibit_sections(client, cik, accession: str, form: str) -> list:
    """``[(key, title, text, url)]`` for each ``EX-99`` exhibit of a filing. The exhibit
    type comes from the ``-index.htm`` page: the file name does not tell it."""
    form = str(form or "").strip().upper()
    out, seen = [], set()
    for row in edgar.filing_exhibits(client, cik, accession):
        typ = row["type"].strip().upper()
        if not typ.startswith("EX-99") or not _TEXT_DOC.search(row["url"].split("?")[0]):
            continue
        key = f"{form}:{typ}"
        if key in seen:
            continue
        seen.add(key)
        text = html_to_text(client.get_text(row["url"]))
        out.append((key, row["description"] or typ, text, row["url"]))
    return out


def _previous_section(conn, *, ticker: str, form: str, key: str, filed_date: str, accession: str):
    """The text of ``key`` in the latest earlier ingested filing of the same form."""
    row = conn.execute(
        "SELECT d.text AS text, f.accession AS accession FROM filings f JOIN documents d "
        "ON d.accession = f.accession AND d.kind = 'filing_section' AND d.section_key = ? "
        "WHERE f.ticker = ? AND f.form = ? AND f.filed_date < ? AND f.accession != ? "
        "ORDER BY f.filed_date DESC, f.accession DESC LIMIT 1",
        (key, str(ticker).upper(), form, filed_date, accession)).fetchone()
    return (row["text"], row["accession"]) if row else (None, None)


def ingest_filing(conn, client, *, ticker: str, cik, accession: str, form: str,
                  primary_doc: Optional[str], now: str) -> dict:
    """Fetch one filing, store its sections, and store its risk diff (§7.4 rules 5, 6).

    8-K, 6-K and 40-F also store each ``EX-99`` exhibit. A new 10-K (1A), 10-Q (II-1A) or
    20-F (3D) stores ``<key>-added`` and ``<key>-removed`` against the previous ingested
    filing of the same form; a form's first filing stores no diff, and an empty diff stores
    nothing. Every fetch runs before the first write. A failure sets
    ``filings.ingest_error`` and returns ``ok`` False; a 403 also raises ``EdgarBlocked``.
    Returns ``{accession, form, ok, keys, documents, diffs, error}``."""
    ticker = str(ticker).upper()
    form = str(form or "").strip().upper()
    base = base_form(form)
    out = {"accession": accession, "form": form, "ok": False, "keys": [], "documents": {},
           "diffs": {}, "error": None}
    frow = conn.execute("SELECT filed_date, accepted_at FROM filings WHERE accession=?",
                        (accession,)).fetchone()
    filed = frow["filed_date"] if frow else None
    published = (frow["accepted_at"] or frow["filed_date"]) if frow else None
    try:
        if not primary_doc:
            raise edgar.EdgarError("the filing has no primary document")
        url = edgar.archive_url(cik, accession, primary_doc)
        text = html_to_text(client.get_text(url))
        parts = [(k, title, t, url) for k, title, t in split_sections(text, form)]
        if base in BODY_FORMS:
            parts += exhibit_sections(client, cik, accession, form)
        if not parts:
            raise edgar.EdgarError("the primary document has no text")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:500]
        conn.execute("UPDATE filings SET ingest_error=? WHERE accession=?", (out["error"], accession))
        if isinstance(e, edgar.EdgarBlocked):
            raise
        return out
    texts = {}
    for key, title, t, u in parts:
        if not t.strip():
            continue
        texts[key] = t
        out["documents"][key] = store.store_document(
            conn, kind="filing_section", title=f"{ticker} {form} {filed or accession}: {title}",
            text=t, now=now, ticker=ticker, accession=accession, section_key=key, url=u,
            published_at=published)
    risk = RISK_KEYS.get(form)
    if risk and filed and f"{form}:{risk}" in texts:
        key = f"{form}:{risk}"
        prev_text, prev_acc = _previous_section(conn, ticker=ticker, form=form, key=key,
                                                filed_date=filed, accession=accession)
        if prev_text is not None:
            added, removed = risk_diff(prev_text, texts[key])
            for suffix, sents in (("added", added), ("removed", removed)):
                if not sents:
                    continue
                dkey = f"{key}-{suffix}"
                out["diffs"][dkey] = store.store_document(
                    conn, kind="filing_section",
                    title=f"{ticker} {form} {filed}: risk factor sentences {suffix} since {prev_acc}",
                    text="\n".join(sents), now=now, ticker=ticker, accession=accession,
                    section_key=dkey, url=None, published_at=published)
    conn.execute("UPDATE filings SET ingested_at=?, ingest_error=NULL WHERE accession=?", (now, accession))
    out["ok"] = True
    out["keys"] = list(out["documents"]) + list(out["diffs"])
    return out
