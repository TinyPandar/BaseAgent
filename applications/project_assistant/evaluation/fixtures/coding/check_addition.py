"""Standalone acceptance test; no dependencies or network access."""

import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent
assert Path.cwd().resolve() == root
assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
sys.path.insert(0, str(root))
import sample

assert sample.add(2, 3) == 5
assert sample.add(-2, 3) == 1
assert sample.untouched == "sentinel"
print("addition acceptance passed")
