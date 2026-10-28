# EXECUTE — the runbook of an execute job

`N` is the proposal id and `J` the job id from "This job" at the end of your instructions.
`<acct>` is its `account_number`. Every file goes in `state/tmp/`. Every file is an object keyed
by tool name, each value the raw tool result unchanged (a list in call order for several calls).

If a Robinhood tool returns a 401 or an authentication error, run
`.venv/bin/python tick.py auth-stop --job J` and stop.

If "This job" says `"recovery": true`, skip to **Recovery job** below.

## 1. Load the tools

Call `ToolSearch` once with this query, exactly:
`select:mcp__robinhood-trading__get_portfolio,mcp__robinhood-trading__get_equity_positions,mcp__robinhood-trading__get_equity_quotes,mcp__robinhood-trading__get_trade_approval_setting,mcp__robinhood-trading__get_equity_orders,mcp__robinhood-trading__review_equity_order,mcp__robinhood-trading__place_equity_order`

## 2. Sells check (run it for every proposal, also one with no sell leg)

Call `get_portfolio` (account `<acct>`), `get_equity_positions` (account `<acct>`; page with `cursor`
until `next` is empty), `get_trade_approval_setting` (account `<acct>`), and `get_equity_quotes` for
the symbols of EVERY leg in "This job", sells and buys. Write them to `state/tmp/exec_N_sells.json`:
`{"get_portfolio": ..., "get_equity_positions": ..., "get_trade_approval_setting": ..., "get_equity_quotes": ...}`.

Run `.venv/bin/python tick.py exec-check --proposal N --phase sells --input state/tmp/exec_N_sells.json`.
- `"action": "requote"` or `"action": "refused"`, or an `"error"`: stop. Python sends the new numbers.
- `"action": "none"`: no sell to place. Go to step 4.
- `"action": "place"`: go to step 3 with its `legs`.

## 3. Place the returned legs

For each returned leg, in order:
1. Call `review_equity_order` with exactly the leg's fields except `ref_id`
   (`account_number`, `symbol`, `side`, `type`, `quantity` or `dollar_amount`, `market_hours`,
   `time_in_force`).
2. Call `place_equity_order` with exactly the leg's fields, `ref_id` included. If the hook denies
   it, go on with the next leg.

Write every `place_equity_order` result, in call order, to `state/tmp/exec_N_results.json` as
`{"place_equity_order": [ ... ]}`. Run
`.venv/bin/python tick.py exec-commit --proposal N --input state/tmp/exec_N_results.json`.

After the sells, go to step 4. After the buys, go to step 6.

## 4. Wait for the sells (only when the proposal has a buy leg)

Skip this step when step 3 placed no sell. Call `get_equity_orders` (account `<acct>`,
`agent_initiated` true, `created_at_gte` = `fills_created_at_gte` from "This job"). If a placed sell
is not `filled`, run `.venv/bin/python tick.py wait --seconds 20` and call it again; at most 3
calls in all. Write the last result to `state/tmp/exec_N_sellfills.json` as
`{"get_equity_orders": ...}` and run
`.venv/bin/python tick.py fills --input state/tmp/exec_N_sellfills.json`.

## 5. Buys check (only when the proposal has a buy leg)

Call `get_portfolio`, `get_equity_positions`, `get_trade_approval_setting`, and
`get_equity_quotes` for the buy legs' symbols. Write `state/tmp/exec_N_buys.json` in the same
shape and run
`.venv/bin/python tick.py exec-check --proposal N --phase buys --input state/tmp/exec_N_buys.json`.
With `"action": "place"`, place the returned legs as in step 3. Otherwise go to step 6.

## 6. Fills

Call `get_equity_orders` (account `<acct>`, `agent_initiated` true, `created_at_gte` =
`fills_created_at_gte`). If an order of this job is not `filled`, run
`.venv/bin/python tick.py wait --seconds 20` and call it again; at most 3 calls. Write the last
result to `state/tmp/exec_N_fills.json` as `{"get_equity_orders": ...}` and run
`.venv/bin/python tick.py fills --input state/tmp/exec_N_fills.json`.

## 7. Snapshot

Call `get_portfolio`, `get_equity_positions`, `get_trade_approval_setting`, and
`get_equity_quotes` for the `book_tickers` of "This job". Write `state/tmp/exec_N_after.json` in
the same shape and run
`.venv/bin/python tick.py snapshot-commit --input state/tmp/exec_N_after.json`. Then stop.

## Recovery job

1. Load the tools as in step 1.
2. Call `get_equity_orders` (account `<acct>`, `agent_initiated` true, `created_at_gte` =
   `created_at_gte` from "This job"). Call `get_portfolio`, `get_equity_positions`,
   `get_trade_approval_setting`, and `get_equity_quotes` for the legs' symbols. Write all five to
   `state/tmp/exec_N_recover.json` and run
   `.venv/bin/python tick.py exec-check --proposal N --phase recover --input state/tmp/exec_N_recover.json`.
3. With `"action": "place"`, review and place each returned leg as in step 3 (its own `ref_id`),
   write `state/tmp/exec_N_results.json`, and run `exec-commit` as in step 3.
4. Run steps 6 and 7.
