"""Check or rebind the evaluation question set against the current repository.

``--check`` (default) recomputes ``corpus_version`` with the dataset's
``corpus_version_method`` (``collect_corpus(repository_root)`` with default
include/exclude, then ``compute_corpus_version(chunks)``) and verifies every
``evidence_sha256`` against its cited line slice. When a slice changed, the same
file is searched for a unique window of the same length with the recorded hash:
that is line drift (the text moved), reported as old -> new reference.
Exit status 1 means something differs.

``--write`` applies line drift (renames the reference in ``evidence_sha256`` and
in every ``expected_evidence``; hash values are never recomputed) and rebinds
``corpus_version``. It refuses when any evidence text really changed or moved
ambiguously; those need human review. Gold ranges and answer summaries are not
regenerated. No network or model access.

    python scripts/rebind_corpus_version.py --check
    python scripts/rebind_corpus_version.py --write
"""

from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from applications.project_assistant.corpus import collect_corpus, compute_corpus_version  # noqa: E402
from applications.project_assistant.evaluation.retrieval import QUESTION_SET, parse_reference  # noqa: E402


def _digest(lines):
    return sha256("\n".join(lines).encode()).hexdigest()


def inspect(dataset, root):
    """Return (current corpus version, drift {old: new}, problems [str])."""
    version = compute_corpus_version(collect_corpus(root))
    drift, problems = {}, []
    for reference, expected in dataset["evidence_sha256"].items():
        path, start, end = parse_reference(reference)
        target = root / path
        if not target.is_file():
            problems.append(f"{reference}: file is missing")
            continue
        lines = target.read_text(encoding="utf-8").splitlines()
        if end <= len(lines) and _digest(lines[start - 1:end]) == expected:
            continue
        size = end - start + 1
        matches = [first + 1 for first in range(len(lines) - size + 1) if _digest(lines[first:first + size]) == expected]
        if len(matches) == 1:
            drift[reference] = f"{path}:{matches[0]}-{matches[0] + size - 1}"
        elif matches:
            problems.append(f"{reference}: text moved but matches {len(matches)} places; review by hand")
        else:
            problems.append(f"{reference}: cited text changed; review the gold range and answer summary")
    return version, drift, problems


def apply(dataset, version, drift):
    for old, new in drift.items():
        if new in dataset["evidence_sha256"] and dataset["evidence_sha256"][new] != dataset["evidence_sha256"][old]:
            raise ValueError(f"{new} already records a different hash")
    dataset["evidence_sha256"] = {drift.get(key, key): value for key, value in dataset["evidence_sha256"].items()}
    for question in dataset["questions"]:
        question["expected_evidence"] = [drift.get(reference, reference) for reference in question["expected_evidence"]]
    dataset["corpus_version"] = version
    return dataset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report differences without writing (default)")
    mode.add_argument("--write", action="store_true", help="Apply line drift and rebind corpus_version")
    parser.add_argument("--questions", type=Path, default=QUESTION_SET)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    text = args.questions.read_text(encoding="utf-8")
    dataset = json.loads(text)
    version, drift, problems = inspect(dataset, args.root.resolve())
    print(f"corpus_version: recorded {dataset['corpus_version']}, current {version}"
          + (" (match)" if version == dataset["corpus_version"] else " (differs)"))
    print(f"method: {dataset['corpus_version_method']}")
    for old, new in drift.items():
        print(f"line drift: {old} -> {new} (hash unchanged)")
    for problem in problems:
        print(f"needs review: {problem}")
    if not drift and not problems:
        print(f"evidence: all {len(dataset['evidence_sha256'])} hashes match their cited lines")
    clean = version == dataset["corpus_version"] and not drift and not problems
    if not args.write:
        return 0 if clean else 1
    if problems:
        print("refusing to write: resolve the evidence above first", file=sys.stderr)
        return 1
    if clean:
        print("nothing to write")
        return 0
    updated = json.dumps(apply(dataset, version, drift), ensure_ascii=False, indent=2) + "\n"
    args.questions.write_text(updated, encoding="utf-8")
    print(f"wrote {args.questions}: corpus_version {version}, {len(drift)} reference(s) moved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
