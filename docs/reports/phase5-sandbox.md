# Docker sandbox and Terminal-Bench offline check

Ran on 2026-11-06. No paid model. Cost: 0 CNY.

`python -B scripts/demo_docker.py` used the local `nginx:alpine` image. One container, network `none`. `echo ok` succeeded. Reading a host file outside the mount failed. Outbound HTTP failed. The container was removed afterward.

Correction (2026-11-15): the `--fixture` run recorded here as "1/1 passed" was a report smoke over one synthetic record. Its executor returned `passed=True` without running a container or a grader, so it was never a Terminal-Bench or Harbor score. That path has been removed; the synthetic report check is now `--synthetic`, and the fixture is a Harbor task graded by `harbor run` (see [docs/terminal_bench.md](../terminal_bench.md)). Infrastructure failures are counted separately and are not model quality. The fixed subset has 18 Terminal-Bench 2.0 ids from Harbor registry `7367e809`. The real subset command was printed with `--budget-cny 15` and was not executed.

| Check | Result |
| --- | --- |
| Echo in container | pass |
| Host path outside mount | blocked |
| Outbound network | blocked |
| Container removed | yes |
| Fixture score | none: the old 1/1 was a synthetic record (`passed=True` written by the script), not a graded result |
| Real subset | not run |
