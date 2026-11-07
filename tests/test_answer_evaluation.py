"""Offline answer scoring. No network and no provider client."""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from applications.project_assistant.evaluation.answers import (
    DEFAULT_NLI_BASE,
    DEFAULT_NLI_LARGE,
    LocalDebertaNLI,
    _load_local_nli,
    consume_budget,
    evaluate_answers,
    human_review_sample,
    is_refusal,
    make_source_reader,
    nli_variant,
    planned_live,
    render_markdown,
    resolve_nli_model,
    score_record,
    synthetic_answers,
    tokenize,
)
from applications.project_assistant.evaluation.answers_cli import main as answers_main
from applications.project_assistant.evaluation.retrieval import QUESTION_SET


def question(identifier, split, category, evidence, summary="summary"):
    return {"id": identifier, "split": split, "category": category, "question": identifier,
            "answer_summary": summary, "expected_evidence": evidence}


class AnswerScoringTests(unittest.TestCase):
    def test_citation_overlap_and_plain_no_are_not_refusals(self):
        gold = question("q", "tune", "exact_identifier", ["src/a.py:1-3"])
        covered = score_record(gold, {"answer": "No. See src/a.py:2-2", "citations": []})
        missed = score_record(gold, {"answer": "No. See other.md:1-1", "citations": ["other.md:1-1"]})
        self.assertTrue(covered["citation_span_hit"])
        self.assertAlmostEqual(covered["citation_line_recall"], 1 / 3)
        self.assertEqual(covered["citation_line_precision"], 1)
        self.assertAlmostEqual(covered["citation_coverage"], 1 / 3)
        self.assertTrue(covered["citation_supported"])
        self.assertFalse(covered["refused"])
        self.assertTrue(covered["citation_and_refusal_pass"])
        self.assertNotIn("rule_correct", covered)
        self.assertNotIn("accuracy", covered)
        self.assertFalse(covered["summary_match"])
        self.assertFalse(missed["citation_supported"])
        self.assertFalse(missed["citation_span_hit"])
        self.assertFalse(is_refusal("No. Built-ins remain coding."))

    def test_no_answer_rules(self):
        item = question("n", "test", "no_answer", [])
        refused = score_record(item, {"answer": "The fact is not established by this checkout."})
        invented = score_record(item, {"answer": "The fact is not established.", "citations": ["src/a.py:1-1"]})
        answered = score_record(item, {"answer": "It cost 12 dollars."})
        self.assertTrue(refused["citation_supported"])
        self.assertTrue(refused["citation_and_refusal_pass"])
        self.assertIsNone(refused["summary_token_f1"])
        self.assertIsNone(refused["summary_match"])
        self.assertFalse(invented["citation_supported"])
        self.assertFalse(invented["citation_and_refusal_pass"])
        self.assertFalse(answered["citation_supported"])
        self.assertFalse(answered["citation_and_refusal_pass"])

    def test_splits_stay_separate_and_test_cannot_tune(self):
        dataset = {"questions": [
            question("a", "tune", "concept", ["src/a.py:1-1"]),
            question("b", "test", "no_answer", []),
        ]}
        records = [
            {"id": "a", "answer": "src/a.py:1-1", "citations": ["src/a.py:1-1"], "source": "fixture"},
            {"id": "b", "answer": "not established", "source": "fixture"},
        ]
        report = evaluate_answers(dataset, records)
        self.assertEqual(report["splits"]["tune"]["count"], 1)
        self.assertEqual(report["splits"]["test"]["count"], 1)
        self.assertIsNone(report["splits"]["tune"]["refusal_rate"])
        self.assertEqual(report["splits"]["test"]["citation_support_rate"], None)
        self.assertIsNone(report["judge_cross_table"])
        with self.assertRaises(ValueError):
            evaluate_answers(dataset, records, judge="tune_on_test")

    def test_judge_cross_table_and_human_sample(self):
        dataset = {"questions": [question(f"q{i:02d}", "tune" if i < 5 else "test", "concept", ["src/a.py:1-1"])
                                 for i in range(10)]}
        records = [{"id": item["id"], "answer": "src/a.py:1-1", "citations": ["src/a.py:1-1"]}
                   for item in dataset["questions"]]
        judge = {item["id"]: {"correct": item["id"] != "q00"} for item in dataset["questions"]}
        report = evaluate_answers(dataset, records, judge=judge)
        self.assertNotIn("accuracy", report["splits"]["tune"])
        self.assertNotIn("accuracy_source", report["splits"]["tune"])
        self.assertEqual(report["judge_cross_table"]["judge_incorrect_citation_pass"], 1)
        self.assertEqual(report["judge_cross_table"]["judge_correct_citation_pass"], 9)
        self.assertNotIn("rule_correct", report["questions"][0])
        sample = human_review_sample(dataset["questions"])
        self.assertEqual(len(sample), 2)
        self.assertTrue(all(item["reviewed"] is None for item in sample))

    def test_real_question_set_synthetic_pipeline_is_free(self):
        dataset = json.loads(QUESTION_SET.read_text(encoding="utf-8"))
        report = evaluate_answers(dataset, synthetic_answers(dataset))
        self.assertEqual(report["splits"]["tune"]["span_hit_rate"], 1)
        self.assertEqual(report["splits"]["test"]["span_hit_rate"], 1)
        self.assertEqual(report["splits"]["tune"]["line_recall"], 1)
        self.assertEqual(report["splits"]["test"]["line_recall"], 1)
        self.assertEqual(report["splits"]["tune"]["line_precision"], 1)
        self.assertEqual(report["splits"]["test"]["line_precision"], 1)
        self.assertEqual(report["splits"]["tune"]["refusal_accuracy"], 1)
        self.assertEqual(report["splits"]["test"]["refusal_accuracy"], 1)
        self.assertGreater(report["splits"]["tune"]["summary_token_f1"], 0.9)
        self.assertGreater(report["splits"]["test"]["summary_rouge_l"], 0.9)
        self.assertNotIn("accuracy", report["splits"]["tune"])
        self.assertNotIn("rule_accuracy", report["splits"]["tune"])
        self.assertEqual(report["nli"]["status"], "skipped")
        self.assertNotIn("faithfulness", report["questions"][0])
        self.assertNotIn("faithfulness_base", report["questions"][0])
        self.assertNotIn("faithfulness_large", report["questions"][0])
        self.assertNotIn("nli_variant", report["questions"][0])
        self.assertEqual(report["splits"]["tune"]["citation_support_rate"], 1)
        self.assertEqual(report["splits"]["test"]["refusal_rate"], 1)
        text = render_markdown(report)
        self.assertNotIn("正确率", text)
        self.assertNotIn("| Accuracy |", text)
        self.assertNotIn("rule_correct", text)
        self.assertIn("Synthetic rows only prove scoring", text)
        self.assertIn("no citations", text.lower())
        self.assertEqual(report["cost_cny"], 0)
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["live_executed"])
        self.assertEqual(len(report["human_review"]), 16)
        tune_ids = {row["id"] for row in report["questions"] if row["split"] == "tune"}
        test_ids = {row["id"] for row in report["questions"] if row["split"] == "test"}
        self.assertFalse(tune_ids & test_ids)
        self.assertEqual(len(tune_ids) + len(test_ids), 80)

    def test_partial_line_overlap_is_not_full_coverage(self):
        gold = question("q", "test", "concept", ["path:10-20"], summary="the gold summary stays intact")
        row = score_record(gold, {"answer": "unrelated", "citations": ["path:15-15"]})
        self.assertTrue(row["citation_span_hit"])
        self.assertAlmostEqual(row["citation_line_recall"], 1 / 11)
        self.assertAlmostEqual(row["citation_coverage"], 1 / 11)
        self.assertNotEqual(row["citation_coverage"], 1.0)
        self.assertEqual(row["citation_line_precision"], 1.0)
        self.assertLess(row["citation_line_recall"], 1)
        self.assertFalse(row["citation_supported"])
        touched = score_record(gold, {"answer": "unrelated", "citations": ["path:10-12"]}, line_recall_min=0.3)
        self.assertAlmostEqual(touched["citation_line_recall"], 3 / 11)
        self.assertFalse(touched["citation_supported"])
        boundary = score_record(
            question("b", "test", "concept", ["path:1-10"]),
            {"answer": "x", "citations": ["path:1-3"]})
        self.assertAlmostEqual(boundary["citation_line_recall"], 0.3)
        self.assertTrue(boundary["citation_span_hit"])
        self.assertTrue(boundary["citation_supported"])

    def test_wrong_answer_with_good_citations_is_not_answer_correct(self):
        summary = "Visible markdown files use the document chunker."
        gold = question("q", "tune", "concept", ["src/a.py:1-10", "src/b.py:2-5"], summary=summary)
        citations = ["src/a.py:1-10", "src/b.py:2-5"]
        row = score_record(gold, {"answer": "2+2=5 " + " ".join(citations), "citations": citations})
        self.assertTrue(row["citation_span_hit"])
        self.assertTrue(row["citation_supported"])
        self.assertEqual(row["citation_line_recall"], 1)
        self.assertEqual(row["citation_line_precision"], 1)
        self.assertLess(row["summary_token_f1"], 0.05)
        self.assertLess(row["summary_rouge_l"], 0.05)
        self.assertFalse(row["summary_match"])
        self.assertNotIn("rule_correct", row)
        self.assertNotIn("accuracy", row)
        report = evaluate_answers({"questions": [gold]}, [{"id": "q", "answer": "2+2=5", "citations": citations}])
        text = render_markdown(report)
        self.assertNotIn("正确率", text)
        self.assertNotIn("| Accuracy |", text)
        self.assertNotIn("rule_correct", text)
        self.assertFalse(report["questions"][0]["summary_match"])

    def test_full_summary_restatement_scores_high_lexical_overlap(self):
        summary = "Sort chunks by path and hash the doc_id values."
        gold = question("q", "test", "concept", ["src/a.py:10-20", "src/b.py:1-4"], summary=summary)
        citations = ["src/a.py:10-20", "src/b.py:1-4"]
        row = score_record(gold, {"answer": summary + " " + " ".join(citations), "citations": citations})
        self.assertTrue(row["citation_span_hit"])
        self.assertTrue(row["citation_supported"])
        self.assertEqual(row["citation_line_recall"], 1)
        self.assertGreater(row["summary_token_f1"], 0.9)
        self.assertGreater(row["summary_rouge_l"], 0.9)
        self.assertTrue(row["summary_match"])
        self.assertEqual(tokenize("chunk_python_code stays whole"), ["chunk_python_code", "stays", "whole"])
        self.assertEqual(tokenize("RAGConfig.from_env"), ["ragconfig.from_env"])

    def test_one_of_several_spans_is_not_a_span_hit(self):
        gold = question("q", "tune", "cross_file", ["src/a.py:1-3", "src/b.py:4-6"])
        row = score_record(gold, {"answer": "only one file", "citations": ["src/a.py:1-3"]})
        self.assertFalse(row["citation_span_hit"])
        self.assertFalse(row["citation_supported"])
        self.assertAlmostEqual(row["citation_line_recall"], 0.5)
        self.assertEqual(row["citation_line_precision"], 1)

    def test_nli_off_still_scores_other_layers_and_variants_stay_separate(self):
        summary = "The gold fact is the documented behavior."
        gold = question("q", "tune", "concept", ["src/a.py:1-4"], summary=summary)
        record = {"id": "q", "answer": "The gold fact is the documented behavior. 2+2=5.",
                  "citations": ["src/a.py:1-4"]}
        dataset = {"questions": [gold]}
        skipped = evaluate_answers(dataset, [record], nli_skip_reason="nli model unavailable: OSError")
        self.assertEqual(skipped["nli"]["status"], "skipped")
        self.assertIn("unavailable", skipped["nli"]["reason"])
        self.assertIsNone(skipped["nli"]["base"])
        self.assertIsNone(skipped["nli"]["large"])
        self.assertNotIn("faithfulness", skipped["splits"]["tune"])
        self.assertNotIn("faithfulness", skipped["questions"][0])
        self.assertNotIn("faithfulness_base", skipped["questions"][0])
        self.assertNotIn("faithfulness_large", skipped["questions"][0])
        self.assertNotIn("nli_variant", skipped["questions"][0])
        self.assertEqual(skipped["splits"]["tune"]["span_hit_rate"], 1)
        self.assertGreater(skipped["splits"]["tune"]["summary_token_f1"], 0.5)
        self.assertIn("skipped", render_markdown(skipped))
        self.assertIsNone(LocalDebertaNLI("cross-encoder/nli-deberta-v3-base")._pipe)
        self.assertEqual(nli_variant("cross-encoder/nli-deberta-v3-large"), "large")
        self.assertEqual(nli_variant(LocalDebertaNLI("cross-encoder/nli-deberta-v3-base").model_name), "base")

        class Backend:
            def __init__(self, model_name, variant, label):
                self.model_name = model_name
                self.variant = variant
                self._label = label

            def label(self, premise, hypothesis):
                self.last = (premise, hypothesis)
                return self._label if "gold fact" in premise and "gold fact" in hypothesis else "contradiction"

        def read_source(path, start, end):
            self.assertEqual((path, start, end), ("src/a.py", 1, 4))
            return "the gold fact is written on these lines"

        base = Backend("cross-encoder/nli-deberta-v3-base", "base", "entailment")
        base_report = evaluate_answers(dataset, [record], nli=base, read_source=read_source)
        self.assertEqual(base_report["nli"]["status"], "ran")
        self.assertIsNone(base_report["nli"]["large"])
        self.assertAlmostEqual(base_report["nli"]["base"]["faithfulness"], 0.5)
        self.assertAlmostEqual(base_report["questions"][0]["faithfulness"], 0.5)
        self.assertAlmostEqual(base_report["questions"][0]["faithfulness_base"], 0.5)
        self.assertIsNone(base_report["questions"][0]["faithfulness_large"])
        self.assertIn("gold fact", base.last[0])
        self.assertIn("Faithfulness (base)", render_markdown(base_report))
        large = Backend("cross-encoder/nli-deberta-v3-large", "large", "contradiction")
        large_report = evaluate_answers(dataset, [record], nli=large, read_source=read_source)
        self.assertIsNone(large_report["nli"]["base"])
        self.assertEqual(large_report["nli"]["large"]["faithfulness"], 0)
        self.assertIsNone(large_report["questions"][0]["faithfulness_base"])
        self.assertEqual(large_report["questions"][0]["faithfulness_large"], 0)
        self.assertIn("Faithfulness (large)", render_markdown(large_report))
        self.assertNotIn("Faithfulness (base)", render_markdown(large_report))

    def test_nli_model_aliases_map_to_documented_ids(self):
        self.assertEqual(resolve_nli_model("base"), DEFAULT_NLI_BASE)
        self.assertEqual(resolve_nli_model("LARGE"), DEFAULT_NLI_LARGE)
        self.assertEqual(resolve_nli_model(" custom/id "), "custom/id")
        large = LocalDebertaNLI("large")
        self.assertEqual(large.model_name, DEFAULT_NLI_LARGE)
        self.assertEqual(large.variant, "large")
        self.assertIsNone(large._pipe)
        self.assertEqual(LocalDebertaNLI("base").model_name, DEFAULT_NLI_BASE)
        self.assertEqual(LocalDebertaNLI("base").variant, "base")

    def test_nli_load_blocks_network_on_transformers_457(self):
        """transformers 4.57.1 ignores pipeline(local_files_only=True). The loader must not download."""
        import socket
        import sys
        from types import ModuleType
        from unittest.mock import patch

        hub_calls = []
        downloads = []
        pipeline_models = []
        sockets = []
        tf_hub = ModuleType("transformers.utils.hub")
        tf_hub._is_offline_mode = False
        hub_constants = ModuleType("huggingface_hub.constants")
        hub_constants.HF_HUB_OFFLINE = False

        def cached_file(repo, filename, local_files_only=False, **kwargs):
            forced = bool(local_files_only or tf_hub._is_offline_mode or hub_constants.HF_HUB_OFFLINE)
            hub_calls.append({
                "filename": filename,
                "local_files_only": bool(local_files_only),
                "tf_offline": bool(tf_hub._is_offline_mode),
                "hub_offline": bool(hub_constants.HF_HUB_OFFLINE),
            })
            if not forced:
                downloads.append((repo, filename))
                raise FileNotFoundError("would have downloaded " + filename)
            return filename

        class _Piece:
            def __init__(self, filename):
                self.filename = filename

            def from_pretrained(self, name, local_files_only=False, **kwargs):
                cached_file(name, self.filename, local_files_only=local_files_only, **kwargs)
                return object()

        def pipeline(task, model=None, tokenizer=None, config=None, **kwargs):
            pipeline_models.append(isinstance(model, str))
            if isinstance(model, str):
                # The 4.57.1 pipeline resolves config.json without local_files_only.
                cached_file(model, "config.json", local_files_only=False)
            return object()

        transformers = ModuleType("transformers")
        transformers.AutoConfig = _Piece("config.json")
        transformers.AutoTokenizer = _Piece("tokenizer.json")
        transformers.AutoModelForSequenceClassification = _Piece("model.safetensors")
        transformers.pipeline = pipeline
        utils = ModuleType("transformers.utils")
        utils.hub = tf_hub
        saved = {}
        for name, module in {
            "transformers": transformers,
            "transformers.utils": utils,
            "transformers.utils.hub": tf_hub,
            "huggingface_hub.constants": hub_constants,
        }.items():
            saved[name] = sys.modules.get(name)
            sys.modules[name] = module

        def blocked(*args, **kwargs):
            sockets.append(args)
            raise OSError("network denied")

        try:
            with self.assertRaises(FileNotFoundError):
                pipeline("text-classification", model=DEFAULT_NLI_BASE, local_files_only=True)
            self.assertEqual(downloads, [(DEFAULT_NLI_BASE, "config.json")])
            downloads.clear()
            hub_calls.clear()
            pipeline_models.clear()
            with patch.object(socket.socket, "connect", blocked), patch("socket.create_connection", blocked):
                loaded = _load_local_nli(DEFAULT_NLI_BASE)
            self.assertIsNotNone(loaded)
            self.assertEqual(downloads, [])
            self.assertEqual(sockets, [])
            self.assertEqual([call["filename"] for call in hub_calls],
                             ["config.json", "tokenizer.json", "model.safetensors"])
            self.assertTrue(all(call["local_files_only"] and call["tf_offline"] and call["hub_offline"]
                                for call in hub_calls))
            self.assertEqual(pipeline_models, [False])
            self.assertFalse(tf_hub._is_offline_mode)
            self.assertFalse(hub_constants.HF_HUB_OFFLINE)
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_source_reader_stays_inside_the_checkout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            reader = make_source_reader(root)
            self.assertEqual(reader("src/a.py", 2, 3), "two\nthree")
            with self.assertRaises(ValueError):
                reader("../a.py", 1, 1)

    def test_live_command_does_not_dispatch_and_budget_stops(self):
        plan = planned_live("1.5")
        self.assertFalse(plan["executed"])
        self.assertEqual(plan["cost_cny"], 0)
        self.assertIn("--budget-cny 1.5", plan["command"])
        calls = []

        def ask(question):
            calls.append(question["id"])
            return {"id": question["id"], "cost_cny": "0.8"}

        stopped = consume_budget([{"id": "a"}, {"id": "b"}, {"id": "c"}], ask, "1.0")
        self.assertEqual(calls, ["a", "b"])
        self.assertTrue(stopped["stopped_for_budget"])
        self.assertEqual(stopped["provider_calls"], 2)

    def test_cli_synthetic_and_live_write_no_secrets(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = answers_main(["--synthetic", "--report-dir", str(directory), "--split", "test"])
            self.assertEqual(code, 0)
            written = list(directory.glob("answers-*.json"))
            self.assertEqual(len(written), 1)
            text = written[0].read_text(encoding="utf-8")
            self.assertNotIn("tune", json.loads(text)["splits"])
            self.assertNotIn("api_key", text.lower())
            self.assertNotIn("sk-", text.lower())
            live = io.StringIO()
            with redirect_stdout(live):
                self.assertEqual(answers_main(["--live", "--budget-cny", "2"]), 0)
            self.assertIn("authorization_required", live.getvalue())
            self.assertNotIn("provider.invalid", live.getvalue())
            nli_out = io.StringIO()
            with redirect_stdout(nli_out):
                code = answers_main(["--synthetic", "--nli", "--nli-model", str(directory / "missing-nli"),
                                     "--split", "test", "--report-dir", str(directory / "nli")])
            self.assertEqual(code, 0)
            saved = json.loads(next((directory / "nli").glob("answers-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(saved["nli"]["status"], "skipped")
            self.assertIn("unavailable", saved["nli"]["reason"])
            self.assertIsNone(saved["nli"]["base"])
            self.assertIsNone(saved["nli"]["large"])
            self.assertIn("span_hit_rate", saved["splits"]["test"])
            self.assertNotIn("faithfulness", saved["splits"]["test"])
            self.assertTrue(saved["questions"])
            for row in saved["questions"]:
                self.assertNotIn("faithfulness", row)
                self.assertNotIn("faithfulness_base", row)
                self.assertNotIn("faithfulness_large", row)
                self.assertNotIn("nli_variant", row)
            self.assertIn("no citations", render_markdown(saved).lower())


if __name__ == "__main__":
    unittest.main()
