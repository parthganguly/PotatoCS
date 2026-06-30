from __future__ import annotations

import argparse
import json
from pathlib import Path

from odysseus_desktop_backend.vision_benchmarks.runner import run_benchmark
from odysseus_desktop_backend.vision_benchmarks.schema import DEFAULT_ROUTES_PATH, DEFAULT_SUITE_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Odysseus Visual Common Sense Benchmark.")
    parser.add_argument("--suite", default=str(DEFAULT_SUITE_PATH), help="Path to vision common-sense suite.json.")
    parser.add_argument("--routes", default=str(DEFAULT_ROUTES_PATH), help="Path to route configuration JSON.")
    parser.add_argument("--route", default="", help="Route id to run.")
    parser.add_argument("--out", default="", help="Output directory. Defaults to reports/vision_common_sense/<run_id>.")
    parser.add_argument("--run-id", default="", help="Optional explicit deterministic run id.")
    parser.add_argument("--case", default="", help="Run only one case id.")
    parser.add_argument("--limit", type=int, default=0, help="Limit cases for a smoke or partial run.")
    parser.add_argument("--local-image-dir", default="", help="Directory used for ignored local_images/* fixture paths.")
    parser.add_argument("--include-local-paths", action="store_true", help="Record absolute/private image paths in result JSONL.")
    parser.add_argument("--profile-dir", default="", help="Optional sidecar profile directory for real route execution.")
    parser.add_argument("--smoke", action="store_true", help="Run deterministic plumbing smoke mode without Florence or Ollama.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    route_id = args.route or ("smoke_stub" if args.smoke else "florence_llama_1b")
    result = run_benchmark(
        suite_path=args.suite,
        routes_path=args.routes,
        route_id=route_id,
        out_dir=args.out or None,
        run_id=args.run_id,
        smoke=args.smoke,
        case_id=args.case,
        limit=args.limit,
        local_image_dir=Path(args.local_image_dir) if args.local_image_dir else None,
        include_local_paths=args.include_local_paths,
        profile_dir=Path(args.profile_dir) if args.profile_dir else None,
    )
    printable = {
        "run_id": result["run_id"],
        "route_id": result["route_id"],
        "output_dir": result["output_dir"],
        "summary": result["summary"],
        "paths": result["paths"],
    }
    print(json.dumps(printable, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
