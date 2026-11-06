# Trigger offline check

Ran `python -B scripts/demo_triggers.py` on 2026-11-06. No watcher, no network, no paid model. Cost: 0 CNY.

A fixture failure log woke `qa` once. The follow-up poll inside the debounce window did not wake again. `sample.py` was unchanged. The ledger recorded `wrote: false` and 0 tokens.

| Check | Result |
| --- | --- |
| Worker | qa |
| Summary | Read-only qa summary: reports/test.log reports failure |
| Business file changed | no |
| Silent write | no |
| Tokens | 0 |
