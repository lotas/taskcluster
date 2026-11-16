-- Shadow task-group completion estimates (group-eta-plan.md P2). Internal only.
-- Idempotent. Apply before enabling scripts/group_eta_live.sh:
--   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d forecasting < migrate-group-predictions-shadow.sql
--
-- One row per (group, estimator), written once at t_pred = decision task resolved
-- + 120s. Estimates are seconds from t0 (= earliest task_created in the group);
-- predicted completion = t0 + p50_s / p90_s. Scored by scripts/weekly-report.sql.
-- Trimmed by the retention service on predicted_at.
--
-- Roll back: DROP TABLE queue_forecast_group_predictions_shadow;

BEGIN;

CREATE TABLE IF NOT EXISTS queue_forecast_group_predictions_shadow (
    predicted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    t0               TIMESTAMPTZ NOT NULL,
    t_pred           TIMESTAMPTZ NOT NULL,
    p50_s            DOUBLE PRECISION,
    p90_s            DOUBLE PRECISION,
    n_known          INTEGER NOT NULL,
    task_group_id    TEXT NOT NULL,
    estimator        TEXT NOT NULL,
    repo_family      TEXT,
    model_version    TEXT,
    PRIMARY KEY (task_group_id, estimator)
);

CREATE INDEX IF NOT EXISTS idx_qf_group_predictions_shadow_predicted_at
    ON queue_forecast_group_predictions_shadow (predicted_at);

COMMIT;
