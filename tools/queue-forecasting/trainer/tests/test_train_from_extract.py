"""One whole cohort, trained from a frozen extract with no DATABASE_URL set.

This is the acceptance shape of the probe path, minus the sandbox: the trainer
gets a directory of Parquet files and a manifest, and has to produce a model and
a manifest that names the data it saw. `DATABASE_URL` is deliberately absent --
under `--network none` it is absent for real, and every loader that reaches for
it raises `KeyError` before training starts, which is the failure this path
exists to remove.

The data is synthetic and the numbers are meaningless. What is asserted is that
the run completes, that nothing touched Postgres, and that the manifest carries
the extract's identity.
"""
from __future__ import annotations

import datetime
import json
from datetime import timezone

import pandas as pd
import pytest

from src import extract_source as xs
from src import train
from tests.test_extract_source import _run_row, write_extract

UTC = timezone.utc
AS_OF = datetime.datetime(2026, 4, 20, tzinfo=UTC)
HOLDOUT_DAYS = ["2026-04-15", "2026-04-16", "2026-04-17", "2026-04-18",
                "2026-04-19"]

CONFIG = """
target: wait_time
target_column: wait_duration_s

lookback_days: 13
holdout_days: 5
validation_days: 1
as_of_date: "2026-04-20"

filters:
  - "r.started_at IS NOT NULL"
  - "r.queue_pending IS NOT NULL"
  - "r.wait_duration_s IS NOT NULL"
  - "r.wait_duration_s >= 0"

categorical_features:
  - task_queue_id
  - priority_at_pending
  - tags.kind

numeric_features:
  - queue_pending
  - max_run_time_s
  - hour_sin
  - hour_cos

derived_features:
  cyclical_time:
    source: pending_at

model_type: lightgbm
quantiles: [0.5, 0.9]
model_params:
  num_leaves: 7
  learning_rate: 0.2
  n_estimators: 15
  min_data_in_leaf: 5
"""


def _synthetic_runs():
    """Enough rows across every day of the window for a model to fit.

    The wait varies with `queue_pending` so there is a signal to learn; a
    constant target makes LightGBM emit a single-leaf tree, which trains fine
    and would hide a broken feature path.
    """
    rows = []
    for day in range(1, 20):
        for hour in range(0, 24, 2):
            queue_pending = 1 + (day * hour) % 40
            rows.append(_run_row(
                day, hour,
                task=f"t-{day:02d}-{hour:02d}",
                wait=float(10 + queue_pending * 7),
                queue_pending=queue_pending,
                kind="build" if hour % 4 == 0 else "test",
            ))
    return rows


def _write_baselines(directory):
    """The per-day baseline JSONs `_require_baselines` insists on.

    Only `mae` and `within_2x` are read for the comparison
    (`evaluate.aggregate_days`), so only those are written.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for day in HOLDOUT_DAYS:
        (directory / f"{day}.json").write_text(json.dumps({
            "eval_date": day,
            "wait": {
                "mae": {"eligible_n": 12, "sum_abs_error": 3600.0},
                "within_2x": {"eligible_n": 12, "hit_n": 6},
            },
        }))


@pytest.fixture
def cohort(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv(xs.ENV_DIR, raising=False)
    xs._reset_for_tests()

    extract = write_extract(tmp_path / "extract", runs_rows=_synthetic_runs())
    _write_baselines(tmp_path / "data" / "baseline")
    monkeypatch.setattr(train, "TRAINER_ROOT", tmp_path)
    monkeypatch.setattr(train, "MODELS_DIR", tmp_path / "data" / "models")

    config_path = tmp_path / "wait_time_smoke.yaml"
    config_path.write_text(CONFIG)
    yield config_path, extract, tmp_path
    xs._reset_for_tests()


def test_cohort_trains_from_an_extract_and_records_its_provenance(cohort):
    config_path, extract, root = cohort

    rc = train.main(["--config", str(config_path),
                     "--from-extract", str(extract)])
    assert rc == 0

    run_dir = root / "data" / "models" / "2026-04-20"
    manifest = json.loads(
        (run_dir / "wait_time_smoke_manifest.json").read_text())

    # The model exists and is servable.
    assert (run_dir / "wait_time_smoke_p50.lgb").exists()
    assert (run_dir / "wait_time_smoke_p90.onnx").exists()

    # The manifest names the data, not a local scratch file. Both cache fields
    # are None because this path writes no cache -- see extract_source's
    # module docstring.
    lineage = manifest["training_lineage"]
    assert lineage["training_cache_content_sha256"] is None
    assert lineage["extract"]["extract_hash"] == "e" * 64
    assert lineage["extract"]["extract_dir"] == str(extract.resolve())
    assert set(lineage["extract"]["files"]) >= {"runs", "daily_health"}

    # A holdout was actually scored, over the days the config asked for.
    primary = manifest["evaluation"]["primary"]["aggregate"]
    assert primary["mae"]["eligible_n"] > 0
    assert set(manifest["evaluation"]["primary"]["per_day"]) == set(HOLDOUT_DAYS)


def test_env_var_alone_selects_the_extract(cohort):
    """The env var is the source for anything that can set one.

    NOT the sandbox: a probe's entrypoint is one script path with no arguments
    and no injected environment, so the research-side wrapper has to pass
    `--from-extract /extract` itself. This covers the local shell and compose
    forms.
    """
    config_path, extract, root = cohort
    import os
    os.environ[xs.ENV_DIR] = str(extract)
    try:
        assert train.main(["--config", str(config_path)]) == 0
    finally:
        del os.environ[xs.ENV_DIR]


def test_extract_lineage_never_cites_a_cache_it_did_not_read(cohort, monkeypatch):
    """A host that has trained from Postgres HAS these cache files.

    The extract path reads none of them, so a digest of one in the manifest
    would be provenance that reads as confirmed and is wrong. Asserted with a
    decoy file sitting at exactly the path this cohort's config resolves to.
    """
    config_path, extract, root = cohort
    from src import config as cfg
    from src import data_loader as dl

    cache = root / "cache"
    cache.mkdir()
    monkeypatch.setattr(dl, "CACHE_DIR", cache)
    decoy = dl.cache_path(cfg.load_config(config_path))
    pd.DataFrame({"task_id": ["decoy"], "run_id": [0], "y": [1.0]}) \
        .to_parquet(decoy, index=False)
    assert decoy.exists()

    assert train.main(["--config", str(config_path),
                       "--from-extract", str(extract)]) == 0

    manifest = json.loads(
        (root / "data" / "models" / "2026-04-20"
         / "wait_time_smoke_manifest.json").read_text())
    lineage = manifest["training_lineage"]
    assert lineage["source"] == "extract"
    assert lineage["training_cache_file"] is None
    assert lineage["training_cache_content_sha256"] is None
    assert "engineered_feature_inputs" not in lineage
    assert decoy.name not in json.dumps(manifest)


def test_without_an_extract_the_db_path_is_taken(cohort):
    """The DB path is untouched by this change: with no extract configured and
    no DATABASE_URL, the loader still raises KeyError exactly as before."""
    config_path, _extract, _root = cohort
    with pytest.raises(KeyError, match="DATABASE_URL"):
        train.main(["--config", str(config_path)])


def test_predictions_out_covers_the_unfiltered_holdout(tmp_path, monkeypatch):
    """The evaluator's completeness rule is about the CONTRACT's slice, not the
    config's filtered population.

    `host/evaluator/rows.py:117` refuses a set that omits a primary-slice row on
    a claimed day, and its slice applies none of `c.filters`. So a holdout row
    the config filtered out still has to be predicted -- here, rows with a null
    `queue_pending`, which `r.queue_pending IS NOT NULL` drops from training.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv(xs.ENV_DIR, raising=False)
    xs._reset_for_tests()

    rows = _synthetic_runs()
    # One unfilterable row per holdout day: completed, but no queue_pending.
    for day in (15, 16, 17, 18, 19):
        rows.append(_run_row(day, 1, task=f"t-nofilter-{day}", wait=42.0,
                             queue_pending=None))
    extract = write_extract(tmp_path / "extract", runs_rows=rows)
    _write_baselines(tmp_path / "data" / "baseline")
    monkeypatch.setattr(train, "TRAINER_ROOT", tmp_path)
    monkeypatch.setattr(train, "MODELS_DIR", tmp_path / "data" / "models")
    config_path = tmp_path / "wait_time_smoke.yaml"
    config_path.write_text(CONFIG)

    out = tmp_path / "out" / "predictions.parquet"
    assert train.main(["--config", str(config_path),
                       "--from-extract", str(extract),
                       "--predictions-out", str(out)]) == 0

    preds = pd.read_parquet(out)
    manifest = json.loads(
        (tmp_path / "data" / "models" / "2026-04-20"
         / "wait_time_smoke_manifest.json").read_text())

    # Exactly the frozen contract, in order, closed-world.
    assert list(preds.columns) == list(train.PREDICTION_COLUMNS)
    # A strict superset of what the model was scored on: the five filtered-out
    # rows are present.
    assert len(preds) == manifest["windows"]["holdout"]["rows"] + 5
    assert {f"t-nofilter-{d}" for d in (15, 16, 17, 18, 19)} <= set(preds["task_id"])
    # Non-null everywhere, and row_id is `task_id:run_id`.
    assert not preds.isna().any().any()
    assert (preds["row_id"] == preds["task_id"] + ":"
            + preds["run_id"].astype(str)).all()
    assert (preds["p50"] > 0).all() and (preds["p90_raw"] > 0).all()


# A hazard cohort, end to end. `model_type: discrete_hazard` takes a different
# branch of `main()` -- one that used to `return 0` before the predictions block,
# so the run SUCCEEDED and wrote no `predictions.parquet`, and the probe failed
# minutes later at the handoff with `handoff_missing_artifact`.
#
# The unit tests around `_run_discrete_hazard_training` and `_HazardQuantile`
# cannot see that: the defect was the CALL, not either piece. Deleting the
# `_write_predictions` call in the hazard branch has to fail something, and this
# is the something.
HAZARD_CONFIG = CONFIG.replace(
    "model_type: lightgbm", "model_type: discrete_hazard"
).replace(
    "quantiles: [0.5, 0.9]",
    # The terminal edge is `.inf` because `hazard_labels.bin_edges_seconds`
    # refuses anything else. The last FINITE edge is 2 minutes, chosen so the
    # synthetic waits (10s..290s) STRADDLE it: `DiscreteHazardModel.fit` raises
    # on a bin with an empty training risk set, so a grid whose top edge sits
    # past every wait cannot be fitted at all -- and rows that survive past it
    # give the tail rate observed starts to fit, which is what makes a tail
    # quantile finite. This grid therefore exercises the tail branch of
    # `predict_quantile` rather than avoiding it.
    "quantiles: [0.5, 0.9]\nhazard_bins_minutes: [0, 1, 2, .inf]"
)


def test_a_hazard_cohort_writes_the_same_prediction_contract(tmp_path,
                                                             monkeypatch):
    """One prediction contract, both model types.

    A hazard model trains one booster per wait bin and answers a quantile off
    the survival curve, which is a completely different object from a pair of
    quantile regressors -- and the evaluator must not be able to tell. Same five
    columns, same completeness over the unfiltered holdout, same non-null rule.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv(xs.ENV_DIR, raising=False)
    xs._reset_for_tests()

    rows = _synthetic_runs()
    # The same unfilterable rows as the quantile test: completed, no
    # `queue_pending`, so training drops them and the contract still wants them.
    for day in (15, 16, 17, 18, 19):
        rows.append(_run_row(day, 1, task=f"t-nofilter-{day}", wait=42.0,
                             queue_pending=None))
    extract = write_extract(tmp_path / "extract", runs_rows=rows)
    _write_baselines(tmp_path / "data" / "baseline")
    monkeypatch.setattr(train, "TRAINER_ROOT", tmp_path)
    monkeypatch.setattr(train, "MODELS_DIR", tmp_path / "data" / "models")
    config_path = tmp_path / "wait_hazard_smoke.yaml"
    config_path.write_text(HAZARD_CONFIG)

    out = tmp_path / "out" / "predictions.parquet"
    assert train.main(["--config", str(config_path),
                       "--from-extract", str(extract),
                       "--predictions-out", str(out)]) == 0

    assert out.exists(), ("the hazard branch returned 0 and wrote no prediction"
                          " set -- which is exactly how it failed in a probe")
    preds = pd.read_parquet(out)
    manifest = json.loads(
        (tmp_path / "data" / "models" / "2026-04-20"
         / "wait_hazard_smoke_manifest.json").read_text())
    assert manifest["model_type"] == "discrete_hazard"

    assert list(preds.columns) == list(train.PREDICTION_COLUMNS)
    assert len(preds) == manifest["windows"]["holdout"]["rows"] + 5
    assert {f"t-nofilter-{d}" for d in (15, 16, 17, 18, 19)} <= set(preds["task_id"])
    assert not preds.isna().any().any()
    assert (preds["row_id"] == preds["task_id"] + ":"
            + preds["run_id"].astype(str)).all()
    # FINITE and positive, which is the hazard path's own failure mode: an
    # unplaceable quantile comes back as `inf`, and `inf` is not null.
    import numpy as np
    assert np.isfinite(preds["p50"]).all() and np.isfinite(preds["p90_raw"]).all()
    assert (preds["p50"] > 0).all() and (preds["p90_raw"] > 0).all()
    assert (preds["p90_raw"] >= preds["p50"]).all()
