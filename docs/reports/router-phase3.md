# Router / Worker offline check

Ran `python -B scripts/demo_router.py` on 2026-11-06. No paid model. Cost: 0 CNY.

`knowledge_search 怎么防越权` routed to `qa`. `Fix sample.py and run the tests` routed to `coding`. The coding edit stopped at `awaiting_approval`. Task nodes were `qa` and `coding`, with separate ledgers. The router context did not contain the worker tool body.

| Comparison | Tokens |
| --- | ---: |
| Baseline, both tool bodies inlined | 1009 |
| Router handoff, summary plus citation | 34 |
| Router / baseline | 0.034 |

The router uses about 3.4% of the tokens that would be spent if both tool outputs were copied back. qa has no write tools. coding writes still use the existing approval ledger.
