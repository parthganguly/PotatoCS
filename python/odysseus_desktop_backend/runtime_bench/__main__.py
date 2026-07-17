"""Dev CLI for the benchmark harness.

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

from odysseus_desktop_backend.runtime_bench.harness import run_ollama_batch


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--notes", default="")
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
        notes=args.notes,
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


if __name__ == "__main__":
    sys.exit(main())
