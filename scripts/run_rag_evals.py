from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from odysseus_desktop_backend.services.eval_service import EvalService, format_benchmark_summary
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.storage import Database


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run local RAG evals against installed Ollama models. "
            "These evals use bundled fixture documents, not the user's imported Documents library. "
            "Results report whether retrieval used semantic Ollama embeddings or the lexical fallback."
        )
    )
    parser.add_argument("--cases", type=Path, default=ROOT / "evals" / "rag_cases_v018")
    parser.add_argument("--models", nargs="*", help="Specific Ollama model names. Defaults to all installed models.")
    parser.add_argument("--verify", action="store_true", help="Enable the optional verifier pass during evals.")
    parser.add_argument("--show-answers", action="store_true", help="Accepted for compatibility; answers are not stored.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Ollama generation temperature for eval runs.")
    parser.add_argument("--style", help="Override answer_style for every eval case.")
    parser.add_argument(
        "--mode",
        choices=["retrieval_only", "oracle_generation", "end_to_end"],
        default="end_to_end",
        help="Benchmark mode.",
    )
    parser.add_argument("--thinking", choices=["off", "on", "auto"], default="off")
    parser.add_argument("--repeats", type=int, choices=[1, 3], default=1)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="odysseus-rag-eval-cli-") as temp:
        db = Database(Path(temp))
        try:
            service = EvalService(db, cases_dir=args.cases)
            listed = service.list_cases()
            if listed["case_count"] == 0:
                print(f"No eval cases found in {args.cases}", file=sys.stderr)
                return 2

            model_names = args.models or installed_ollama_models()
            if not model_names:
                print("No installed Ollama models found at 127.0.0.1:11434.", file=sys.stderr)
                return 2

            print(f"Running {listed['case_count']} RAG eval case(s) against {len(model_names)} model(s).")
            failures = 0
            runs = []
            for model in model_names:
                run = service.run(
                    model=model,
                    verify=args.verify,
                    answer_style_override=args.style,
                    temperature=args.temperature,
                    benchmark_mode=args.mode,
                    thinking_mode=args.thinking,
                    repeats=args.repeats,
                )
                runs.append(run)
                failures += run["total_failed"]
                print(f"\nmodel={model}")
                print(
                    "  embeddings={backend}/{model_name} temperature={temperature:.2f}".format(
                        backend=run.get("embedding_backend") or "unknown",
                        model_name=run.get("embedding_model") or "unknown",
                        temperature=float(run.get("temperature") or 0),
                    )
                )
                for case in run["cases"]:
                    status = "PASS" if case["passed"] else "FAIL"
                    print(
                        f"  {status} {case['case_id']} style={case['answer_style']} "
                        f"embeddings={case.get('embedding_backend')}/{case.get('embedding_model')} "
                        f"latency_ms={case['latency_ms']} "
                        f"expected={case['expected_passed']} forbidden={case['forbidden_passed']} "
                        f"source={case['source_passed']}"
                    )
                    for reason in case["reasons"]:
                        print(f"    - {reason}")

            print("\nSummary:")
            print(format_benchmark_summary(runs))
            return 1 if failures else 0
        finally:
            db.close()


def installed_ollama_models() -> list[str]:
    with tempfile.TemporaryDirectory(prefix="odysseus-rag-eval-models-") as temp:
        db = Database(Path(temp))
        try:
            status = ModelService(db).detect_ollama()
            if not status.get("reachable"):
                return []
            return [str(model) for model in status.get("models", []) if model]
        finally:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
