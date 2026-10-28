# SNAPSHOT — the runbook of a snapshot job

`J` is the job id and `<acct>` the `account_number` from "This job" at the end of your
instructions. A snapshot job places nothing: the hook denies every order tool in it.

If a Robinhood tool returns a 401 or an authentication error, run
`.venv/bin/python tick.py auth-stop --job J` and stop.

1. Call `ToolSearch` once with this query, exactly:
   `select:mcp__robinhood-trading__get_portfolio,mcp__robinhood-trading__get_equity_positions,mcp__robinhood-trading__get_equity_quotes,mcp__robinhood-trading__get_trade_approval_setting,mcp__robinhood-trading__get_equity_orders`
2. Call `get_portfolio` (account `<acct>`), `get_equity_positions` (account `<acct>`; page with
   `cursor` until `next` is empty), `get_equity_quotes` for the `tickers` of "This job" (in calls of
   at most 20 symbols), and `get_trade_approval_setting` (account `<acct>`).
3. If "This job" lists `reconcile` legs, call `get_equity_orders` (account `<acct>`,
   `agent_initiated` true, `created_at_gte` = `fills_created_at_gte`).
4. Write all of them to `state/tmp/snapshot_J.json`, each value the raw result unchanged:
   `{"get_portfolio": ..., "get_equity_positions": ..., "get_equity_quotes": ..., "get_trade_approval_setting": ..., "get_equity_orders": ...}`.
   Run `.venv/bin/python tick.py snapshot-commit --input state/tmp/snapshot_J.json`. Then stop.
