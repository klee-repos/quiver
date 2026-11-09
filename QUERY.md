# QUERY — the runbook of a query job

`J` is the job id from "This job" at the end of your instructions. `<acct>` is its
`account_number`. The job names one Robinhood tool (`tool`) and its arguments (`args`).

If the tool returns a 401 or an authentication error, run
`.venv/bin/python tick.py auth-stop --job J` and stop.

## 1. Load the tool

Call `ToolSearch` once with this query, exactly: `select:mcp__robinhood-trading__<tool>`.

## 2. Call it

Call `mcp__robinhood-trading__<tool>` once with exactly the `args` of "This job". When the tool
takes an `account_number`, add `<acct>`. Add nothing else and change nothing. If the result
has a `next` cursor and "This job" says `"pages": N` above 1, call it again with that cursor,
at most N calls in all.

## 3. Hand the result to Python

Write the raw result, unchanged, to `state/tmp/query_J.json` as `{"<tool>": <result>}` (a list
in call order for several pages). Run
`.venv/bin/python tick.py query-commit --job J --input state/tmp/query_J.json`. Then stop.

If the tool returned an error, write the error text as the result and run `query-commit` the
same way.
