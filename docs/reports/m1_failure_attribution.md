# M1 coding scenario failure attribution

Analyzed 8 runs; 6 failed.

## Deterministic rules

A run fails when `external_acceptance_passed` is false. Verification is `verify_command`, or `run_command` with the fixture argv `python -I -B check_addition.py`. Repeated reads require a second `read_file` for a path before a successful edit to that path. Context blowup is a tool reply over 8,000 characters or prompt-token growth greater than cumulative preceding tool-output characters / 4. Prompt attribution estimates output tokens as chars/4.

Primary priority is `tool_output_context_blowup` > `repeated_file_reads` > `verify_never_called` > `step_limit_exhausted` > `other`. Repeated reads require an observed duplicate path; verify omission requires tool activity; blowup requires an observed output or prompt-growth signal. These three are primary only when their run-level evidence makes consumed steps plausible. Step exhaustion is a fallback cause because it describes the limit, not why the run reached it.

## Primary category shares

| Category | Failed runs | % failed | % all |
|---|---:|---:|---:|
| tool_output_context_blowup | 0 | 0.0% | 0.0% |
| repeated_file_reads | 1 | 16.7% | 12.5% |
| verify_never_called | 0 | 0.0% | 0.0% |
| step_limit_exhausted | 5 | 83.3% | 62.5% |
| other | 0 | 0.0% | 0.0% |

## Step budget breakdown

| Failed run | Productive tool steps | Denied tool steps | Stalled final answers | Initial rejected / other no-tool steps | Total recorded steps |
|---|---:|---:|---:|---:|---:|
| coding_scenario / 1 | 8 | 4 | 7 | 1 | 20 |
| coding_scenario / 2 | 8 | 4 | 7 | 1 | 20 |
| coding_scenario / 3 | 8 | 4 | 7 | 1 | 20 |
| coding_scenario / 4 | 8 | 3 | 8 | 1 | 20 |
| coding_scenario / 5 | 11 | 3 | 4 | 2 | 20 |
| coding_scenario_v2 / 2 | 10 | 3 | 6 | 1 | 20 |
| **Aggregate** | **53 (44.2%)** | **21 (17.5%)** | **39 (32.5%)** | **7 (5.8%)** | **120** |

Shares use total recorded steps in failed runs. A productive step contains tool calls without a permission denial; a denied step contains at least one denied call; a stalled step is a no-tool assistant turn after a completion rejection. The residual counts no-tool steps preceding the first rejection, including the answer first rejected by the harness.

## Per-step tokens and tool output

| Step | Mean prompt tokens | Mean completion tokens | Mean new tool-output chars returned |
|---:|---:|---:|---:|
| 1 | 2040.0 | 54.0 | 0.0 |
| 2 | 3220.0 | 39.4 | 2782.0 |
| 3 | 3470.4 | 180.2 | 380.0 |
| 4 | 3982.6 | 207.8 | 586.2 |
| 5 | 4287.1 | 186.8 | 188.1 |
| 6 | 4537.2 | 110.9 | 147.0 |
| 7 | 4863.5 | 116.5 | 452.5 |
| 8 | 5136.0 | 86.8 | 404.1 |
| 9 | 5420.7 | 118.1 | 629.3 |
| 10 | 5674.7 | 150.4 | 324.0 |
| 11 | 6022.1 | 326.7 | 401.6 |
| 12 | 6511.1 | 132.1 | 312.0 |
| 13 | 6817.7 | 206.7 | 486.9 |
| 14 | 7080.6 | 222.4 | 55.7 |
| 15 | 7360.1 | 182.7 | 34.3 |
| 16 | 7597.7 | 182.1 | 88.1 |
| 17 | 7910.2 | 210.8 | 199.5 |
| 18 | 8192.2 | 194.8 | 83.0 |
| 19 | 8424.7 | 201.8 | 0.0 |
| 20 | 8664.2 | 215.3 | 0.0 |

At failed runs' final steps, estimated prompt tokens attributable to tool outputs: **21.4%**; estimated as tool-output chars / 4, with the remaining prompt tokens attributed to other messages.

## Findings

- Stalled final answers account for 39 of 120 failed-run steps (32.5%). A wrap-up hint at <=3 remaining steps targets this pattern.
- 21 permission-denied calls occurred across 6 of 6 failed runs. Identical-call detection found 4 repeats in failed runs; unchanged-file read dedupe targets the 1 failed run with repeated reads.
- Tool outputs account for an estimated 21.4% of final-step failed-run prompt tokens; spilling large outputs targets this context growth.
- v1 cassettes have no explicit `scenario_policy_version` header (the recorder defaults these unversioned cassettes to policy v1); v2 headers explicitly record policy v2. The v1 runs therefore capture the older stricter policy.

## Runs

| Scenario | Run | Policy | Status | Pass | Primary | Signals | Denied calls (tools) | Rejections | Stalled | Repeated calls | Verify step / ok | Edit ok | Steps |
|---|---:|---|---|:---:|---|---|---|---:|---:|---:|---|:---:|---:|
| coding_scenario | 1 | 1 | max_steps_exceeded | no | step_limit_exhausted | step_limit_exhausted | 4 (edit_file, git_diff, run_command, write_file) | 7 | 7 | 0 | 7 / no | no | 20 |
| coding_scenario | 2 | 1 | max_steps_exceeded | no | step_limit_exhausted | step_limit_exhausted | 4 (edit_file, git_diff, run_command, write_file) | 7 | 7 | 0 | 7 / no | no | 20 |
| coding_scenario | 3 | 1 | max_steps_exceeded | no | step_limit_exhausted | step_limit_exhausted | 4 (edit_file, git_diff, run_command, write_file) | 7 | 7 | 0 | 7 / no | no | 20 |
| coding_scenario | 4 | 1 | max_steps_exceeded | no | step_limit_exhausted | step_limit_exhausted | 3 (edit_file, run_command, write_file) | 8 | 8 | 0 | 8 / no | no | 20 |
| coding_scenario | 5 | 1 | max_steps_exceeded | no | repeated_file_reads | repeated_file_reads, step_limit_exhausted | 3 (edit_file, git_diff, write_file) | 4 | 4 | 4 | 7 / no | no | 20 |
| coding_scenario_v2 | 1 | 2 | completed | yes | — | — | 0 (—) | 0 | 0 | 0 | 6 / yes | yes | 8 |
| coding_scenario_v2 | 2 | 2 | max_steps_exceeded | no | step_limit_exhausted | step_limit_exhausted | 3 (edit_file, git_diff, write_file) | 6 | 6 | 0 | 7 / no | no | 20 |
| coding_scenario_v2 | 3 | 2 | completed | yes | — | repeated_file_reads | 2 (edit_file, write_file) | 0 | 0 | 4 | 9 / yes | yes | 16 |
