from __future__ import annotations

import os
import sys
from pathlib import Path

PYTHON_DIR = Path(__file__).resolve().parents[1]
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

os.environ["ODYSSEUS_STRICT_TRACE"] = "1"
