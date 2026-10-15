-- Shadow wait-model predictions
-- Idempotent: safe to re-run.
-- Run against an existing forecasting DB BEFORE starting a live-predictor with
-- SHADOW_WAIT_STEM set:
--   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d forecasting < migrate-shadow-predictions.sql
--
-- queue_forecast_run_predictions holds ONE row per (task_id, run_id), so a second
-- wait model cannot share it: whichever insert landed first would win and the
-- other would be dropped by ON CONFLICT DO NOTHING. The shadow model writes
-- here instead, with the same key, so the two tables join 1:1 on (task_id, run_id)
-- and nothing that reads the main table sees a difference.
--
-- Drop it to roll back: DROP TABLE queue_forecast_run_predictions_shadow;

BEGIN;

CREATE TABLE IF NOT EXISTS queue_forecast_run_predictions_shadow (
    predicted_at        TIMESTAMPTZ DEFAULT now(),
    wait_p50_s          DOUBLE PRECISION,
    wait_p90_s          DOUBLE PRECISION,
    run_id              INT  NOT NULL,
    task_id             TEXT NOT NULL,
    wait_model_version  TEXT,
    wait_artifact_hash  TEXT,
    input_features      JSONB,

    PRIMARY KEY (task_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_qf_run_predictions_shadow_predicted_at
    ON queue_forecast_run_predictions_shadow (predicted_at);

COMMIT;
