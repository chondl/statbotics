# Statbotics Models

Standalone match prediction models and evaluation framework for FRC.

## Setup

```bash
cd models
poetry install
```

## Get the data

Statbotics has no relational database (retired 2026-07-27). The entity tables
are Parquet files in the public GCS bucket, one directory per season. Pull down
the two tables this runner needs, resolving the content-hashed keys through the
manifest:

```bash
BUCKET=https://storage.googleapis.com/statbotics-staging-site
curl -s --compressed $BUCKET/manifest.json -o manifest.json
for YEAR in $(seq 2002 2026); do
  for TABLE in matches year; do
    KEY=$(python3 -c "import json,sys; m=json.load(open('manifest.json'))['blobs']; print(m.get('parquet/$YEAR/$TABLE.parquet',''))")
    [ -n "$KEY" ] || continue
    mkdir -p parquet/$YEAR && curl -s -o parquet/$YEAR/$TABLE.parquet "$BUCKET/$KEY"
  done
done
```

## Run evaluation

Against the Parquet tree (the default; DuckDB reads it directly):

```bash
poetry run python runner.py --model epa
poetry run python runner.py --model wins --parquet-dir ./parquet
```

Against CSVs instead, if you already have them in the same column layout:

```bash
poetry run python runner.py --model epa --matches-csv matches.csv --years-csv years.csv
```

## Models

### `EPAModel` (`epa_model.py`)

Simplified EPA using total match score. Reproduces the core statbotics EPA logic:

- **Cross-year initialization** — normalized EPA carries across seasons with 0.4 mean reversion toward a slightly-below-average baseline (1450 on a 1500±250 scale).
- **EWMA update** — `epa += weight * percent * (error / num_teams)`, where `percent` starts at ~33% and decays to ~20% after 12+ qual matches.
- **Win probability** — `1 / (1 + 10^(k * norm_diff))` with `k = -5/8` (2008+) or `-5/12` (pre-2008).
- Elimination matches count at 1/3 weight and do not increment the match count.

### `WinsModel` (`wins_model.py`)

Win-rate baseline. Each team accumulates a win rate with a 1W/1L Laplace prior (starts at 0.5). Predicts via:

```
P(red wins) = avg_red_rate / (avg_red_rate + avg_blue_rate)
```

## Adding a new model

Subclass `Model` from `base.py` and implement three methods:

```python
from base import Model

class MyModel(Model):
    def start_year(self, year, score_mean, score_sd, **kwargs): ...
    def predict(self, red1, red2, red3, blue1, blue2, blue3) -> float: ...
    def update(self, red1, red2, red3, blue1, blue2, blue3,
               winner, red_score, blue_score, elim=False): ...
```

Then evaluate it:

```python
from runner import load_from_csv, evaluate, report

matches, years = load_from_csv("matches.csv", "years.csv")
preds = evaluate(MyModel(), matches, years)
report(preds)
```

## Output format

```
  year       n    acc   brier    champs_n  champs_acc  champs_brier
------------------------------------------------------------------------
  2002     ...  0.xxx  0.xxxx         ...       0.xxx        0.xxxx
  ...
------------------------------------------------------------------------
   ALL     ...  0.xxx  0.xxxx         ...       0.xxx        0.xxxx
```

- **acc** — fraction of matches where the predicted winner (prob > 0.5) was correct (ties excluded)
- **brier** — mean squared error on win probability (lower is better; 0.25 = random)
- **champs** — same metrics filtered to championship events only
