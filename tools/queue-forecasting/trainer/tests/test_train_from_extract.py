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
