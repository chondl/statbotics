# CLAUDE.md — `src/data/`

## Seeding EPA Means from Preseason Events

Before week 1 data exists, `avg.py` has a year-specific override block that hardcodes the component means used to seed EPA ratings. The block is skipped automatically once week 1 matches are present.

The store is GCS Parquet: run the aggregates with DuckDB over `parquet/{YEAR}/matches.parquet`. The Parquet lives at a content-hashed key, so resolve it through the manifest first, then query the downloaded file (note: RP columns are booleans and require an `::int` cast):

```bash
KEY=$(curl -s --compressed https://storage.googleapis.com/statbotics-staging-site/manifest.json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['blobs']['parquet/{YEAR}/matches.parquet'])")
curl -s -o matches.parquet "https://storage.googleapis.com/statbotics-staging-site/$KEY"
```

```sql
-- duckdb (CLI, or python: duckdb.connect().execute(...)); verified against a
-- downloaded staging parquet on 2026-07-21.
SELECT
    AVG((red_score + blue_score) / 2.0)           AS score_mean,
    STDDEV((red_score + blue_score) / 2.0)        AS score_sd,
    AVG((red_no_foul + blue_no_foul) / 2.0)       AS no_foul_mean,
    AVG((red_foul + blue_foul) / 2.0)             AS foul_mean,
    AVG((red_auto + blue_auto) / 2.0)             AS auto_mean,
    AVG((red_teleop + blue_teleop) / 2.0)         AS teleop_mean,
    AVG((red_endgame + blue_endgame) / 2.0)       AS endgame_mean,
    AVG((red_rp_1::int + blue_rp_1::int) / 2.0)   AS rp_1_mean,
    AVG((red_rp_2::int + blue_rp_2::int) / 2.0)   AS rp_2_mean,
    AVG((red_rp_3::int + blue_rp_3::int) / 2.0)   AS rp_3_mean,
    -- Include comp_0 through comp_N where N is the number of comps defined
    -- for that year in key_to_name[YEAR] in src/breakdown.py (max 10, varies by year)
    AVG((red_comp_0 + blue_comp_0) / 2.0)         AS comp_0_mean,
    AVG((red_comp_1 + blue_comp_1) / 2.0)         AS comp_1_mean,
    -- ... through comp_N_mean
    COUNT(*)                                      AS num_matches
FROM read_parquet('matches.parquet')
WHERE event = '{YEAR}week0' AND status = 'Completed';
```

(To skip the download, `INSTALL httpfs; LOAD httpfs;` lets `read_parquet` take the `https://storage.googleapis.com/statbotics-staging-site/$KEY` URL directly.)

Set `tiebreaker_mean` based on the year's specific tiebreaker rule — it is not always `no_foul_mean`. Check `clean_breakdown_{year}()` in `src/tba/breakdown.py` to confirm what value is assigned to `tiebreaker` for that year. Add a `# TODO: Remove once week 1 data is available` comment to the override block.
