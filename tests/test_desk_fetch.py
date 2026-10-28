"""Safe URL fetch (plan §7.9): scheme and address checks, IP pinning, redirects, caps, store.

Every fetch goes through ``FakeResolver`` and ``FakeConn``. The page body is the real
``ex991`` capture (the ANET Q2 2026 EX-99.1, served by sec.gov). A redirect or a refusal
is a status and a header with no body. One last case runs the production connector
against a loopback ``http.server`` with ``fetch_url``'s checks bypassed.
"""

from __future__ import annotations

import http.server
import threading

from desk_fakes import FakeClock, FakeConn, FakeResolver, Results, fx, fx_bytes, temp_root

from lib.desk import documents, news, sections, store
from lib.desk.documents import FetchError, PDF_ERROR
from lib.dataflows.yfinance_news import fetch_news_items

r = Results("desk-fetch")
NOW = "2026-10-28T15:00:00Z"
SEC = "www.sec.gov"
SEC_IP = "23.53.40.98"
PATH = "/Archives/edgar/data/1596532/000159653226000174/ex991q226-earningsrelease.htm"
URL = f"https://{SEC}{PATH}"
HTML = fx_bytes("ex991")
HTML_CT = {"Content-Type": "text/html; charset=utf-8"}


def _fake(table=None):
    res = FakeResolver(table or {SEC: [SEC_IP]})
    conn = FakeConn()
    return res, conn


# --- scheme ----------------------------------------------------------------------
for bad in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/1",
            "javascript:alert(1)", "data:text/html,hi", "//example.com/x"):
    res, conn = _fake({"example.com": ["93.184.216.34"]})
    r.raises(f"scheme: {bad.split(':')[0] or 'none'} refused", lambda: documents.fetch_url(
        bad, resolver=res, connector=conn), FetchError, "only http and https")
    r.eq(f"scheme: {bad[:12]} refused before DNS", (res.calls, conn.calls), ([], []))
res, conn = _fake()
r.raises("scheme: a user name in the URL is refused",
         lambda: documents.fetch_url(f"https://user:pw@{SEC}/", resolver=res, connector=conn),
         FetchError, "user name")

# --- addrs -----------------------------------------------------------------------
BAD = ["10.0.0.5", "172.16.0.1", "192.168.1.1", "127.0.0.1", "::1", "169.254.169.254", "fe80::1",
       "224.0.0.1", "240.0.0.1", "0.0.0.0", "100.64.0.1", "::ffff:169.254.169.254"]
for addr in BAD:
    res, conn = _fake({"evil.example": [addr]})
    r.raises(f"addrs: a DNS answer {addr} refused",
             lambda: documents.fetch_url("https://evil.example/", resolver=res, connector=conn),
             FetchError, "not a public address")
    r.eq(f"addrs: {addr} gets no connection", conn.calls, [])
    host = f"[{addr}]" if ":" in addr else addr
    res, conn = _fake()
    r.raises(f"addrs: a literal URL host {addr} refused",
             lambda: documents.fetch_url(f"http://{host}/latest/meta-data/", resolver=res, connector=conn),
             FetchError, "not a public address")
    r.eq(f"addrs: a literal {addr} needs no DNS and gets no connection", (res.calls, conn.calls), ([], []))
res, conn = _fake({"mixed.example": ["93.184.216.34", "10.0.0.1"]})
r.raises("addrs: an answer [93.184.216.34, 10.0.0.1] refused",
         lambda: documents.fetch_url("https://mixed.example/", resolver=res, connector=conn),
         FetchError, "10.0.0.1")
r.eq("addrs: the mixed answer gets no connection", conn.calls, [])
for extra in ("ff02::1", "64:ff9b::a9fe:a9fe", "::", "2002:a9fe:a9fe::1", "198.18.0.1", "192.0.2.1"):
    r.raises(f"addrs: {extra} refused", lambda: documents.check_address(extra), FetchError)
r.eq("addrs: a public IPv4 passes", str(documents.check_address("93.184.216.34")), "93.184.216.34")
r.eq("addrs: a public IPv6 passes", str(documents.check_address("2606:2800:220:1:248:1893:25c8:1946")),
     "2606:2800:220:1:248:1893:25c8:1946")
res = FakeResolver({})
r.raises("addrs: a host that does not resolve is refused",
         lambda: documents.fetch_url("https://nohost.example/", resolver=res, connector=FakeConn()),
         FetchError, "cannot resolve")

# --- pin -------------------------------------------------------------------------
res, conn = _fake()
conn.add(SEC_IP, PATH, 200, HTML, HTML_CT)
out = documents.fetch_url(URL, resolver=res, connector=conn)
call = conn.calls[0]
r.eq("pin: the connector gets the checked IP", call["ip"], SEC_IP)
r.eq("pin: SNI host = the name", call["host"], SEC)
r.eq("pin: Host header = the name", call["headers"].get("Host"), SEC)
r.eq("pin: port 443 for https", call["port"], 443)
r.eq("pin: GET with the path", (call["method"], call["path"]), ("GET", PATH))
r.eq("pin: one DNS call", res.calls, [SEC])
r.eq("pin: status 200", out["status"], 200)
r.eq("pin: final_url is the URL", out["final_url"], URL)
r.ok("pin: the connector timeout is at most 20 s", 0 < call["timeout"] <= 20, call["timeout"])
res, conn = _fake({"example.com": ["93.184.216.34"]})
conn.add("93.184.216.34", "/a%20b?q=x%20y", 200, HTML, HTML_CT)
documents.fetch_url("https://example.com/a b?q=x y", resolver=res, connector=conn)
r.eq("pin: a space in the path is quoted", conn.calls[0]["path"], "/a%20b?q=x%20y")


def _reset(*a):
    raise ConnectionResetError("peer reset")


r.raises("pin: a connection error is a FetchError", lambda: documents.fetch_url(
    "https://example.com/", resolver=res, connector=_reset), FetchError, "peer reset")
res, conn = _fake({"example.com": ["93.184.216.34"]})
conn.add("93.184.216.34", "/a?x=1", 200, HTML, HTML_CT)
documents.fetch_url("http://example.com:8080/a?x=1", resolver=res, connector=conn)
r.eq("pin: a port that is not the default goes in the Host header",
     (conn.calls[0]["port"], conn.calls[0]["headers"]["Host"], conn.calls[0]["path"]),
     (8080, "example.com:8080", "/a?x=1"))

# --- redirects -------------------------------------------------------------------
def _chain(n_redirects, last_location=None):
    res = FakeResolver({f"h{i}.example": ["93.184.216.34"] for i in range(6)} | {SEC: [SEC_IP]})
    conn = FakeConn()
    for i in range(n_redirects):
        loc = f"https://h{i + 1}.example/p{i + 1}"
        if i == n_redirects - 1 and last_location:
            loc = last_location
        conn.add("93.184.216.34", f"/p{i}", 302, b"", {"Location": loc})
    conn.add("93.184.216.34", f"/p{n_redirects}", 200, HTML, HTML_CT)
    return res, conn


res, conn = _chain(3)
out = documents.fetch_url("https://h0.example/p0", resolver=res, connector=conn)
r.eq("redirects: 3 pass", (out["status"], out["final_url"]), (200, "https://h3.example/p3"))
r.eq("redirects: one DNS call per hop", res.calls, ["h0.example", "h1.example", "h2.example", "h3.example"])
r.eq("redirects: each hop pins its own host", [c["host"] for c in conn.calls],
     ["h0.example", "h1.example", "h2.example", "h3.example"])
res, conn = _chain(4)
r.raises("redirects: a 4th is refused",
         lambda: documents.fetch_url("https://h0.example/p0", resolver=res, connector=conn),
         FetchError, "more than 3 redirects")
r.eq("redirects: the 4th target is never fetched", len(conn.calls), 4)
res, conn = _chain(1, "http://169.254.169.254/latest/meta-data/")
r.raises("redirects: one to 169.254.169.254 is refused",
         lambda: documents.fetch_url("https://h0.example/p0", resolver=res, connector=conn),
         FetchError, "not a public address")
r.ok("redirects: the metadata server gets no connection",
     all(c["ip"] != "169.254.169.254" for c in conn.calls), conn.calls)
res, conn = _chain(1, "https://h9.internal/")
res.table["h9.internal"] = ["10.1.2.3"]
r.raises("redirects: one to a host with a private answer is refused",
         lambda: documents.fetch_url("https://h0.example/p0", resolver=res, connector=conn),
         FetchError, "not a public address")
res, conn = _chain(1, "ftp://h1.example/p1")
r.raises("redirects: one to ftp: is refused",
         lambda: documents.fetch_url("https://h0.example/p0", resolver=res, connector=conn),
         FetchError, "only http and https")
res, conn = _chain(1, "/p1")
out = documents.fetch_url("https://h0.example/p0", resolver=res, connector=conn)
r.eq("redirects: a relative Location resolves", out["final_url"], "https://h0.example/p1")
r.eq("redirects: the relative hop goes to the same host", conn.calls[-1]["host"], "h0.example")

# --- caps ------------------------------------------------------------------------
BIG = (HTML * (5_000_001 // len(HTML) + 1))[:5_000_001]
with temp_root() as root, root.led().connect() as db:
    res, conn = _fake()
    conn.add(SEC_IP, PATH, 200, BIG, HTML_CT)
    r.raises("caps: 5 MB + 1 byte aborts",
             lambda: documents.store_fetched(db, URL, now=NOW, resolver=res, connector=conn),
             FetchError, "over 5,000,000 bytes")
    r.eq("caps: 5 MB + 1 byte stores no row", db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
    res, conn = _fake()
    conn.add(SEC_IP, PATH, 200, BIG[:5_000_000], HTML_CT)
    out = documents.fetch_url(URL, resolver=res, connector=conn)
    r.eq("caps: exactly 5 MB passes", out["status"], 200)

    clock = FakeClock()
    res, conn = _fake()
    conn.add(SEC_IP, PATH, 200, HTML, HTML_CT)

    def _slow(*a):
        clock.advance(21)
        return conn(*a)
    r.raises("caps: 20 s on FakeClock aborts",
             lambda: documents.store_fetched(db, URL, now=NOW, resolver=res, connector=_slow, clock=clock),
             FetchError, "over 20 s")
    r.eq("caps: the slow fetch stores no row", db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    # Three hops of 7 s each: the 20 s budget covers the whole fetch, not one hop.
    clock = FakeClock()
    res, conn = _chain(2)
    seen_timeouts = []

    def _seven(ip, port, host, method, path, headers, timeout):
        seen_timeouts.append(timeout)
        clock.advance(7)
        return conn(ip, port, host, method, path, headers, timeout)
    r.raises("caps: 3 hops of 7 s abort at 20 s",
             lambda: documents.fetch_url("https://h0.example/p0", resolver=res, connector=_seven, clock=clock),
             FetchError, "over 20 s")
    r.eq("caps: each hop gets the time that is left", seen_timeouts, [20.0, 13.0, 6.0])

    res, conn = _fake()
    conn.add(SEC_IP, "/r.pdf", 200, b"", {"Content-Type": "application/pdf"})
    r.raises("caps: application/pdf refused",
             lambda: documents.fetch_url(f"https://{SEC}/r.pdf", resolver=res, connector=conn),
             FetchError, PDF_ERROR)
    e = r.raises("caps: the PDF error text is exact",
                 lambda: documents.fetch_url(f"https://{SEC}/r.pdf", resolver=res, connector=conn), FetchError)
    r.eq("caps: the PDF error text", str(e), "PDF not supported; use the EDGAR exhibit")
    # No PDF capture exists; the check reads only the first bytes, so the body is the header.
    conn.add(SEC_IP, "/r2", 200, b"%PDF-1.7\n", HTML_CT)
    r.raises("caps: a %PDF body refused", lambda: documents.fetch_url(
        f"https://{SEC}/r2", resolver=res, connector=conn), FetchError, PDF_ERROR)
    conn.add(SEC_IP, "/img", 200, b"", {"Content-Type": "image/png"})
    r.raises("caps: an image type is refused", lambda: documents.fetch_url(
        f"https://{SEC}/img", resolver=res, connector=conn), FetchError, "not text")

# --- store -----------------------------------------------------------------------
with temp_root() as root, root.led().connect() as db:
    res, conn = _fake()
    conn.add(SEC_IP, PATH, 200, HTML, HTML_CT)
    out = documents.fetch_url(URL, resolver=res, connector=conn)
    want = sections.html_to_text(HTML.decode("utf-8"))
    r.eq("store: fetch_url text is the §7.4 converter output", out["text"], want)
    r.ok("store: the text holds the release headline",
         "Arista Networks, Inc. Reports Second Quarter 2026 Financial Results" in out["text"], out["text"][:200])
    doc_id = documents.store_fetched(db, URL, now=NOW, resolver=res, connector=conn)
    doc = store.get_document(db, doc_id)
    r.eq("store: kind web", doc["kind"], "web")
    r.eq("store: the stored text is the §7.4 text", doc["text"], want.strip())
    r.eq("store: the url is the final URL", doc["url"], URL)
    again = documents.store_fetched(db, URL, now="2026-10-28T16:00:00Z", resolver=res, connector=conn)
    r.eq("store: the same page twice -> one id", again, doc_id)
    r.eq("store: one row", db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)

    res403, conn403 = _fake()
    conn403.add(SEC_IP, "/paywall", 403, b"", {})
    r.raises("store: a 403 stores nothing", lambda: documents.store_fetched(
        db, f"https://{SEC}/paywall", now=NOW, resolver=res403, connector=conn403), FetchError, "HTTP 403")

    # A news fetch: the feed row (real producer: fetch_news_items over yf_news) links the document.
    feed = fx("yf_news")["news"]
    store.upsert_company(db, ticker="ANET", kind="company", status="holding", now=NOW)
    news.ingest(db, ["ANET"], now=NOW,
                fetch_fn=lambda t, n: fetch_news_items(t, n, search_fn=lambda q, c: feed))
    item = db.execute("SELECT * FROM news_items WHERE ticker='ANET' ORDER BY published_at DESC LIMIT 1").fetchone()
    host = "finance.yahoo.com"
    path = item["url"].split(host, 1)[1]
    resn = FakeResolver({host: ["69.147.88.8"]})
    # No Yahoo article page was captured; the ex991 capture stands in as the article body.
    conn.add("69.147.88.8", path, 200, HTML, HTML_CT)
    nid = documents.store_fetched(db, item["url"], kind="news", ticker="ANET", now=NOW, resolver=resn,
                                  connector=conn)
    ndoc = store.get_document(db, nid)
    r.eq("store: kind news", ndoc["kind"], "news")
    r.ok("store: a news document is a new row (kind is part of the key)", nid != doc_id, (nid, doc_id))
    linked = db.execute("SELECT document_id FROM news_items WHERE url_hash=? AND ticker='ANET'",
                        (item["url_hash"],)).fetchone()[0]
    r.eq("store: news_items links the document", linked, nid)
    r.eq("store: published_at comes from the feed", ndoc["published_at"], item["published_at"])
    r.eq("store: the title comes from the feed", ndoc["title"], item["title"])
    r.eq("store: the url_hash is SHA-256 of the URL", item["url_hash"], store.sha256_hex(item["url"]))
    r.raises("store: a bad kind is refused", lambda: documents.store_fetched(db, URL, kind="filing_section",
             now=NOW, resolver=res, connector=conn), ValueError)


# --- the production connector (loopback; fetch_url's address check is bypassed) ------
class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802
        _Handler.seen.append((self.path, self.headers.get("Host")))
        body = HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
try:
    port = srv.server_address[1]
    status, hdrs, body = documents._http_connect(
        "127.0.0.1", port, "pinned.example", "GET", "/x?y=1",
        {"Host": "pinned.example", "Accept-Encoding": "identity"}, 5.0, tls=False, max_bytes=1000)
    r.eq("connector: status", status, 200)
    r.eq("connector: the Host header is the name, not the IP", _Handler.seen[-1], ("/x?y=1", "pinned.example"))
    r.eq("connector: it reads at most max_bytes + 1", len(body), 1001)
    status, hdrs, body = documents._http_connect(
        "127.0.0.1", port, "pinned.example", "GET", "/", {"Host": "pinned.example"}, 5.0, tls=False,
        max_bytes=10_000_000)
    r.eq("connector: a page under the cap is read whole", body, HTML)
finally:
    srv.shutdown()
    srv.server_close()

# Gate B SEC-3: a server on the loopback address that drips the status line, the headers or
# the body one byte each 0.3 s. A socket timeout limits one read only; the deadline timer
# must end the whole fetch at its time limit (1 s here), not after 7 to 9 s.
import socket as _socket  # noqa: E402
import time as _time  # noqa: E402


def _drip_server(mode):
    ls = _socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)

    def run():
        c, _ = ls.accept()
        c.recv(4096)
        head = {"status": b"HTTP/1.1 200 OK\r\n",
                "headers": b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n",
                "body": b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 100\r\n\r\n"}[mode]
        try:
            if mode == "body":
                c.sendall(head)
                head = b"x" * 100
            for b in head:
                c.send(bytes([b]))
                _time.sleep(0.3)
        except OSError:
            pass
        c.close()
        ls.close()
    threading.Thread(target=run, daemon=True).start()
    return ls.getsockname()[1]


for _mode in ("status", "headers", "body"):
    _port = _drip_server(_mode)
    _t0 = _time.monotonic()
    _err = None
    try:
        documents._http_connect("127.0.0.1", _port, "drip.example", "GET", "/", {"Host": "drip.example"}, 1.0,
                                tls=False, max_bytes=10_000)
    except documents.FetchError as e:
        _err = str(e)
    _took = _time.monotonic() - _t0
    r.ok(f"deadline: a dripped {_mode} fails with the time-limit error", bool(_err) and "took over" in _err, _err)
    r.ok(f"deadline: a dripped {_mode} ends within 2 s of a 1 s limit", _took < 2.0, round(_took, 2))

raise SystemExit(r.done())
