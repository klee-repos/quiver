"""``tick.py desk-seed``: the one-time cutover seed (plan §16.1).

It is idempotent: a second run changes nothing. Each step writes only what is absent.
After the seed, the ledger holds the premise, the book and the settings, and only a
confirmed proposal changes them.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from lib import market
from lib.desk import edgar, events, settings as desk_settings, store

INTRO = ("I'm switched over. From now on nothing trades unless you tap Approve.\n\n"
         "I'm reading each holding's filings now, starting with the largest. For each one I'll "
         "send you a thesis to confirm.\n\n"
         "Ask me anything while I work.")


def _macro_text(strategy_path: str) -> str:
    """The ``macro_thesis`` block of strategy.yaml as plain text."""
    import yaml
    try:
        raw = yaml.safe_load(Path(strategy_path).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return ""
    mt = raw.get("macro_thesis") or {}
    parts = []
    if mt.get("summary"):
        parts.append(" ".join(str(mt["summary"]).split()))
    cats = mt.get("catalysts_to_watch") or []
    if cats:
        parts.append("Catalysts to watch: " + "; ".join(str(c) for c in cats) + ".")
    if mt.get("correlation_note"):
        parts.append(" ".join(str(mt["correlation_note"]).split()))
    return "\n\n".join(parts)


def _strategy_md_sections(path: Path) -> str:
    """Sections 1 to 3 of STRATEGY.md (the goal, the philosophy, the macro thesis)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^## 1\..*?(?=^## 4\.|\Z)", text, flags=re.S | re.M)
    return m.group(0).strip() if m else ""


def _import_history(conn, root: Path, owner_chat: str) -> int:
    """Import the old bridge's thread files (``state/chat/history/<chat-id>.jsonl``) as
    ``chat_messages`` rows (``source='chat'``, already delivered)."""
    if store.get_state(conn, "history_imported"):
        return 0
    n = 0
    hist = root / "state" / "chat" / "history"
    for f in sorted(hist.glob("*.jsonl")) if hist.is_dir() else []:
        chat_id = f.stem
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("role") not in ("user", "assistant") or not isinstance(o.get("text"), str):
                continue
            ts = o.get("ts")
            at = (datetime.fromtimestamp(int(ts), tz=timezone.utc) if isinstance(ts, (int, float))
                  else store.utc_now())
            conn.execute("INSERT INTO chat_messages (chat_id, role, source, text, created_at, delivered_to_session) "
                         "VALUES (?, ?, 'chat', ?, ?, 1)",
                         (chat_id, "owner" if o["role"] == "user" else "desk", o["text"], store.iso(at)))
            n += 1
    store.set_state(conn, "history_imported", store.now_iso())
    return n


def desk_seed(led, cfg, *, root: Path, run_strategy_set, seams=None, send=None,
              skip_load: bool = False, skip_intro: bool = False) -> dict:
    """Run the nine §16.1 steps. ``seams`` is an ``events.Seams`` (the production one when
    None); ``send(text)`` sends the intro (the outbox to the owner when None)."""
    out = {"ok": True}
    now = store.now_iso()
    # 1b. The book: keep an active goal and its carried targets; else seed from strategy.yaml.
    goal = led.get_active_goal()
    if goal is None:
        out["strategy_set"] = run_strategy_set(cfg, led, {})
        goal = led.get_active_goal()
    else:
        out["strategy_set"] = "kept the active goal"
    if seams is None:
        seams = events.Seams(client=edgar.client_from_env(), av_key=os.environ.get("ALPHA_VANTAGE_API_KEY") or "")
    # 2. The SEC entity map and the four overrides.
    with led.connect() as conn:
        out["ticker_map"] = edgar.refresh_ticker_maps(conn, seams.client, now)
        out["overrides"] = edgar.seed_overrides(conn)
    # 3. One holding company per active book row.
    rows = led.active_target_portfolio(goal["id"], statuses=("active",))
    added = 0
    with led.connect() as conn:
        for r in rows:
            t = str(r["ticker"]).upper()
            if store.get_company(conn, t) is not None:
                if store.get_company(conn, t)["status"] != "holding":
                    store.set_company_status(conn, t, "holding", "in the seeded book", now)
                continue
            try:
                ent = edgar.resolve(conn, t)
            except edgar.UnknownTicker:
                ent = {"kind": "company", "cik": None, "series_id": None, "title": None}
            store.upsert_company(conn, ticker=t, kind=ent["kind"], status="holding", now=now,
                                 name=ent.get("title"), cik=ent.get("cik"), series_id=ent.get("series_id"),
                                 sleeve=r.get("sleeve"), status_reason="in the seeded book")
            added += 1
    out["companies"] = added
    # 4. Premise v1.
    with led.connect() as conn:
        if conn.execute("SELECT 1 FROM premise WHERE version=1").fetchone() is None:
            text = _macro_text(cfg.strategy_path)
            md = _strategy_md_sections(Path(cfg.strategy_path).parent / "STRATEGY.md")
            if md:
                text = (text + "\n\n" + md).strip()
            goal_json = {"target_return_pct": goal.get("target_return_pct"),
                         "horizon_months": goal.get("horizon_months"), "benchmark": goal.get("benchmark")}
            conn.execute("INSERT INTO premise (version, text, goal_json, confirmed_at, proposal_id) "
                         "VALUES (1, ?, ?, ?, NULL)", (text or "(no premise text in strategy.yaml)",
                                                       json.dumps(goal_json), now))
            out["premise"] = "written"
        else:
            out["premise"] = "kept"
        # 5. Settings start values.
        out["settings"] = desk_settings.seed(conn, cfg, now)
        # 6. The old chat threads.
        owner = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").replace(",", " ").split() or [""])[0]
        out["history_imported"] = _import_history(conn, Path(root), owner)
    # 7. load_company for each holding (filings as baseline, so the first sweep makes no flood).
    sett = None
    with led.connect() as conn:
        sett = desk_settings.effective(conn, cfg)
    loaded = []
    if not skip_load:
        for r in rows:
            t = str(r["ticker"]).upper()
            with led.connect() as conn:
                comp = store.get_company(conn, t)
                done = store.get_state(conn, f"seed_loaded:{t}")
            if done or comp is None or comp["kind"] == "cash":
                continue
            try:
                summary = events.load_company(led, t, settings=sett, seams=seams, status="holding",
                                              sleeve=r.get("sleeve"))
                entry = {"ticker": t, "filings": summary.get("filings"), "documents": summary.get("documents")}
                if summary.get("lookthrough_errors"):
                    entry["lookthrough_errors"] = summary["lookthrough_errors"]
                loaded.append(entry)
                if not summary.get("lookthrough_errors"):
                    with led.connect() as conn:
                        store.set_state(conn, f"seed_loaded:{t}", now)
            except Exception as e:  # noqa: BLE001 — one bad name never stops the seed
                loaded.append({"ticker": t, "error": f"{type(e).__name__}: {e}"})
    out["loaded"] = loaded
    # A failed load is not "ok": run desk-seed again before the bridge starts (§15.6 S10).
    failed = [l["ticker"] for l in loaded if l.get("error") or l.get("lookthrough_errors")]
    if failed:
        out["ok"] = False
        out["failed"] = failed
    # 8. One deep-dive per holding, largest position first (the cash sleeve excluded).
    snap = None
    with led.connect() as conn:
        row = conn.execute("SELECT positions_json FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        snap = store.loads(row[0], {}) if row else {}

    def size(r):
        p = (snap or {}).get(str(r["ticker"]).upper()) or {}
        return (float(p.get("market_value") or 0.0), float(r.get("target_weight") or 0.0))

    queued = []
    with led.connect() as conn:
        for r in sorted(rows, key=size, reverse=True):
            t = str(r["ticker"]).upper()
            comp = store.get_company(conn, t)
            if comp is None or comp["kind"] == "cash":
                continue
            exists = conn.execute("SELECT 1 FROM jobs WHERE kind='deep_dive' AND payload_json LIKE ?",
                                  (f'%"ticker": "{t}"%',)).fetchone()
            if exists:
                continue
            store.enqueue_job(conn, kind="deep_dive", payload={"ticker": t, "seed": True, "focus": None},
                              requested_by="desk-seed", now=now)
            queued.append(t)
    out["deep_dives"] = queued
    # 9. The intro message, once.
    with led.connect() as conn:
        sent = store.get_state(conn, "intro_sent")
    if not sent and not skip_intro:
        res = (send or _default_send(led))(INTRO)
        if res and res.get("ok"):
            with led.connect() as conn:
                store.set_state(conn, "intro_sent", now)
        out["intro"] = bool(res and res.get("ok"))
    else:
        out["intro"] = "already sent" if sent else "skipped"
    return out


REVIEW_FOCUS = ("A review of the position the owner holds now: keep it as planned, buy more, sell some, "
                "or sell all, and why, in plain words.")


def queue_reviews(led, *, now: str, tickers=None) -> dict:
    """Queue one deep-dive review for each holding of the newest snapshot, largest
    position first (``tick.py desk-review``). ``tickers`` limits the list. A name with a
    deep-dive queued or running, a cash fund, or a name that is not loaded is skipped,
    with the reason. Returns ``{"ok", "queued", "skipped"}``."""
    want = {str(t).strip().upper() for t in tickers or [] if str(t).strip()}
    queued, skipped = [], {}
    with led.connect() as conn:
        row = conn.execute("SELECT positions_json FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        held = store.loads(row[0], {}) if row else {}
        names = sorted(held, key=lambda t: -float((held[t] or {}).get("market_value") or 0.0))
        for t in sorted(want - {n.upper() for n in names}):
            skipped[t] = "not held in the newest snapshot"
        for t in names:
            t = t.upper()
            if want and t not in want:
                continue
            comp = store.get_company(conn, t)
            if comp is None:
                skipped[t] = "not loaded"
                continue
            if comp["kind"] == "cash":
                skipped[t] = "a cash fund"
                continue
            busy = conn.execute("SELECT id FROM jobs WHERE kind='deep_dive' AND status IN ('queued','running') "
                                "AND payload_json LIKE ?", (f'%"ticker": "{t}"%',)).fetchone()
            if busy:
                skipped[t] = f"deep-dive job {busy[0]} is already queued or running"
                continue
            store.enqueue_job(conn, kind="deep_dive", payload={"ticker": t, "focus": REVIEW_FOCUS, "review": True},
                              requested_by="desk-review", now=now)
            queued.append(t)
    return {"ok": True, "queued": queued, "skipped": skipped}


def _default_send(led):
    from lib import telegram
    from lib.desk import outbox
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").replace(",", " ").split() or [None])[0]
    box = outbox.Outbox(led, transport=outbox.telegram_transport(token), chat_id=chat)
    return lambda text: box.send(text, source="system")
