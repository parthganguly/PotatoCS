#!/usr/bin/env python3
"""Fixture-driven stand-in for the upstream `coli` CLI.

Behavior is selected with the FAKE_COLI_MODE environment variable so the
subprocess wrapper can be exercised without a Colibri installation:

- plan_ok / doctor_ok / doctor_ram_fail / doctor_unknown_version:
  print the matching fixture JSON, exit 0 (1 for doctor_ram_fail, matching
  upstream's error exit code).
- plan_error: mimic upstream cmd_plan failure — plain text on stderr, exit 1.
- doctor_bad_args: mimic upstream exit code 2 with the synthetic
  config.arguments JSON report.
- garbage: non-JSON stdout, exit 0.
- sleep: block longer than any sane wrapper timeout.
- env_probe: report whether secret env vars leaked into the child.
- echo_argv: print received argv as JSON (argv-construction test).
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODE = os.environ.get("FAKE_COLI_MODE", "doctor_ok")


def emit_fixture(name: str) -> None:
    sys.stdout.write((HERE / name).read_text(encoding="utf-8"))


def main() -> int:
    if MODE == "echo_argv":
        print(json.dumps({"argv": sys.argv[1:]}))
        return 0
    if MODE == "env_probe":
        print(
            json.dumps(
                {
                    "coli_api_key_present": "COLI_API_KEY" in os.environ,
                    "odysseus_key_present": "ODYSSEUS_COLIBRI_API_KEY" in os.environ,
                }
            )
        )
        return 0
    if MODE == "sleep":
        time.sleep(10)
        return 0
    if MODE == "garbage":
        print("colibri doctor \xb7 pretty human output, not JSON")
        return 0
    if MODE == "plan_ok":
        emit_fixture("plan_v2.json")
        return 0
    if MODE == "plan_error":
        print("cannot create resource plan: no safetensors shards: /bad/path", file=sys.stderr)
        return 1
    if MODE == "hostile":
        # Adversarial failure: stderr and stdout stuffed with everything the
        # wrapper must never surface — paths, usernames, keys, env contents.
        hostile = os.environ.get("FAKE_COLI_HOSTILE_TEXT", "HOSTILE")
        env_dump = json.dumps(dict(os.environ))
        print(f"scanning {hostile} as user {hostile}\n{env_dump}", file=sys.stderr)
        print(f"fatal: cannot read {hostile}", file=sys.stdout)
        return 1
    if MODE == "plan_unknown_version":
        plan = json.loads((HERE / "plan_v2.json").read_text(encoding="utf-8"))
        plan["version"] = 99
        print(json.dumps(plan))
        return 0
    if MODE == "doctor_ok":
        emit_fixture("doctor_ok.json")
        return 0
    if MODE == "doctor_ram_fail":
        emit_fixture("doctor_ram_fail.json")
        return 1
    if MODE == "doctor_unknown_version":
        emit_fixture("doctor_unknown_version.json")
        return 0
    if MODE == "doctor_bad_args":
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "error",
                    "model": "/nvme/glm52_i4",
                    "checks": [
                        {"id": "config.arguments", "status": "fail", "summary": "--ctx must be positive"}
                    ],
                    "plan": None,
                }
            )
        )
        return 2
    print(f"unknown FAKE_COLI_MODE: {MODE}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
