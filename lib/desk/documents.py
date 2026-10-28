"""Safe URL fetch and web or news document storage (plan §7.9).

``fetch_url`` gets one page from the open internet for the desk. It refuses a URL that is
not ``http`` or ``https``. It resolves the host and refuses the fetch when any answer is
not a public address: private, loopback, link-local (the GCP metadata server at
169.254.169.254), shared (100.64.0.0/10), multicast, reserved or unspecified. It unwraps
an IPv4-mapped IPv6 answer first. It connects to the checked address, with the original
host name for TLS (SNI) and for the ``Host`` header, so a second DNS answer cannot move the
request. It follows at most 3 redirects and checks each one again. It stops at 5 MB or at
20 seconds. It converts HTML to text and refuses a PDF.

``store_fetched`` stores the page as a ``web`` or ``news`` document. The same text gives
the same id. A ``news`` fetch links the ``news_items`` row of that URL to the document.

Seams (plan §18.2): ``resolver(host) -> [ip]``, ``connector(ip, port, host, method, path,
headers, timeout) -> (status, headers, body)`` and ``clock() -> float`` (monotonic seconds).
"""

from __future__ import annotations

import html as htmllib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
import urllib.parse
import warnings
from typing import Optional

from lib.desk import store

log = logging.getLogger(__name__)

MAX_BYTES = 5_000_000
TIMEOUT = 20.0
MAX_REDIRECTS = 3
PDF_ERROR = "PDF not supported; use the EDGAR exhibit"
USER_AGENT = "Mozilla/5.0 (compatible; QuiverDesk/1.0)"
_REDIRECTS = {301, 302, 303, 307, 308}
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml",
               "application/rss+xml", "application/atom+xml", "application/ld+json")


class FetchError(RuntimeError):
    """A fetch was refused or failed. The text says why."""


# --- address checks ----------------------------------------------------------------

def check_address(addr: str):
    """Return the checked ``ipaddress`` object, or raise ``FetchError``.

    An IPv4-mapped IPv6 address (``::ffff:a.b.c.d``) is checked as its IPv4 address. The
    address must be ``is_global``. Python 3.12 calls 224.0.0.1 and ff02::1 global, so the
    check also refuses a multicast, reserved or unspecified address.
    """
    raw = str(addr or "").strip().strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        raise FetchError(f"not an IP address: {addr!r}") from None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        raise FetchError(f"refused: {ip} is not a public address")
    return ip


def _default_resolver(host: str) -> list:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    out = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return out


def _resolve(host: str, resolver) -> list:
    """The checked addresses of ``host``. A literal IP needs no DNS call."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        return [check_address(str(literal))]
    try:
        answers = list((resolver or _default_resolver)(host))
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - a resolver can raise any lookup error
        raise FetchError(f"cannot resolve {host}: {exc}") from None
    if not answers:
        raise FetchError(f"cannot resolve {host}: no address")
    return [check_address(a) for a in answers]


# --- the production connector ------------------------------------------------------

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection to a fixed IP that sends ``sni_host`` as the TLS server name and
    checks the certificate against that name."""

    def __init__(self, ip: str, port: int, sni_host: str, timeout: float):
        self._pin_ctx = ssl.create_default_context()
        super().__init__(ip, port, timeout=timeout, context=self._pin_ctx)
        self._sni_host = sni_host

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self._raw_sock = sock  # the deadline timer shuts it down during a slow TLS handshake
        self.sock = self._pin_ctx.wrap_socket(sock, server_hostname=self._sni_host)


def _http_connect(ip, port, host, method, path, headers, timeout, *, tls: bool,
                  max_bytes: int, clock=time.monotonic):
    """One request to ``ip`` with ``http.client``. Reads at most ``max_bytes + 1`` bytes and
    stops when ``timeout`` seconds pass. A socket timeout limits one read only, so a server
    that drips the TLS handshake, the status line or the headers could hold the thread far
    longer. A timer shuts the socket down at the deadline and the fetch fails."""
    start = clock()
    if tls:
        conn = _PinnedHTTPSConnection(ip, port, host, timeout)
    else:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
    expired = threading.Event()

    def _deadline():
        expired.set()
        for s in (getattr(conn, "_raw_sock", None), conn.sock):
            try:
                if s is not None:
                    s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    timer = threading.Timer(max(0.0, float(timeout)), _deadline)
    timer.daemon = True
    timer.start()
    try:
        return _http_exchange(conn, method, path, headers, timeout, max_bytes=max_bytes, start=start, clock=clock,
                              expired=expired)
    except FetchError:
        raise
    except Exception:
        if expired.is_set():
            raise FetchError(f"refused: the fetch took over {timeout:g} s") from None
        raise
    finally:
        timer.cancel()
        conn.close()


def _http_exchange(conn, method, path, headers, timeout, *, max_bytes, start, clock, expired):
    """Send the request and read the answer; ``expired`` is set by the deadline timer."""
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    resp = conn.getresponse()
    chunks, size = [], 0
    while size <= max_bytes:
        if clock() - start > timeout or expired.is_set():
            raise FetchError(f"refused: the fetch took over {timeout:g} s")
        chunk = resp.read(min(65536, max_bytes + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if expired.is_set():
        raise FetchError(f"refused: the fetch took over {timeout:g} s")
    return resp.status, {k: v for k, v in resp.getheaders()}, b"".join(chunks)


# --- conversion ----------------------------------------------------------------------

def _header(headers: dict, name: str) -> str:
    for k, v in (headers or {}).items():
        if str(k).lower() == name.lower():
            return str(v)
    return ""


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, val = part.strip().partition("=")
        if key.lower() == "charset" and val:
            return val.strip().strip('"\'')
    return "utf-8"


def _html_title(html: str) -> str:
    """The text of the first ``<title>`` element, or an empty string."""
    m = _TITLE.search(html[:200_000])
    return " ".join(htmllib.unescape(m.group(1)).split())[:500] if m else ""


def html_text(html: str) -> str:
    """HTML to text with the §7.4 converter (``lib.desk.sections.html_to_text``). When
    that module is not there, a plain BeautifulSoup ``get_text`` with the scripts removed."""
    try:
        from lib.desk.sections import html_to_text
    except ImportError:
        html_to_text = None
    if html_to_text is not None:
        return html_to_text(html)
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    lines = (" ".join(line.split()) for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def _looks_html(content_type: str, body: bytes) -> bool:
    if "html" in content_type:
        return True
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


# --- fetch ---------------------------------------------------------------------------

def _split(url: str):
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError as exc:
        raise FetchError(f"bad URL: {exc}") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchError(f"refused: only http and https URLs are allowed, not {scheme or 'none'!r}")
    if parts.username or parts.password:
        raise FetchError("refused: a URL with a user name or password")
    host = (parts.hostname or "").lower()
    if not host:
        raise FetchError("bad URL: no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise FetchError("bad URL: bad port") from None
    # Quote a space or a control character; keep every character that is already legal.
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        path += "?" + urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=-._~?")
    return scheme, host, port, path


def fetch_url(url: str, *, resolver=None, connector=None, clock=None, max_bytes: int = MAX_BYTES,
              timeout: float = TIMEOUT) -> dict:
    """Fetch one page safely. Returns ``{final_url, status, content_type, text, title}``.

    Raises ``FetchError`` on a refused scheme or address, a 4th redirect, a page over
    ``max_bytes``, a fetch over ``timeout`` seconds, a PDF, or a type that is not text.
    A final 4xx or 5xx status returns normally: the caller decides.
    """
    clock = clock or time.monotonic
    start = clock()
    current = str(url or "").strip()
    redirects = 0
    while True:
        scheme, host, port, path = _split(current)
        addrs = _resolve(host, resolver)
        ip = str(addrs[0])
        default_port = 443 if scheme == "https" else 80
        host_hdr = f"[{host}]" if ":" in host else host
        if port != default_port:
            host_hdr += f":{port}"
        headers = {"Host": host_hdr, "User-Agent": USER_AGENT,
                   "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                   "Accept-Encoding": "identity", "Connection": "close"}
        remaining = timeout - (clock() - start)
        if remaining <= 0:
            raise FetchError(f"refused: the fetch took over {timeout:g} s")
        try:
            if connector is None:
                status, rheaders, body = _http_connect(ip, port, host, "GET", path, headers, remaining,
                                                       tls=scheme == "https", max_bytes=max_bytes,
                                                       clock=clock)
            else:
                status, rheaders, body = connector(ip, port, host, "GET", path, headers, remaining)
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - socket, TLS and HTTP errors all mean "failed"
            raise FetchError(f"fetch of {host} failed: {type(exc).__name__}: {exc}"[:300]) from None
        body = body or b""
        if clock() - start > timeout:
            raise FetchError(f"refused: the fetch took over {timeout:g} s")
        if len(body) > max_bytes:
            raise FetchError(f"refused: the page is over {max_bytes:,} bytes")
        location = _header(rheaders, "Location")
        if int(status) in _REDIRECTS and location:
            redirects += 1
            if redirects > MAX_REDIRECTS:
                raise FetchError(f"refused: more than {MAX_REDIRECTS} redirects")
            current = urllib.parse.urljoin(current, location.strip())
            continue
        break

    content_type = _header(rheaders, "Content-Type")
    ctype = content_type.split(";")[0].strip().lower()
    if not 200 <= int(status) < 300:
        return {"final_url": current, "status": int(status), "content_type": content_type,
                "text": "", "title": ""}
    if ctype == "application/pdf" or body.lstrip()[:4] == b"%PDF":
        raise FetchError(PDF_ERROR)
    is_html = _looks_html(ctype, body)
    if not is_html and ctype and not ctype.startswith(_TEXT_TYPES):
        raise FetchError(f"refused: content type {ctype!r} is not text")
    title = ""
    if not body:
        text = ""
    elif is_html:
        html = body.decode(_charset(content_type), "replace")
        text = html_text(html)
        title = _html_title(html)
    else:
        text = body.decode(_charset(content_type), "replace")
    return {"final_url": current, "status": int(status), "content_type": content_type,
            "text": text, "title": title}


def store_fetched(conn, url: str, *, kind: str = "web", ticker: Optional[str] = None, now,
                  published_at: Optional[str] = None, **seams) -> int:
    """Fetch ``url`` with ``fetch_url`` and store the text. Returns the document id.

    ``kind`` is ``web`` (research only) or ``news``. For ``news``, the title and
    ``published_at`` come from the ``news_items`` row (the feed, never the page), and that
    row's ``document_id`` points to the new document. Raises ``FetchError`` for a status
    that is not 2xx or a page with no text, and stores nothing then.
    """
    if kind not in ("web", "news"):
        raise ValueError("kind must be 'web' or 'news'")
    now_dt = store.parse_ts(now)
    if now_dt is None:
        raise ValueError(f"bad time {now!r}")
    now_s = store.iso(now_dt)
    ticker = str(ticker).strip().upper() if ticker else None
    res = fetch_url(url, **seams)
    status = res["status"]
    if not 200 <= status < 300:
        raise FetchError(f"HTTP {status} from {urllib.parse.urlsplit(res['final_url']).hostname}")
    text = (res["text"] or "").strip()
    if not text:
        raise FetchError("the page has no text")
    url_hash = store.sha256_hex(url)
    title = res.get("title") or ""
    if kind == "news":
        sql = "SELECT title, published_at FROM news_items WHERE url_hash=?"
        args = [url_hash]
        if ticker:
            sql += " AND ticker=?"
            args.append(ticker)
        row = conn.execute(sql + " ORDER BY seen_at LIMIT 1", args).fetchone()
        if row:
            title = row[0] or title
            published_at = published_at or row[1]
    title = title or res["final_url"]
    doc_id = store.store_document(conn, kind=kind, title=title, text=text, now=now_s, ticker=ticker,
                                  url=res["final_url"], published_at=published_at)
    if kind == "news":
        sql = "UPDATE news_items SET document_id=? WHERE url_hash=?"
        args = [doc_id, url_hash]
        if ticker:
            sql += " AND ticker=?"
            args.append(ticker)
        conn.execute(sql, args)
    log.info("fetched %s -> %s document %s (%s chars)", res["final_url"], kind, doc_id, len(text))
    return doc_id
