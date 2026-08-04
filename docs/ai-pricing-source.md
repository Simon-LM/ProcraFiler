<!-- @format -->

# Where AI prices come from — spec for the companion `ai-pricing` repository

ProcraFiler measures what a run consumes (tokens, OCR pages) but deliberately does
**not** know what any of it costs. This document specifies the separate repository
that supplies prices, and is written to be handed to someone — or some agent —
building that repository from nothing.

Two audiences, one file: the **contract** section is binding on ProcraFiler too,
because it is what this app will read.

---

## 1. Why a separate repository at all

**Mistral publishes no machine-readable pricing.** Verified 2026-07-30:

- there is no pricing endpoint in the API (`docs.mistral.ai/api/`);
- `GET /v1/admin/usage` does return a `prices` field, but requires an **admin**
  API key, which an ordinary user of an open-source tool does not have and should
  never be asked for;
- the public page `https://mistral.ai/pricing/api` carries the figures as plain
  text — readable, but a marketing page with no contract and no versioning.

So somebody, somewhere, has to read a web page. The whole design question is
**where that happens**, and the answer is: not on the user's machine.

If ProcraFiler scraped the page itself, a redesign of that page would not crash the
app — it would make it read the **wrong number** and cheerfully announce €0.80 for
a run that costs €8. Worse, it would do so in every installation at once. Scraping
belongs in CI, where a human sees the diff before anything reaches a user.

A dedicated repository also serves goals ProcraFiler alone does not:

- **one source for several projects** — the price of `mistral-medium-latest` is not
  a ProcraFiler fact;
- **price history for free** — `git log` on a single JSON file is a record nobody
  else publishes;
- **an independent release cycle** — a price correction must not require cutting a
  ProcraFiler release, tagging, and asking every user to upgrade.

## 2. The contract — `pricing.json`

Consumers fetch exactly one URL:

```
https://raw.githubusercontent.com/<owner>/ai-pricing/main/pricing.json
```

```json
{
  "schema_version": 2,
  "providers": {
    "mistral": {
      "checked_utc": "2026-08-04T00:00:00Z",
      "updated": "2026-08-04",
      "source": "https://mistral.ai/pricing/api",
      "currency": "USD",
      "models": {
        "mistral-small-latest": {
          "in_per_mtok": 0.15,
          "out_per_mtok": 0.6,
          "display_name": "Mistral Small 4"
        }
      }
    },
    "ovh": {
      "checked_utc": "2026-08-04T00:00:00Z",
      "updated": "2026-08-04",
      "source": "https://www.ovhcloud.com/fr/public-cloud/ai-endpoints/catalog/",
      "currency": "EUR",
      "models": {
        "whisper-large-v3-turbo": {
          "per_audio_second": 1.278e-05,
          "display_name": "whisper-large-v3-turbo"
        }
      }
    }
  }
}
```

**Prices are keyed by SELLER first.** The same model is sold by more than one
provider, at different prices — and, the part a flat table cannot express at all,
in different **currencies**: Mistral publishes in USD, OVH in EUR. Currency, the
source page and the freshness date all belong to the seller, not to the model,
which is why they sit inside the provider rather than at the top of the file.

Model ids are the seller's own. OVH does not call a model
`mistral-small-latest`, so these are genuinely different keys rather than two
prices for one key.

Field by field, and why each exists:

| field | meaning |
| --- | --- |
| `schema_version` | A consumer that does not recognise it must ignore the file and fall back, rather than misread it. Bump on any breaking change. |
| `providers` | Keyed by a short seller id (`mistral`, `ovh`). Consumers match it against their own provider name. |
| `checked_utc` | When the scraper last **verified** that seller's figures. Distinct from `updated`, and the difference matters: "confirmed unchanged today" is a much stronger statement than "last edited in May". |
| `updated` | When one of that seller's figures last actually **changed**. |
| `source` | The page the numbers came from, so a human can check in one click. |
| `currency` | Per seller. Never assume, and never convert. |
| `models` | Keyed by the **API model id** as that seller names it — see §3. |
| `display_name` | The marketing name, kept only so a diff is readable by a human. Never used for matching. |

### Units

Explicit in the key name so a consumer cannot silently apply a per-token price to
a per-page model:

| key | unit |
| --- | --- |
| `in_per_mtok` / `out_per_mtok` | per million tokens |
| `per_1k_pages` | per thousand pages |
| `per_audio_minute` | per minute of audio |
| `per_audio_second` | per second of audio |
| `free: true` | the seller charges nothing |

**Do not** add a generic `price` field, and **do not** normalise units: publish
what the page publishes, so any figure can be checked against its source. Mistral
prices Voxtral per minute and OVH prices Whisper per second; converting one into
the other is a factor-of-sixty waiting to happen.

A model may carry **several units at once**. `voxtral-small-latest` bills per
minute of audio *and* per million text tokens; any consumer assuming one unit per
model silently drops half its bill.

`free: true` is a **fact**, not an absence. A consumer must be able to tell "this
costs nothing" from "I have no price for this" — the second must never be shown
as zero.

Only models actually consumed need to be present, but publishing a seller's whole
catalogue costs nothing and spares a release when a project switches model.

## 3. The trap that will eventually bite: aliases

`mistral-medium-latest` is an **alias**. Today it resolves to "Mistral Medium 3.5";
tomorrow it will resolve to something else, **at a different price, with no change
anywhere in this file or in ProcraFiler**.

The scraper therefore matches marketing names to API ids through an **explicit,
hand-maintained mapping** committed in the repo — never through fuzzy matching,
never by lowercasing and hoping. When the page shows a name the mapping does not
know, that is a **failure to report**, not a row to guess at.

This is also why every consumer must display the date alongside any converted
figure: `≈ $0.80 (rates of 2026-07-30)`, never `$0.80`.

## 4. Safety rules, in priority order

1. **Never publish an unreviewed number.** A price *change* opens a pull request.
   It is not committed to `main` by the workflow.
2. **Fail loudly, never silently.** If the page cannot be fetched, if the layout
   no longer parses, or if a model in the mapping is absent from the page: open an
   issue, fail the job, and **leave `pricing.json` untouched**. A stale price the
   user can see the date of is far better than a wrong one they cannot.
3. **Sanity bounds.** Reject any figure outside a plausible range (suggested:
   0.001–1000 in the file's currency unit) and any change larger than a factor of
   5 from the committed value. Both mean "the page changed shape", not "the price
   moved". Refuse, report, keep the old file.
4. **No secrets.** The scraper reads a public page. It needs no API key of any
   kind, and must never be given one.

## 5. The workflow, and the 60-day problem

GitHub disables a scheduled workflow after **60 days without repository activity**,
and **only new commits reset that timer** — tags, issues and merged pull requests
do not. A repository whose content changes every two or three months is precisely
the profile that gets silently switched off.

The fix falls out of the design rather than being bolted on: on every run, whether
or not a price moved, the workflow commits the updated `checked_utc`. That is a
real piece of information for consumers *and* it keeps the schedule alive.

```yaml
on:
  schedule:
    - cron: "0 4 * * 1" # Mondays, 04:00 UTC
  workflow_dispatch: # so a human can force a check
```

Three outcomes, and the job must implement all three:

| outcome | action |
| --- | --- |
| figures unchanged | commit `checked_utc` only, straight to `main` |
| a figure changed | update `pricing.json`, open a **pull request**, do not merge |
| fetch or parse failed | open an issue, fail the job, change nothing |

> Note: a commit pushed with `GITHUB_TOKEN` does not itself trigger further
> workflow runs (GitHub's anti-loop rule). That is fine here — the trigger is the
> schedule, not the push — but do not build anything that depends on it.

Actions minutes are **free and unlimited on public repositories**, so this costs
nothing. Keep the repository public.

## 6. Seed values

Read from `https://mistral.ai/pricing/api` on **2026-07-30**. Treat as a starting
point to be confirmed by the scraper's first successful run, not as authority:

| API model id | marketing name | input | output |
| --- | --- | --- | --- |
| `mistral-medium-latest` | Mistral Medium 3.5 | $1.5 /Mtok | $7.5 /Mtok |
| `mistral-small-latest` | Mistral Small 4 | $0.15 /Mtok | $0.6 /Mtok |
| `mistral-ocr-latest` | OCR 4 | $4 per 1 000 pages | — |

The alias→name mapping above is an **assumption**, not a verified fact: it is the
plausible reading of the pricing page, and §3 is exactly why it must be written
down explicitly and revisited.

## 7. Explicit non-goals

- **No currency conversion.** Publish what the source publishes.
- **No token estimation.** This repository knows prices, never volumes.
- **No account or usage data.** Nothing here touches anyone's Mistral account.
- **No auto-merge of price changes**, however confident the parser looks.

## 8. What ProcraFiler does with it (the consumer side, for context)

Not part of the companion repository's work, listed so its author knows what the
file has to survive:

1. A copy of `pricing.json` **ships inside the ProcraFiler package**, so a machine
   with no network still has dated figures.
2. A refresh from the URL above, **at most weekly**, cached locally, **never
   blocking a run**, and disableable outright. Implemented in `pricing_refresh`:
   5-second timeout, every failure swallowed, the attempt recorded whether or not
   it succeeded (so an offline machine tries once a week rather than every run).
3. A user-level override file that wins over both, for negotiated rates or another
   provider.
4. Conversion happens only at display time, always with the date attached.

The consumer **validates before it trusts**, and two of those checks are worth
knowing about on the publishing side:

- a `schema_version` it does not recognise means the file is ignored entirely,
  never read optimistically. ProcraFiler reads **1 and 2**; a schema-1 file is
  still accepted and its models are treated as applying to any seller, which is
  what such a file meant;
- a file whose figures are all rejected as implausible is **refused as a whole**
  rather than accepted with its prices stripped — otherwise a published mistake
  would replace a working copy with one that cannot price anything.

Consequently: **the file may be fetched by an old client at any time.** Never
remove a field within a `schema_version`; add, and bump when you must break.

---

## 9. Prompt to hand to a fresh session

> Build a small public GitHub repository called `ai-pricing` whose single job is to
> publish machine-readable AI model prices as `pricing.json` at the repository
> root, for other projects to fetch over `raw.githubusercontent.com`.
>
> The full specification — the exact JSON schema and the rationale for each field,
> the safety rules, the workflow's three required outcomes, and the seed values —
> is in `docs/ai-pricing-source.md` of the ProcraFiler repository. Read it first
> and treat it as binding; it is the contract a live consumer already depends on.
>
> Deliver:
>
> 1. `pricing.json` seeded with the values in §6 of that document.
> 2. `scripts/scrape.py` — fetches `https://mistral.ai/pricing/api`, extracts the
>    per-model figures, and maps marketing names to API model ids through an
>    explicit committed mapping (never fuzzy matching). Standard library only if
>    practical; no API key, ever.
> 3. `.github/workflows/refresh.yml` — weekly cron plus `workflow_dispatch`.
>    Unchanged figures → commit the `checked_utc` stamp to `main` (this doubles as
>    the keepalive against GitHub's 60-day scheduled-workflow disable, which only
>    commits reset). Changed figures → open a pull request, never auto-merge.
>    Fetch or parse failure → open an issue, fail the job, leave `pricing.json`
>    untouched.
> 4. Validation enforcing the sanity bounds of §4, with tests covering: a normal
>    price change, a page whose layout no longer parses, a model present in the
>    mapping but missing from the page, and an out-of-bounds figure. Each must
>    produce the specified outcome — especially that `pricing.json` is left
>    unchanged on every failure path.
> 5. A `README.md` stating plainly that these figures are scraped from a public
>    marketing page, are reviewed by a human before publication, and carry no
>    guarantee — with the authoritative link.
>
> Everything in English. Do not add currency conversion, token estimation, or any
> feature that reads a user's Mistral account.
