# Odysseus Visual Common Sense Benchmark

This benchmark asks a product-level question: can Odysseus answer ordinary image questions with useful, grounded local evidence?

It is separate from the RAG reliability benchmark, model-ranking reports, prompt-comparability reports, and campaign reports. It is not a public leaderboard, an academic proof, or a GPT-4o comparison.

## Fixture Schema

`suite.json` contains:

- `images`: generated or local image references with `id`, `path`, `license`, `source_note`, and `safe_to_thumbnail`.
- `cases`: one image-scoped conversation each, with a `category`, scorer notes, and one or more `turns`.
- `turns`: each question includes expected concepts, acceptable concepts, forbidden concepts, whether abstention is expected, whether follow-up evidence should be reused, and notes for human scorers.

Paths under `fixtures/` are committed generated images. Paths under `local_images/` are ignored and are for private manual runs.

## Manual Scoring

Use the generated `manual_scores.csv`:

- `2` = Good
- `1` = Partly right
- `0` = Wrong / useless
- `H` = Hallucination
- `A` = Correct abstention
- `S` = Skipped

## CLI

Smoke mode validates benchmark plumbing without Florence or Ollama:

```powershell
python -m odysseus_desktop_backend.vision_benchmarks.run --smoke
```

Real local routes run only when required local components already exist:

```powershell
python -m odysseus_desktop_backend.vision_benchmarks.run --route florence_llama_1b
```

No route pulls, installs, creates, or removes models.
