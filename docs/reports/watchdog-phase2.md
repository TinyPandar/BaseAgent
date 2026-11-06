# Watchdog offline check

Ran `python -B scripts/demo_watchdog.py` on 2026-11-06. No paid model. Cost: 0 CNY.

The fixture repeats `read_file` on the same path. Watchdog stopped at `aborted_by_watchdog` after 3 tool calls, before the 8-step limit. The error was `tool read_file repeated 3 times with equivalent arguments`.

| Metric | Value |
| --- | ---: |
| stall decisions | 1 |
| repeat decisions | 1 |
| budget remaining steps | 3 |
| provider calls | 0 |
| cost CNY | 0 |

Unit tests cover stall, repeat, budget and tool timeout, including the cases that must not fire. A write scheduled after the stop decision does not run.
