"""Shared fakes for the desk suites (plan §18.1 rule 5).

No network, no LLM, no broker. Every fake records its calls so a test can assert on them.
Fixtures are the real captures in ``tests/fixtures/desk`` (§18.3); ``fx(id)`` loads one.

Seams (§18.2), and the contract each fake implements:
  * opener(url, headers, timeout) -> (status:int, headers:dict, body:bytes). A body may be
    gzip bytes when headers has "Content-Encoding: gzip". It never raises for an HTTP status.
  * sleep(seconds), clock() -> float (monotonic seconds). ``FakeClock`` gives both.
  * resolver(host) -> [ip, ...] and connector(ip, port, host, method, path, headers, timeout)
    -> (status, headers, body). ``FakeResolver`` and ``FakeConn`` give them.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
FIX = REPO / "tests" / "fixtures" / "desk"

# §18.3 id -> file name.
FX = {
    "sub_anet": "sub_0001596532.json.gz",
    "sub_spcx": "sub_0001181412.json.gz",
    "cf_spcx": "cf_0001181412.json.gz",
    "cf_anet": "cf_anet.json.gz",
    "10k": "anet10k.htm.gz",
    "10q_q2": "anet10q.htm.gz",
    "10q_q1": "anet10q_q1.htm.gz",
    "ex991": "anet_ex991.htm.gz",
    "s1": "spcx424b4.htm.gz",
    "spcx_ex991": "spcx_ex991.htm.gz",
    "tick_co": "company_tickers.json.gz",
    "tick_mf": "company_tickers_mf.json.gz",
    "nport_soxx": "soxx_nport.xml.gz",
    "nport_bot": "bot_nport.xml.gz",
    "nport_smh": "smh_nport.xml.gz",
    "feed_soxx": "nport_S000004354.xml.gz",
    "av_anet": "av_anet_2026Q2.json.gz",
    "av_empty": "av_empty.json.gz",
    "av_err": "av_err.json.gz",            # apikey=invalid: Alpha Vantage still returns the transcript
    "idx_spcx": "idx_spcx.htm.gz",
    "idx_anet": "idx_anet.htm.gz",
    "body_anet": "body_anet.htm.gz",
    "sub_ceg": "sub_0001868275.json.gz",
    "cf_ceg": "cf_ceg.json.gz",
    "10k_ceg": "10k_ceg.htm.gz",
    "10q_ceg": "10q_ceg.htm.gz",
    "idx_ceg": "idx_ceg.htm.gz",
    "ex991_ceg": "ex991_ceg.htm.gz",
    "body_ceg": "body_ceg.htm.gz",
    "sub_tsm": "sub_tsm.json.gz",
    "cf_tsm": "cf_tsm.json.gz",
    "yf_news": "yf_news.json.gz",
    "yf_news_ceg": "yf_news_ceg.json.gz",
    "yf_top": "yf_top.json.gz",
    "rh_snap": "rh_snap.json.gz",
    "rh_review_buy": "rh_review_buy.json.gz",
    "rh_review_sell": "rh_review_sell.json.gz",
    "rh_orders_live": "rh_orders_live.json.gz",
    "rh_tools": "rh_tools.json.gz",
    "pre_db": "pre_db.db.gz",
    "tg": "tg.json.gz",
}


def fx_bytes(fid: str) -> bytes:
    """The decompressed bytes of a fixture."""
    return gzip.decompress((FIX / FX[fid]).read_bytes())


def fx(fid: str):
    """A fixture as data: JSON parsed, HTML/XML as text, a database as bytes."""
    name = FX[fid]
    raw = fx_bytes(fid)
    if name.endswith(".json.gz"):
        return json.loads(raw)
    if name.endswith(".db.gz"):
        return raw
    return raw.decode("utf-8", "replace")


class Results:
    """The plain-assert harness every desk suite uses (§18.1 rule 1)."""

    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = []
        self.skipped = 0

    def ok(self, label: str, cond, extra=None):
        if cond:
            self.passed += 1
        else:
            self.failed.append(label)
            print(f"  FAIL {label}   -> {extra!r}"[:2000])

    def eq(self, label: str, got, want):
        self.ok(label, got == want, {"got": got, "want": want})

    def raises(self, label: str, fn, exc=Exception, contains: str = ""):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            if isinstance(e, exc):
                self.ok(label, contains in str(e), str(e))
            else:
                self.ok(label, False, f"wrong exception {type(e).__name__}: {e}")
            return e
        self.ok(label, False, "no exception")
        return None

    def skip(self, n: int = 1):
        self.skipped += n

    def done(self) -> int:
        tail = f", {self.skipped} skipped" if self.skipped else ""
        print(f"{self.name}: {self.passed} passed, {len(self.failed)} failed{tail}")
        return 1 if self.failed else 0


class FakeClock:
    """A pinned clock. ``sleep`` advances it; ``pin_market`` makes ``lib.market.now_et``
    and ``lib.desk.store.utc_now`` read it too."""

    def __init__(self, start: datetime | str = "2026-08-05T14:00:00Z"):
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self.now = start
        self.mono = 1000.0
        self.sleeps = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)
        self.mono += seconds
        self._sync()

    def set(self, when: datetime | str) -> None:
        if isinstance(when, str):
            when = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        self.now = when
        self._sync()

    def monotonic(self) -> float:
        return self.mono

    def __call__(self) -> float:
        return self.mono

    def _sync(self) -> None:
        from lib import market
        if getattr(self, "_pinned", False):
            market.set_clock(self.now)

    def pin_market(self):
        from lib import market
        self._pinned = True
        market.set_clock(self.now)
        return self

    def unpin(self) -> None:
        from lib import market
        self._pinned = False
        market.set_clock(None)


class FakeOpener:
    """Scripted HTTP. ``add(url_or_prefix, status, body, headers)`` queues answers; a
    list of answers for one key is replayed in order, then the last repeats. Records
    every request as (url, headers)."""

    def __init__(self):
        self.routes = []      # (key, [answers], exact)
        self.calls = []

    def add(self, key: str, status: int = 200, body=b"", headers=None, *, exact: bool = False,
            gzip_body: bool = False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        hdrs = dict(headers or {})
        if gzip_body:
            body = gzip.compress(body)
            hdrs["Content-Encoding"] = "gzip"
        for k, answers, ex in self.routes:
            if k == key and ex == exact:
                answers.append((status, hdrs, body))
                return self
        self.routes.append((key, [(status, hdrs, body)], exact))
        return self

    def __call__(self, url: str, headers: dict, timeout: float):
        self.calls.append((url, dict(headers or {})))
        best = None
        for key, answers, exact in self.routes:
            if (exact and url == key) or (not exact and url.startswith(key)):
                if best is None or len(key) > len(best[0]):
                    best = (key, answers)
        if best is None:
            return (404, {}, b"not found")
        answers = best[1]
        ans = answers.pop(0) if len(answers) > 1 else answers[0]
        return ans


class FakeResolver:
    """host -> [ip]. Records each lookup."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def __call__(self, host: str):
        self.calls.append(host)
        if host not in self.table:
            raise OSError(f"no such host {host}")
        return list(self.table[host])


class FakeConn:
    """A scripted connector: (ip, path) -> (status, headers, body). Records each call
    as a dict with ip, port, host (SNI and Host header), method, path, headers."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, ip: str, path: str, status=200, body=b"", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.routes[(ip, path)] = (status, dict(headers or {}), body)
        return self

    def __call__(self, ip, port, host, method, path, headers, timeout):
        self.calls.append({"ip": ip, "port": port, "host": host, "method": method,
                           "path": path, "headers": dict(headers or {}), "timeout": timeout})
        return self.routes.get((ip, path), (404, {}, b""))


class TempRoot:
    """A temp QUIVER_ROOT (§18.1 rule 2): config.yaml, state/tmp, state/triggers,
    logs/desk, and a fresh ledger. Never touches state/ledger.db."""

    def __init__(self, *, strategy_text: str | None = None, extra_config: str = ""):
        self.path = Path(tempfile.mkdtemp(prefix="quiver_desk_"))
        for d in ("state/tmp", "state/triggers", "logs/desk"):
            (self.path / d).mkdir(parents=True, exist_ok=True)
        cfg = (REPO / "config.yaml").read_text(encoding="utf-8") + extra_config
        (self.path / "config.yaml").write_text(cfg, encoding="utf-8")
        if strategy_text is not None:
            (self.path / "strategy.yaml").write_text(strategy_text, encoding="utf-8")
        self.db = self.path / "state" / "ledger.db"
        self._env_prev = {}
        self.env = {"QUIVER_ROOT": str(self.path), "QUIVER_LEDGER_DB": str(self.db),
                    "QUIVER_CONFIG": str(self.path / "config.yaml"),
                    "RH_ACCOUNT_NUMBER": "TEST1234", "SEC_USER_AGENT": "Quiver test@example.com",
                    "QUIVER_TELEGRAM_DISABLE": "1"}

    def __enter__(self):
        for k, v in self.env.items():
            self._env_prev[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self._env_prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.path, ignore_errors=True)
        return False

    def led(self):
        from lib.ledger import Ledger
        return Ledger(self.db)

    def cfg(self):
        from lib.config import load_config
        return load_config(self.path / "config.yaml")

    @property
    def kill(self) -> Path:
        return self.path / "KILL"


def temp_root(**kw) -> TempRoot:
    return TempRoot(**kw)


# --- producers (§18.1 rule 4): ledger state through the real code -----------------

FX["ex992_ceg"] = "ex992_ceg.htm.gz"

# SEC URLs of the captured files, keyed by the URL's last path part (or the full URL).
SEC_ROUTES = {
    "company_tickers.json": "tick_co",
    "company_tickers_mf.json": "tick_mf",
    "CIK0001596532.json": None,          # submissions + companyfacts: resolved by host below
    "anet-20251231.htm": "10k",
    "anet-20260630.htm": "10q_q2",
    "anet-20260331.htm": "10q_q1",
    "anet-20260804.htm": "body_anet",
    "0001596532-26-000174-index.htm": "idx_anet",
    "ex991q226-earningsrelease.htm": "ex991",
    "ceg-20251231.htm": "10k_ceg",
    "ceg-20260630.htm": "10q_ceg",
    "ceg-20260806.htm": "body_ceg",
    "0001868275-26-000097-index.htm": "idx_ceg",
    "ceg-20260806991.htm": "ex991_ceg",
    "ceg-20260806992.htm": "ex992_ceg",
    "0001628280-26-066482-index.htm": "idx_spcx",
    "spaceexplorationtechnologi.htm": "spcx_ex991",
}
SUBMISSIONS = {"0001596532": "sub_anet", "0001868275": "sub_ceg", "0001181412": "sub_spcx",
               "0001046179": "sub_tsm"}
COMPANYFACTS = {"0001596532": "cf_anet", "0001868275": "cf_ceg", "0001181412": "cf_spcx",
                "0001046179": "cf_tsm"}


class SecFixtures:
    """An opener (the edgar seam) that answers SEC URLs from the captured files. Records
    each URL. ``drop`` maps a fixture id to a function that edits its parsed JSON (a test
    may change values or drop entries; it never adds a key). A URL with no capture is 404."""

    def __init__(self, drop=None, missing_ok=True):
        self.calls = []
        self.drop = dict(drop or {})

    def body(self, fid: str) -> bytes:
        raw = fx_bytes(fid)
        if fid in self.drop:
            data = json.loads(raw)
            data = self.drop[fid](data) or data
            raw = json.dumps(data).encode()
        return raw

    def __call__(self, url: str, headers: dict, timeout: float):
        self.calls.append(url)
        last = url.rsplit("/", 1)[-1].split("?")[0]
        if "/submissions/" in url:
            cik = last.replace("CIK", "").replace(".json", "")
            fid = SUBMISSIONS.get(cik)
        elif "/companyfacts/" in url:
            cik = last.replace("CIK", "").replace(".json", "")
            fid = COMPANYFACTS.get(cik)
        else:
            fid = SEC_ROUTES.get(last)
        if not fid:
            return (404, {}, b"not captured")
        return (200, {}, self.body(fid))


TEST_BOOK = """schema: 1
goal:
  target_return_pct: 15
  horizon_months: 12
  benchmark: "SGOV ~3.6%"
  benchmark_annual_pct: 3.6
  constraint: "test book"
macro_thesis:
  version: "2026-10-27"
  summary: >
    The growth of AI and of the energy it needs. Networking for AI clusters and
    reliable power are the two bottlenecks this test book owns.
  catalysts_to_watch: ["earnings"]
  deploy_trigger_pce_pct: 2.5
  standdown_trigger_pce_pct: 3.5
  correlation_note: "One AI bet."
rh_tradable_confirmed: [ANET, CEG, SGOV]
risk_policy:
  per_name_max_pct: 30
books:
  core_test:
    default: true
    enabled: true
    holdings:
      - {ticker: ANET, sleeve: AI Networking, weight: 25, band: 5}
      - {ticker: CEG, sleeve: Power, weight: 15, band: 5}
      - {ticker: SGOV, sleeve: Cash, weight: 60, band: 10}
"""


class FakePrices:
    """The yfinance seams: a settable last price per ticker."""

    def __init__(self, prices=None):
        self.prices = dict(prices or {"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0})

    def __call__(self, ticker):
        t = str(ticker).upper()
        if t not in self.prices:
            raise KeyError(t)
        return self.prices[t]


def seams(*, opener=None, prices=None, av_key=""):
    """Ingestion seams over the fixtures (no network)."""
    from lib.desk import edgar, events
    op = opener or SecFixtures()
    fc = FakeClock()
    client = edgar.EdgarClient("Quiver test@example.com", opener=op, sleep=fc.sleep, clock=fc.monotonic)
    px = prices or FakePrices()
    return events.Seams(client=client, price_fn=px, market_cap_fn=lambda t: None,
                        technicals_fn=lambda t: None, next_earnings_fn=lambda t: None,
                        top_fn=lambda t: (_ for _ in ()).throw(RuntimeError("no yfinance in tests")),
                        av_key=av_key)


def seed_desk(root: "TempRoot", *, book: str = TEST_BOOK, load: bool = True, opener=None, prices=None):
    """``seed``: a temp ledger made by the real ``desk-seed`` (the TEST_BOOK book, CEG and
    ANET loaded from the captured SEC files, no transcript). Returns the desk_seed output."""
    import tick
    from lib.desk import seed
    (root.path / "strategy.yaml").write_text(book, encoding="utf-8")
    cfg = root.cfg()
    led = root.led()
    sent = []
    out = seed.desk_seed(led, cfg, root=root.path, run_strategy_set=tick._run_strategy_set,
                         seams=seams(opener=opener, prices=prices), send=lambda t: sent.append(t) or {"ok": True},
                         skip_load=not load, skip_intro=False)
    out["intro_texts"] = sent
    return out


def rh_snap_file(*, positions=None, buying_power=None, equity=None, quotes=None, at=None,
                 approval=None, drop_positions=True):
    """The captured ``rh_snap`` with VALUES changed (rule 4). ``positions`` maps a symbol to
    a quantity: the captured entries are kept in order and their symbol and quantity values
    set (no entry is added). ``quotes`` maps a symbol to a price: the matching captured
    quote entries get that price and fresh venue times at ``at`` (ISO UTC). A symbol with no
    captured quote takes an unused captured entry's slot (its symbol value changes)."""
    data = fx("rh_snap")
    at = at or "2026-08-05T14:00:00Z"
    venue = at.replace("Z", ".000000000Z")
    if positions is not None:
        plist = data["get_equity_positions"]["data"]["positions"]
        keep = []
        for (sym, qty), entry in zip(positions.items(), plist):
            entry["symbol"] = sym
            for k in ("quantity", "shares_available_for_sells"):
                entry[k] = f"{float(qty):.6f}"
            entry["shares_held_for_sells"] = "0.000000"
            keep.append(entry)
        if len(positions) > len(plist):
            raise ValueError("more positions than captured entries")
        data["get_equity_positions"]["data"]["positions"] = keep if drop_positions else plist
    if quotes is not None:
        res = data["get_equity_quotes"]["data"]["results"]
        used = []
        for sym, price in quotes.items():
            entry = next((r for r in res if r["quote"]["symbol"] == sym and r not in used), None)
            if entry is None:
                entry = next(r for r in res if r not in used and r["quote"]["symbol"] not in quotes)
                entry["quote"]["symbol"] = sym
                entry["close"]["symbol"] = sym
            q = entry["quote"]
            for k in ("last_trade_price", "bid_price", "ask_price"):
                q[k] = f"{float(price):.6f}"
            for k in ("venue_last_trade_time", "venue_bid_time", "venue_ask_time"):
                q[k] = venue
            used.append(entry)
        data["get_equity_quotes"]["data"]["results"] = used
    pf = data["get_portfolio"]["data"]
    if buying_power is not None:
        pf["buying_power"]["buying_power"] = f"{float(buying_power):.4f}"
        pf["cash"] = f"{float(buying_power):.2f}"
    if equity is not None:
        pf["total_value"] = f"{float(equity):.6f}"
    if approval is not None:
        data["get_trade_approval_setting"]["data"]["setting"]["human_must_approve_trades"] = approval
    return data


def write_tmp(root: "TempRoot", name: str, obj) -> str:
    """Write a runbook file into the temp root's ``state/tmp`` and return its relative path."""
    p = root.path / "state" / "tmp" / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return f"state/tmp/{name}"


STD_SNAP = {"positions": {"ANET": 2, "CEG": 1, "SGOV": 11},
            "quotes": {"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0}, "buying_power": 300.0, "equity": 2000.0}


def snapshot(root: "TempRoot", *, at="2026-08-05T13:35:00Z", **kw) -> dict:
    """``snap``: the real ``snapshot-commit`` on ``rh_snap`` with the §18.4 values (ANET 2 sh at
    $150.00, CEG 1 at $300.00, SGOV 11 at $100.00, buying power $300.00: deployable $2,000)."""
    from lib.desk import execution
    vals = {**STD_SNAP, **kw}
    data = rh_snap_file(positions=vals["positions"], quotes=vals["quotes"], buying_power=vals["buying_power"],
                        equity=vals["equity"], at=at, approval=vals.get("approval"))
    cfg = root.cfg()
    led = root.led()
    from lib.desk import store
    return execution.snapshot_commit(led, cfg, data, now_dt=store.parse_ts(at))
