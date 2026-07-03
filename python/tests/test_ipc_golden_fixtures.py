from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "ipc_contract.golden.json"

TAURI_HANDLER_RE = re.compile(r"tauri::generate_handler!\[(.*?)\]", re.S)
PYTHON_METHOD_TABLE_RE = re.compile(
    r"self\.methods:\s*dict\[str, Callable\[\[JsonDict\], Any\]\]\s*=\s*\{(.*?)\n\s*\}", re.S
)
PYTHON_METHOD_KEY_RE = re.compile(r'"([a-zA-Z0-9_.]+)":\s*self\.\w+')
FRONTEND_INVOKE_RE = re.compile(r'invoke(?:<[^>]*>)?\(\s*["\']([\w.]+)["\']')
FRONTEND_RPC_RE = re.compile(r'\brpc(?:<[^>]*>)?\(\s*["\']([\w.]+)["\']')


def extract_tauri_commands() -> list[str]:
    lib_rs = (REPO_ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    match = TAURI_HANDLER_RE.search(lib_rs)
    assert match, "could not find tauri::generate_handler![...] in lib.rs"
    return sorted(name.strip() for name in match.group(1).split(",") if name.strip())


def extract_python_rpc_methods() -> list[str]:
    rpc_server = (REPO_ROOT / "python" / "rpc_server.py").read_text(encoding="utf-8")
    match = PYTHON_METHOD_TABLE_RE.search(rpc_server)
    assert match, "could not find self.methods dispatch table in rpc_server.py"
    return sorted(set(PYTHON_METHOD_KEY_RE.findall(match.group(1))))


def extract_frontend_calls() -> tuple[list[str], list[str]]:
    invoke_commands: set[str] = set()
    rpc_methods: set[str] = set()
    for path in (REPO_ROOT / "src").glob("**/*.ts*"):
        text = path.read_text(encoding="utf-8")
        invoke_commands.update(FRONTEND_INVOKE_RE.findall(text))
        rpc_methods.update(FRONTEND_RPC_RE.findall(text))
    return sorted(invoke_commands), sorted(rpc_methods)


def load_golden() -> dict[str, list[str]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_tauri_command_inventory_matches_golden_fixture():
    golden = load_golden()
    actual = extract_tauri_commands()
    assert actual == golden["tauri_commands"], (
        "Rust #[tauri::command] invoke_handler list changed. "
        f"Update {GOLDEN_PATH.name} deliberately if this is intended.\n"
        f"actual={actual}\nexpected={golden['tauri_commands']}"
    )


def test_python_rpc_method_inventory_matches_golden_fixture():
    golden = load_golden()
    actual = extract_python_rpc_methods()
    assert actual == golden["python_rpc_methods"], (
        "Python rpc_server.py dispatch table changed. "
        f"Update {GOLDEN_PATH.name} deliberately if this is intended.\n"
        f"actual={actual}\nexpected={golden['python_rpc_methods']}"
    )


def test_frontend_call_inventory_matches_golden_fixture():
    golden = load_golden()
    actual_invoke, actual_rpc = extract_frontend_calls()
    assert actual_invoke == golden["frontend_invoke_commands"], (
        "Frontend direct invoke<...>(...) call sites changed. "
        f"Update {GOLDEN_PATH.name} deliberately if this is intended.\n"
        f"actual={actual_invoke}\nexpected={golden['frontend_invoke_commands']}"
    )
    assert actual_rpc == golden["frontend_rpc_methods"], (
        "Frontend rpc(...) method-name call sites changed. "
        f"Update {GOLDEN_PATH.name} deliberately if this is intended.\n"
        f"actual={actual_rpc}\nexpected={golden['frontend_rpc_methods']}"
    )


def test_frontend_rpc_calls_resolve_to_known_python_methods():
    _, frontend_rpc_methods = extract_frontend_calls()
    python_rpc_methods = extract_python_rpc_methods()
    unknown = sorted(set(frontend_rpc_methods) - set(python_rpc_methods))
    assert not unknown, (
        "Frontend calls rpc() with method name(s) not present in the Python "
        f"dispatch table (possible drift or typo): {unknown}"
    )


def test_frontend_invoke_calls_resolve_to_known_tauri_commands():
    frontend_invoke_commands, _ = extract_frontend_calls()
    tauri_commands = extract_tauri_commands()
    unknown = sorted(set(frontend_invoke_commands) - set(tauri_commands))
    assert not unknown, (
        "Frontend calls invoke() with a command name not registered in "
        f"tauri::generate_handler![...] (possible drift or typo): {unknown}"
    )
