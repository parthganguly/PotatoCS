"""Dev CLI for single-arm, paired-arm and comparison benchmarks.

Example (from the `python/` directory):

    python -m odysseus_desktop_backend.runtime_bench \
        --model llama3.2:1b --shape tiny --batch-id ollama-1b-tiny-baseline \
        --artifact-dir ../projects/odysseus/benchmarks/local-runtime

Every artifact this writes is schema-validated and redaction-checked.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from odysseus_desktop_backend.runtime_bench.comparison import load_and_compare
from odysseus_desktop_backend.runtime_bench.harness import run_ollama_batch
from odysseus_desktop_backend.runtime_bench.paired import run_paired_ollama_batch


def _legacy_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runtime_bench")
    parser.add_argument("--model", required=True)
    parser.add_argument("--shape", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--no-cold", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--keep-alive", default=None)
    parser.add_argument("--think", choices=["on", "off"], default=None)
    parser.add_argument(
        "--options-json",
        default="{}",
        help='per-request options, e.g. {"num_ctx": 8192, "num_thread": 6}',
    )
    parser.add_argument(
        "--server-env-json",
        default="{}",
        help="allow-listed env of the controlled server this run talked to (record only)",
    )
    args = parser.parse_args(argv)

    options = json.loads(args.options_json)
    server_env = json.loads(args.server_env_json)
    think = None if args.think is None else args.think == "on"
    keep_alive: str | int | None = args.keep_alive
    if isinstance(keep_alive, str) and keep_alive.isdigit():
        keep_alive = int(keep_alive)

    artifact = run_ollama_batch(
        model=args.model,
        shape=args.shape,
        batch_id=args.batch_id,
        artifact_dir=args.artifact_dir,
        options=options,
        keep_alive=keep_alive,
        think=think,
        server_env=server_env,
        repeats=args.repeats,
        include_cold=not args.no_cold,
        timeout=args.timeout,
    )
    summary = {
        "batch_id": artifact["batch_id"],
        "runs": [
            {
                "cold": run["cold"],
                "total_ms": run["timings_ms"]["total"],
                "first_token_ms": run["timings_ms"]["first_token"],
                "generation_tps": run["tokens"]["generation_tps"],
                "quality": run["quality_check"],
                "error": run["error_category"],
            }
            for run in artifact["runs"]
        ],
    }
    print(json.dumps(summary, indent=1))
    return 0


def _compare_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runtime_bench compare")
    parser.add_argument("artifacts", nargs="+", help="two or more schema-v2 arm artifacts")
    parser.add_argument("--policy", help="optional closed schema-v1 interference policy JSON")
    args = parser.parse_args(argv)
    if len(args.artifacts) < 2:
        parser.error("compare requires at least two artifacts")
    policy = None
    if args.policy:
        try:
            policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parser.error(f"cannot load interference policy: {exc}")
    try:
        report = load_and_compare(args.artifacts, policy=policy)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


def _paired_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runtime_bench paired")
    parser.add_argument("--model", required=True)
    parser.add_argument("--shape", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--pair-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--context-limit", type=int, default=4096)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300.0, help="hard per-arm wall-clock deadline in seconds")
    parser.add_argument("--cancel-probe", action="store_true")
    parser.add_argument("--cancel-after-ms", type=int, default=500)
    parser.add_argument("--baseline-options-json", default="{}")
    parser.add_argument("--candidate-options-json", default="{}")
    args = parser.parse_args(argv)
    baseline_options = json.loads(args.baseline_options_json)
    candidate_options = json.loads(args.candidate_options_json)
    if not isinstance(baseline_options, dict) or not isinstance(candidate_options, dict):
        parser.error("arm options must be JSON objects")
    result = run_paired_ollama_batch(
        model=args.model,
        shape=args.shape,
        experiment_id=args.experiment_id,
        pair_id=args.pair_id,
        batch_id=args.batch_id,
        artifact_dir=args.artifact_dir,
        baseline_options=baseline_options,
        candidate_options=candidate_options,
        context_limit=args.context_limit,
        repeats=args.repeats,
        include_cold=True,
        cancel_probe=args.cancel_probe,
        cancel_after_ms=args.cancel_after_ms,
        timeout=args.timeout,
    )
    print(
        json.dumps(
            {
                "aborted": result["aborted"],
                "artifacts": [artifact["batch_id"] for artifact in result["artifacts"]],
            },
            indent=1,
        )
    )
    return 2 if result["aborted"] else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "compare":
        return _compare_main(args[1:])
    if args and args[0] == "paired":
        return _paired_main(args[1:])
    return _legacy_main(args)


if __name__ == "__main__":
    sys.exit(main())
