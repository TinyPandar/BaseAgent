# Phase 1 offline evaluation

Ran on 2026-11-06. No paid model call. Cost: 0 CNY.

Fresh machine reports from `python scripts/evaluate_suite.py offline` go to `applications/project_assistant/evaluation/reports/suite/`. That directory is excluded from the retrieval corpus.

## Coding

Offline scripted harness: 6/6. Usage is synthetic, not a bill.

Retained M1 qwen-plus comparison, not recomputed: success 2/3 → 5/5, mean tokens 80780 → 36185.4 (−55.2%). Five after-runs averaged about 0.030 CNY, about 0.15 CNY together. Source: `docs/reports/m1_coding_comparison-20261102T111829Z-b21f9bdd6a3745a4b572a5e3dcf309e1.md`.

## Retrieval

Retained test hybrid MRR: 0.845 (vector 0.678, BM25 0.759, hybrid+rerank 0.857). Rerank stays off: R@5 drops from 0.782 to 0.770 and each query costs about 14k tokens. Source: `applications/project_assistant/evaluation/reports/bailian-80q-summary-2026-10-21.md`. This phase does not replace that number.

## Generated answers

Synthetic pipeline only. Tune and test are scored separately. On the fake answers, span hit, line recall, line precision and no-answer refusal accuracy are 1.000. `summary_token_f1` and `summary_rouge_l` are lexical overlap with `answer_summary`, not semantic correctness. Faithfulness/NLI was not run. Tokens 0, cost 0, provider calls 0. This is not a model score.

Human review sample is listed in the machine report. `reviewed` stays null.

Live 80-question generation was not run. Command, do not start without Frank:

```text
python scripts/evaluate_answers.py --live --budget-cny 2
```

## Not run

M2 smoke suggested cap 0.20 CNY. The coding CLI has no `--budget-cny` switch, so the commands were not started. See `docs/eval.md`.
