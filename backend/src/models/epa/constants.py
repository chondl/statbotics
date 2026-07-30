NORM_MEAN = 1500
NORM_SD = 250
INIT_PENALTY = 0.2

YEAR_ONE_WEIGHT = 0.7
MEAN_REVERSION = 0.4

ELIM_WEIGHT = 1 / 3

# Match count a team's rating carries into an offseason sandbox fork.
# percent_func spans 0.333 -> 0.200 and clamps at 12 matches, so 12 and a full
# season's count are the same setting. The 2026-07-30 backtest over 61 events /
# 3,294 matches measured 0 best on RMSE and Brier; no value removes the
# degraded-event tail, so this is a re-tuning hook, not a risk dial.
SANDBOX_SEED_COUNT = 0
