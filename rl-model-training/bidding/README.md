# HOSPILOT Allocation Engine

Auction-based allocation of scarce hospital resources. Two families ship here:

| Family | What is auctioned | Resources |
|---|---|---|
| `bed` | One bed, to one department | `icu` · `hdu` · `pacu` · `resus` · `ed` · `ward` |
| `diagnostic_machine` | One capacity interval on one machine | `ct` · `mri` · `x_ray` · `ultrasound` |

Contending departments bid for the resource across several rounds. A bid is a utility score
built from eight components, clamped by a per-agent budget. Every round records one row per
agent — winners, losers and withdrawals — because the fairness and cap-fitting questions
cannot be answered later from a log that kept only the winner.

> **SYNTHETIC HOSPITAL — SIMULATION ONLY — NOT CLINICALLY VALIDATED.**
> Caps, budgets and reward weights are chosen, not fitted. `mode: live` is refused over HTTP.

## Install

```bash
pip install -e ".[api]"
```

## Run one bed auction

```bash
python -m allocation "ER, OT, and ICU/Ward demand compete for one limited ICU bed"
```

Add `--explain` for every factor, weight and intermediate value; `--json` for a machine.

## Run one diagnostic allocation

```bash
python -m allocation.use_cases.diagnostic_machine scenarios
python -m allocation.use_cases.diagnostic_machine run three_way_contention
python -m allocation.use_cases.diagnostic_machine governance
```

## HTTP

```bash
uvicorn allocation.api.app:create_app --factory --port 8901
```

| Route | Purpose |
|---|---|
| `GET /health` | liveness and the versions this process runs |
| `GET /use-cases` | registered bed profiles, and a query resolving to each |
| `GET /scenarios` | bed scenario names |
| `POST /auction` | run one bed auction, full ladder |
| `POST /session` | many bed auctions against one ledger — shows burn rate |
| `GET /diagnostic/modalities` | the four modalities and their caps/budget tables |
| `GET /diagnostic/scenarios` | deterministic diagnostic fixtures |
| `POST /diagnostic/auction` | run one diagnostic auction, full ladder |

`POST /diagnostic/auction` names the world exactly one way — `scenario`, `query`, or
`machines` + `requests`. The last carries patient data and is refused unless the process was
started with `ALLOCATION_API_KEY` set, the same rule `candidates` follows on `POST /auction`.

```bash
curl -s localhost:8901/diagnostic/auction \
  -H 'content-type: application/json' \
  -d '{"scenario": "three_way_contention"}'
```

## Layout

```
allocation/
  contracts.py            the shared vocabulary
  auction/ budget/        the auction core and the per-agent ledgers
  utility/ features/      the eight components and their normalisation
  ingest/ pathway/        one read of the world; what a loser does next
  audit/                  one row per agent per round
  profiles/               generic ResourceProfile machinery — no use case here
  config/                 shared tables: thresholds, auction, rules, bed caps/budgets
  api/  cli.py            the composition root: the only code that names a family
  use_cases/
    bed/profiles/         the six bed profiles
    diagnostic_machine/   contracts, utility, scoring, policy, auction, config, serving
```

Core layers never import a use case — `tests/test_diagnostic_boundaries.py` enforces it
statically, exempting only the composition root, which has to register the families somewhere.

Bed configuration stays in `allocation/config/` rather than moving under `use_cases/bed/`.
`config_version` is a digest over every file the loader globs from one directory, and
`diagnostic_machine.config.for_modality` merges its modality tables into a base `Config`
loaded from that same directory. Splitting that seam per family is a loader change, not a
file move.

## Configuration

`config_version` and `caps_version` are content hashes, stamped onto every run and every
audit row, so a score can be re-derived later against the exact tables that produced it.
`GET /config` and `python -m allocation.use_cases.diagnostic_machine governance` report which
tables are unsigned.

Only `icu_bed` carries fitted-for-purpose caps. The other five bed types inherit ICU's and
say so on every run. Each diagnostic modality has its own caps and budget tables.

## Tests

```bash
pip install -e ".[api,dev]"
python -m pytest
```

Sixteen tests skip unless `artifacts/er_policy.json` is present — they cover serving a trained
bed Q-policy, and that artifact is produced by the research tree, not by this package.

## Scope

This package **serves** policies; it does not fit them. Training, baselines, counterfactual
tooling and the research harness live in the research tree and are deliberately absent here.
