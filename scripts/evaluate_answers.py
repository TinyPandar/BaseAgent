"""Score answers offline, preview the paid live command with --plan, or run it with --live."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from applications.project_assistant.evaluation.answers_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
