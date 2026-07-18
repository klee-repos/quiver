"""Strategy-intelligence service (analysis side of the wall).

Reads legislation/regulation at the SECTION level, derives what each section mechanically
changes, and (in later phases) chains that to industries -> tickers -> strategy.yaml
proposals. Every module here is analysis-side: it never imports the sizing/limit/broker
modules and never reads a trading limit (enforced by the decision-wall test).

This package proposes; it never trades. Remove it and strategy.yaml goes static.
"""
