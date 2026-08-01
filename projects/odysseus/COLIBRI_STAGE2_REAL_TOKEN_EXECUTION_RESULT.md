# Colibrì Stage 2 — successful real-token execution

## Verdict

**PASS.** The reviewed Colibrì Stage 2 stack generated the independently expected token in a human-approved interactive run on 2026-07-31.

This document records the closed JSON emitted by the reviewed CLI. It does not include the operator's username, absolute engine path, converted-model path, environment values, raw engine output, or prompt token sequence.

## Reviewed code and artifact boundary

- PR branch before this evidence-only commit: `research/colibri-stage2-real-token`
- Reviewed execution head: `e9712960a4749ebddb50df53e5a5c98cb91bc795`
- Colibrì commit: `72d3d37231e922a6fa9afca16e08fa45842d5eb4`
- Model: `allenai/OLMoE-1B-7B-0125-Instruct`
- Model revision: `b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e`
- Converter kind: `bounded`
- Cache cap / expert bits: `8` / `8`

The engine and converted artifacts were accepted only after matching the immutable identities in `REVIEWED_OLMOE_MODEL_REGISTRY`. The CLI exits `0` only when the parsed engine token independently matches the reviewed expected token and cleanup evidence passes.

## Closed execution evidence

```json
{
  "category": "passed",
  "cleanup": {
    "cleanup_complete": true,
    "descendant_count": 0,
    "job_empty_proven": true,
    "job_member_count": 0,
    "orphan_free": true,
    "reference_removed": true,
    "reference_session_removed": true,
    "root_exit_confirmed": true,
    "session_created": true
  },
  "evidence_sha256": "3af9be97a50db1ac6671a18bda40179151690c3208aba429462b2358094de288",
  "failure_metadata": {},
  "identities": {
    "bits_argument": "8",
    "cap_argument": "8",
    "colibri_commit": "72d3d37231e922a6fa9afca16e08fa45842d5eb4",
    "config_sha256": "272998dd7ba4846dcc682f0b5a46144f4bcd9dde8e94d2f17bd8e5cf2f23d6ce",
    "converter_kind": "bounded",
    "converter_sha256": "6f8145fc71f060c75d7d04a34c96cfd58d00daa3d51f2406a6de25e167d2266b",
    "engine_sha256": "d7beaf6fe35de265cfaeb1d07914deeea6ceb8b3650e79b76e9c6d77176b528d",
    "model_repository": "allenai/OLMoE-1B-7B-0125-Instruct",
    "model_revision": "b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e",
    "reference_sha256": "eb27ccf4ab02b54ada485f719c117265e2196c68c57dcca38a9b8886bfb28b1c",
    "shard_sha256": [
      "3b9ad7f9dd39448887c61d590f84e69138e09f6c2e0f337970f4453f5c0f61b2",
      "8f6861509c003f44c395044736a4052651b68fc6e095a11f351cf106330d416f",
      "06aa55f9ffb055dfb2e51ee3b6c2297061eb98e5beeb94172ed27900e57e4af9"
    ]
  },
  "latency": {
    "end_to_end_latency_ms": 6903,
    "end_to_end_latency_state": "measured",
    "first_output_latency_ms": 6867,
    "first_output_latency_state": "measured",
    "generation_latency_ms": 5000,
    "generation_latency_state": "measured",
    "model_load_latency_ms": 1800,
    "model_load_latency_state": "measured"
  },
  "memory": {
    "peak_tree_memory_bytes": 2742247424,
    "peak_tree_memory_state": "measured"
  },
  "process": {
    "exit_category": "clean_exit",
    "exit_code": 0
  },
  "schema_version": "colibri-stage2-olmoe-token-evidence-v3",
  "state": "verified",
  "token": {
    "contract_expected_count": 1,
    "engine_reported_expected_count": 1,
    "expected_token_id": 7785,
    "generated_token_id": 7785,
    "matched_count": 1
  }
}
```

The operator recorded CLI process exit code `0` immediately after this document.

## What this proves

- The real reviewed `olmoe.exe` executed.
- The real converted OLMoE artifact set loaded.
- The engine generated token `7785`, matching the independently reviewed expected token `7785`.
- Model-load, generation, end-to-end latency, and process-tree peak memory were measured.
- The root exited cleanly.
- The Windows Job Object reported zero members after teardown.
- No descendants or orphan process remained.
- The private reference file and private session directory were removed.

## Claim boundary

This closes the Stage 2 question: **the reviewed Colibrì execution path works for real model inference on the tested consumer Windows machine.**

It does not establish interactive performance for GLM-5.2, production readiness, automatic model setup, installer support, or a user-facing PotatoCS feature. Those are separate product-integration questions.

No rerun was performed for this evidence commit. No model, engine, converted artifact, or local runtime directory was changed by this repository update.