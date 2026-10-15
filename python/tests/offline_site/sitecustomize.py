"""Installs the offline test boundary in Python child processes started by tests."""
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("offline_network", Path(__file__).resolve().parents[1] / "offline_network.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules["offline_network"] = _module
_spec.loader.exec_module(_module)
_module.install()
