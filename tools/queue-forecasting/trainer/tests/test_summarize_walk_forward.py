import json


def _fake_manifest(d: dict) -> str:
    return json.dumps(d, default=str)


def _manifest_payload(target, mae, w2x, p90, hold_start, hold_end, hold_rows=800000):
    return {
        "target": target,
        "evaluation": {
            "primary": {
                "aggregate": {"mae_s": mae, "within_2x_rate": w2x, "p90_coverage_rate": p90},
                "baseline_aggregate": {"mae_s": mae + 100.0, "within_2x_rate": w2x - 0.03},
                "buckets_aggregate": {
                    "<1m":   {"mae_s": 40.0,   "within_2x_rate": 0.45},
                    "1-5m":  {"mae_s": 120.0,  "within_2x_rate": 0.65},
                    "5-30m": {"mae_s": 500.0,  "within_2x_rate": 0.55},
                    "30m+":  {"mae_s": 6000.0, "within_2x_rate": 0.30},
                },
            }
        },
        "windows": {"holdout": {
            "start": hold_start,
            "end":   hold_end,
            "rows":  hold_rows,
        }},
    }


def test_extract_row_and_summary(tmp_path, monkeypatch, capsys):
    # Build a faux models directory so the summarizer picks up our fakes.
    import scripts.summarize_walk_forward as swf

    fake_root = tmp_path / "models"
    fake_root.mkdir()
    for day in ["2026-04-20", "2026-04-21"]:
        day_dir = fake_root / day
        day_dir.mkdir()
        for stem, target, mae, w2x, p90 in [
            ("wait_time",                       "wait_time",    700.0, 0.50, 0.88),
            ("wait_time_residual",              "wait_time",    680.0, 0.52, 0.80),
            ("wait_time_residual_throughput",   "wait_time",    650.0, 0.54, 0.75),
            # A duration config in the same models dir — must end up in its own block
            ("run_duration_residual",           "run_duration", 130.0, 0.89, 0.88),
            # A rejected variant — must NOT appear under the default config filter
            ("wait_time_residual_additive",     "wait_time",    660.0, 0.53, 0.90),
        ]:
            (day_dir / f"{stem}_manifest.json").write_text(_fake_manifest({
                "target": target,
                "evaluation": {
                    "primary": {
                        "aggregate": {"mae_s": mae, "within_2x_rate": w2x, "p90_coverage_rate": p90},
                        "baseline_aggregate": {"mae_s": mae + 100.0, "within_2x_rate": w2x - 0.03},
                        "buckets_aggregate": {
                            "<1m":   {"mae_s": 40.0,   "within_2x_rate": 0.45},
                            "1-5m":  {"mae_s": 120.0,  "within_2x_rate": 0.65},
                            "5-30m": {"mae_s": 500.0,  "within_2x_rate": 0.55},
                            "30m+":  {"mae_s": 6000.0, "within_2x_rate": 0.30},
                        },
                    }
                },
                "windows": {"holdout": {"rows": 800000}},
            }))

    monkeypatch.setattr(swf, "MODELS_DIR", fake_root)

    # Default config filter: wait_time, wait_time_residual_throughput,
    # wait_time_residual_throughput_filtered_baseline, run_duration_residual.
    # (wait_time_residual and wait_time_residual_additive are not in the default.)
    output_path = tmp_path / "out.csv"
    rc = swf.main(["--from", "2026-04-20", "--to", "2026-04-21", "--output", str(output_path)])
    assert rc == 0
    assert output_path.exists()
    content = output_path.read_text()
    lines = content.strip().splitlines()
    # Expect 6 data rows: 2 cohorts × (wait_time + wait_time_residual_throughput +
    # run_duration_residual) — filtered_baseline not in fixtures so not counted.
    assert len(lines) == 1 + 6, f"unexpected line count: {len(lines)}\n{content}"
    # Header has target column.
    assert "cohort_as_of,config,target,baseline_mae" in lines[0]
    # Only whitelisted configs appear in rows.
    joined = "\n".join(lines[1:])
    assert "wait_time_residual_throughput" in joined
    assert "run_duration_residual" in joined
    assert "wait_time_residual_additive" not in joined
    # vanilla wait_time_residual is no longer in the default
    rows_no_header = [row for row in lines[1:] if ",wait_time_residual," in row]
    assert rows_no_header == [], f"wait_time_residual should not appear: {rows_no_header}"


def test_skipped_manifests_excluded_from_csv(tmp_path, monkeypatch):
    """Skip-manifests written by train.py when anomaly filter empties train/val
    must not appear as all-NaN rows in the summary CSV."""
    import scripts.summarize_walk_forward as swf

    fake_root = tmp_path / "models"
    fake_root.mkdir()
    day_dir = fake_root / "2026-04-15"
    day_dir.mkdir()

    # Two manifests: one normal, one skipped.
    (day_dir / "wait_time_residual_throughput_manifest.json").write_text(_fake_manifest({
        "target": "wait_time",
        "evaluation": {"primary": {
            "aggregate": {"mae_s": 700.0, "within_2x_rate": 0.50, "p90_coverage_rate": 0.88},
            "baseline_aggregate": {"mae_s": 800.0, "within_2x_rate": 0.47},
            "buckets_aggregate": {},
        }},
        "windows": {"holdout": {"rows": 800000}},
    }))
    (day_dir / "wait_time_residual_throughput_filtered_manifest.json").write_text(_fake_manifest({
        "skipped": True,
        "skip_reason": "anomaly filter emptied val for 2026-04-15",
        "target": "wait_time",
        "as_of_date": "2026-04-15T00:00:00+00:00",
    }))

    monkeypatch.setattr(swf, "MODELS_DIR", fake_root)
    out = tmp_path / "out.csv"
    rc = swf.main(["--from", "2026-04-15", "--to", "2026-04-15",
                   "--configs", "*", "--output", str(out)])
    assert rc == 0
    content = out.read_text()
    lines = content.strip().splitlines()
    # 1 header + 1 data row (the skipped manifest is dropped).
    assert len(lines) == 1 + 1, f"unexpected line count: {len(lines)}\n{content}"
    assert "wait_time_residual_throughput_filtered" not in content


def test_configs_filter_wildcard_includes_all(tmp_path, monkeypatch):
    """--configs='*' disables filtering, all configs/targets included in CSV."""
    import scripts.summarize_walk_forward as swf

    fake_root = tmp_path / "models"
    fake_root.mkdir()
    day_dir = fake_root / "2026-04-20"
    day_dir.mkdir()
    for stem, target in [("wait_time", "wait_time"), ("run_duration", "run_duration")]:
        (day_dir / f"{stem}_manifest.json").write_text(json.dumps({
            "target": target,
            "evaluation": {"primary": {
                "aggregate": {"mae_s": 100.0, "within_2x_rate": 0.5, "p90_coverage_rate": 0.9},
                "baseline_aggregate": {"mae_s": 120.0, "within_2x_rate": 0.48},
                "buckets_aggregate": {},
            }},
            "windows": {"holdout": {"rows": 1000}},
        }))

    monkeypatch.setattr(swf, "MODELS_DIR", fake_root)
    out = tmp_path / "out.csv"
    rc = swf.main(["--from", "2026-04-20", "--to", "2026-04-20", "--configs", "*", "--output", str(out)])
    assert rc == 0
    content = out.read_text()
    assert "wait_time" in content and "run_duration" in content


def test_cohort_is_anomalous_column(tmp_path, monkeypatch):
    """The CSV must include cohort_is_anomalous and reflect the daily_health table."""
    import scripts.summarize_walk_forward as swf

    fake_root = tmp_path / "models"
    fake_root.mkdir()
    # cohort 2026-04-25: holdout = [2026-04-20, 2026-04-25) — includes 04-22, 04-23 → flagged
    # cohort 2026-04-21: holdout = [2026-04-16, 2026-04-21) — none of those days flagged
    layout = [
        ("2026-04-25", "wait_time_residual_throughput",
         "2026-04-20T00:00:00+00:00", "2026-04-25T00:00:00+00:00"),
        ("2026-04-21", "wait_time_residual_throughput",
         "2026-04-16T00:00:00+00:00", "2026-04-21T00:00:00+00:00"),
    ]
    for cohort, stem, hold_start, hold_end in layout:
        day_dir = fake_root / cohort
        day_dir.mkdir()
        (day_dir / f"{stem}_manifest.json").write_text(_fake_manifest(
            _manifest_payload("wait_time", 700.0, 0.50, 0.88, hold_start, hold_end)
        ))

    monkeypatch.setattr(swf, "MODELS_DIR", fake_root)
    # Pretend daily_health flagged 2026-04-22 + 2026-04-23.
    monkeypatch.setattr(
        swf, "_load_anomalous_dates_from_db",
        lambda: {"2026-04-22", "2026-04-23"},
    )

    out = tmp_path / "out.csv"
    rc = swf.main(["--from", "2026-04-21", "--to", "2026-04-25", "--output", str(out)])
    assert rc == 0
    text = out.read_text()
    header, *rows = text.strip().splitlines()
    assert "cohort_is_anomalous" in header.split(","), header
    by_cohort = {r.split(",")[0]: r for r in rows}
    assert by_cohort["2026-04-25"].split(",")[-1] == "True"
    assert by_cohort["2026-04-21"].split(",")[-1] == "False"


def test_cohort_is_anomalous_defaults_false_without_db(tmp_path, monkeypatch):
    """When the DB is unavailable, cohort_is_anomalous must default to False
    rather than raise."""
    import scripts.summarize_walk_forward as swf

    fake_root = tmp_path / "models"
    fake_root.mkdir()
    day_dir = fake_root / "2026-04-25"
    day_dir.mkdir()
    (day_dir / "wait_time_manifest.json").write_text(_fake_manifest(
        _manifest_payload("wait_time", 700.0, 0.50, 0.88,
                          "2026-04-20T00:00:00+00:00",
                          "2026-04-25T00:00:00+00:00")
    ))

    monkeypatch.setattr(swf, "MODELS_DIR", fake_root)
    # Force the helper to think no DB is available.
    monkeypatch.setattr(swf, "_load_anomalous_dates_from_db", lambda: set())

    out = tmp_path / "out.csv"
    rc = swf.main(["--from", "2026-04-25", "--to", "2026-04-25", "--output", str(out)])
    assert rc == 0
    rows = out.read_text().strip().splitlines()
    assert len(rows) == 2  # header + one row
    assert rows[1].split(",")[-1] == "False"
