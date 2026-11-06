# Docker sandbox and Terminal-Bench offline check

Ran on 2026-11-06. No paid model. Cost: 0 CNY.

`python -B scripts/demo_docker.py` used the local `nginx:alpine` image. One container, network `none`. `echo ok` succeeded. Reading a host file outside the mount failed. Outbound HTTP failed. The container was removed afterward.

`python scripts/evaluate_terminal_bench.py --fixture` scored the local fake task: 1/1 passed, 0 tokens, 0 CNY. Infrastructure failures are counted separately and are not model quality. The fixed subset has 18 Terminal-Bench 2.0 ids from Harbor registry `7367e809`. The real subset command was printed with `--budget-cny 15` and was not executed.

| Check | Result |
| --- | --- |
| Echo in container | pass |
| Host path outside mount | blocked |
| Outbound network | blocked |
| Container removed | yes |
| Fixture score | 1/1 |
| Real subset | not run |
