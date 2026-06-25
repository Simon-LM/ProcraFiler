<!-- @format -->

# Choosing the AI (providers & models)

ProcraFiler reads and classifies your documents with AI. **You choose which AI**,
per task, in your env file — nothing is hardcoded. `procrafiler setup` offers the
common choices and writes them for you; you can also edit the env file directly.

There are two providers:

- **`mistral`** — the **Mistral API** (online). Simple, reliable, fast. Needs a
  `MISTRAL_API_KEY`. **This is the default**, because it just works.
- **`ollama`** — **local** models via [Ollama](https://ollama.com) (`http://localhost:11434`).
  Free and fully offline, but **slower** and dependent on your **GPU VRAM**.

Each task is an env variable holding `provider:model` (a fallback can be added after
a comma): `PROCRAFILER_AI_<TASK>_PRIMARY`. The tasks that are actually used:

| Task | What it does |
|------|--------------|
| `ANALYSIS` | reads the content → the whole fiche (name, date, category, summary, keywords) |
| `ORGANIZE` | groups a folder dropped as a set into a dated affair/series folder |
| `OCR` | reads scanned / image-only PDFs |
| `IMAGE` | reads images with a vision model |

A task left empty just sends files that would need it to manual review.

## Profile 1 — All API (Mistral) · recommended

The defaults. One key and everything works.

```dotenv
PROCRAFILER_AI_ANALYSIS_PRIMARY=mistral:mistral-small-latest
PROCRAFILER_AI_ORGANIZE_PRIMARY=mistral:mistral-medium-latest
PROCRAFILER_AI_OCR_PRIMARY=mistral:mistral-ocr-latest
PROCRAFILER_AI_IMAGE_PRIMARY=mistral:mistral-medium-latest
MISTRAL_API_KEY=<your key>
```

> Keep `mistral-ocr-latest` (not a pinned version) so you automatically get the
> newest Mistral OCR.

## Profile 2 — All local (Ollama) · private & free, slower

No key, nothing leaves your machine. Pull the models first
(`ollama pull qwen3.5:9b`, etc.) and keep Ollama running. Expect **~minutes per
document**, not seconds — so set **generous timeouts** (see below).

```dotenv
PROCRAFILER_AI_ANALYSIS_PRIMARY=ollama:qwen3.5:9b
PROCRAFILER_AI_ORGANIZE_PRIMARY=ollama:gemma4:12b
PROCRAFILER_AI_OCR_PRIMARY=ollama:minicpm-v
PROCRAFILER_AI_IMAGE_PRIMARY=ollama:qwen2.5vl:7b
# Local inference is slow + varies by machine and file size — be generous:
PROCRAFILER_AI_TIMEOUT=600
```

### Pick local models by VRAM

| GPU VRAM | Analysis | Organize | OCR | Image (vision) |
|----------|----------|----------|-----|----------------|
| **~8 GB** | `qwen3.5:9b` (6.6 GB) | `qwen3.5:9b` | `minicpm-v` (5.5 GB) | `qwen2.5vl:7b` (6 GB) |
| **12 GB** | `qwen3.5:9b` (fits, no CPU spill) | `gemma4:12b` | `minicpm-v` | `qwen2.5vl:7b` |
| **16 GB+** | `qwen3.5:9b` *or* `gemma4:12b` | `gemma4:26b` *or* `mistral-small` | `minicpm-v` | `qwen2.5vl:7b` |

Tested (on a 12 GB GPU): **`qwen3.5:9b` analysis ✅** — clean JSON incl. the document
date, ~87 s/call, and at 6.6 GB it **fits 12 GB VRAM** (no spill to CPU → faster +
cooler than `gemma4:12b`). `minicpm-v` OCR ✅, `qwen2.5vl:7b` vision ✅, `gemma4:12b`
analysis/organize ✅ (7.6 GB — spills a bit at 12 GB). Local `ORGANIZE` on the small
model and `gemma4:26b` are not yet validated. Avoid reasoning/coder/guard models
(`deepseek-r1`, `*-coder`, `llama-guard`) — they don't return clean JSON.

> **Why generous timeouts.** A local model that is merely *slow* on a weaker machine
> or a large file shouldn't be killed mid-generation and dropped to manual review. The
> per-call timeout (`PROCRAFILER_AI_TIMEOUT`, or per task `PROCRAFILER_AI_<TASK>_TIMEOUT`)
> defaults to 60 s — fine for the fast Mistral API, **too short for local**. Set it high
> (e.g. **600 s**) for Ollama. (`qwen3.5:9b`'s earlier "empty" results were just the
> 60 s default cutting off its ~87 s generation.)

## Profile 3 — Mixed

Mix per task. A common one: keep **reading** (OCR/IMAGE) on the API where quality
matters most, run **analysis** locally to save cost — or the reverse.

```dotenv
PROCRAFILER_AI_ANALYSIS_PRIMARY=ollama:gemma4:12b
PROCRAFILER_AI_ORGANIZE_PRIMARY=ollama:gemma4:12b
PROCRAFILER_AI_OCR_PRIMARY=mistral:mistral-ocr-latest
PROCRAFILER_AI_IMAGE_PRIMARY=mistral:mistral-medium-latest
MISTRAL_API_KEY=<your key>
```

> Even when the rest is local, **OCR is usually best on the Mistral API** (OCR 4):
> local OCR renders pages to images and reads them with a vision model, which is
> slower and less accurate on dense scans.

## Honest summary

- **API = the easy, fast, reliable default.** Start here.
- **Local = privacy + zero cost, at the price of speed** and some setup; quality is
  good for analysis with `gemma4:12b`, more variable for OCR/organize.
- Check your configuration any time with `procrafiler doctor` (it lists each task's
  provider chain and whether the key is set).
