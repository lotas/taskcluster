/**
 * Per-task prediction orchestrator.
 *
 * Pipeline (for one (task_id, run_id)):
 *   1. Wait for enrichment (with timeout) so the duration model has
 *      metadata_name / normalized_name / tags.
 *   2. Fetch the joined task + task_run row from Postgres.
 *   3. Compute live features:
 *        - baseline_wait, baseline_duration via hierarchical BaselineStats
 *        - throughput via per-call SQL query
 *   4. For each bundle:
 *        a. Build feature vector (Float32Array, ordered by feature_order).
 *        b. Run p50 + p90 ONNX inference.
 *        c. Apply residual inverse (log_ratio: exp(raw) * (baseline + 1) - 1).
 *   5. INSERT into queue_forecast_run_predictions ON CONFLICT DO NOTHING.
 *
 * Rows that have already started/resolved by the time we get here are STILL
 * predicted — the prediction is anchored at pending_at for NOTIFY-triggered
 * rows and "current approximation at scoring time" for catch-up rows (see
 * the anchoring caveat in baseline-stats.js).
 */

import * as ort from 'onnxruntime-node';
import { buildFeatureVector } from './feature-builder.js';
import { applyDurationP90Guardrail } from './duration-p90-guardrail.js';
import { applyWaitP90Guardrail } from './wait-p90-guardrail.js';
import { getThroughput } from './throughput.js';
import { getQueueContext, QUEUE_CONTEXT_FEATURE_VERSION } from './queue-context.js';

// Minimum exact-name baseline sample size required before the historical p90
// is allowed to floor the served run-duration p90. Keeps high-variance,
// sparsely-observed cohorts from anchoring predictions to noisy quantiles.
const DURATION_P90_GUARDRAIL_MIN_SAMPLE = 20;

// Same idea for wait time, gated on the strong queue+bucket baseline level.
const WAIT_P90_GUARDRAIL_MIN_SAMPLE = 20;

const FETCH_ROW_SQL = `
SELECT r.task_id, r.run_id, r.pending_at, r.queue_pending,
       r.priority_at_pending, r.started_at, r.resolved_at,
       t.task_queue_id, t.scheduler_id, t.metadata_name,
       t.normalized_name, t.max_run_time_s, t.tags, t.enriched_at,
       t.repo_family
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.task_id = $1 AND r.run_id = $2
;`;

const INSERT_PREDICTION_SQL = `
INSERT INTO queue_forecast_run_predictions (
  task_id, run_id,
  wait_p50_s, wait_p90_s, run_p50_s, run_p90_s,
  wait_model_version, wait_artifact_hash,
  duration_model_version, duration_artifact_hash,
  input_features
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (task_id, run_id) DO NOTHING
;`;

const ENRICHMENT_POLL_INTERVAL_MS = 200;
const ENRICHMENT_POLL_TIMEOUT_MS  = 5000;

function logRatioInverse(raw, baseline) {
  // Treat NaN baseline as 0 to keep the inverse well-defined; the audit
  // payload still records the raw NaN so we can see this happened.
  const bl = Number.isFinite(baseline) ? baseline : 0.0;
  return Math.exp(raw) * (bl + 1.0) - 1.0;
}

async function runQuantile(session, tensor) {
  const out = await session.run({ [session.inputNames[0]]: tensor });
  return out[session.outputNames[0]].data[0];
}

/** Poll until queue_forecast_tasks.enriched_at IS NOT NULL or timeout. */
async function waitForEnrichment(pool, taskId, { timeoutMs = ENRICHMENT_POLL_TIMEOUT_MS } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const r = await pool.query(
      'SELECT enriched_at FROM queue_forecast_tasks WHERE task_id = $1', [taskId]);
    if (r.rows[0]?.enriched_at) return true;
    await new Promise((res) => setTimeout(res, ENRICHMENT_POLL_INTERVAL_MS));
  }
  return false;
}

/**
 * @param {object} args
 * @param {pg.Pool} args.pool
 * @param {{wait: Bundle, duration: Bundle}} args.bundles
 * @param {BaselineStats} args.baselineStats
 * @param {string} args.taskId
 * @param {number} args.runId
 */
export async function predictAndStore({ pool, bundles, baselineStats, taskId, runId }) {
  // Wait for enrichment so the duration model has metadata_name / tags.
  // The result (true/false) is captured for the audit payload — we predict
  // either way, but cold-start predictions are flagged so we can audit them.
  const enriched = await waitForEnrichment(pool, taskId);

  const rowRes = await pool.query(FETCH_ROW_SQL, [taskId, runId]);
  if (rowRes.rowCount === 0) {
    return { inserted: false, reason: 'row-missing' };
  }
  const row = rowRes.rows[0];

  // Note: we deliberately DO NOT bail when row.started_at or row.resolved_at
  // is set. Predictions fire for every newly-pending task regardless of
  // subsequent state transitions, so the prediction log isn't biased toward
  // long-wait tasks. Scoring uses now()-anchored stats (BaselineStats),
  // which is a "current approximation" for catch-up rows — see the
  // anchoring caveat in baseline-stats.js.

  const waitBaseline     = baselineStats.predictWait(row);
  const durationBaseline = baselineStats.predictDuration(row);

  const throughput = await getThroughput(pool, row.task_queue_id, row.pending_at, taskId, runId);

  const queueContext = await getQueueContext(pool, row);

  // liveFeatures supplies every column not on `row` directly: baseline
  // values + throughput aggregates. Baseline-feature names match the
  // training config: bl_wait_p50 / bl_duration_p50 (used by residual inverse).
  const liveFeatures = {
    bl_wait_p50:     waitBaseline     ? Number(waitBaseline.p50)     : NaN,
    bl_wait_p90:     waitBaseline     ? Number(waitBaseline.p90)     : NaN,
    bl_duration_p50: durationBaseline ? Number(durationBaseline.p50) : NaN,
    bl_duration_p90: durationBaseline ? Number(durationBaseline.p90) : NaN,
    ...throughput,
    ...queueContext,
  };

  // ── Wait model ───────────────────────────────────────────────────────────
  const waitVec = buildFeatureVector(row, liveFeatures, bundles.wait.schema, bundles.wait.categories);
  const waitTensor = new ort.Tensor('float32', waitVec, [1, waitVec.length]);
  const waitRawP50 = await runQuantile(bundles.wait.sessionP50, waitTensor);
  const waitRawP90 = await runQuantile(bundles.wait.sessionP90, waitTensor);
  const waitBl = liveFeatures[bundles.wait.schema.residual.baseline_feature];
  const waitP50 = Math.max(0.0, logRatioInverse(waitRawP50, waitBl));
  const rawModelWaitP90 = Math.max(waitP50, logRatioInverse(waitRawP90, waitBl));

  // Floor the served wait p90 with the queue+bucket historical baseline p90
  // when the model compresses the tail; only the strong queue+bucket level
  // with enough samples qualifies (weak fallbacks are badly miscalibrated).
  const waitGuardrail = applyWaitP90Guardrail({
    waitP50,
    rawModelP90: rawModelWaitP90,
    baseline: waitBaseline,
    minSampleSize: WAIT_P90_GUARDRAIL_MIN_SAMPLE,
  });
  const waitP90 = waitGuardrail.finalP90;

  // ── Duration model ───────────────────────────────────────────────────────
  const durVec = buildFeatureVector(row, liveFeatures, bundles.duration.schema, bundles.duration.categories);
  const durTensor = new ort.Tensor('float32', durVec, [1, durVec.length]);
  const durRawP50 = await runQuantile(bundles.duration.sessionP50, durTensor);
  const durRawP90 = await runQuantile(bundles.duration.sessionP90, durTensor);
  const durBl = liveFeatures[bundles.duration.schema.residual.baseline_feature];
  const runP50 = Math.max(0.0, logRatioInverse(durRawP50, durBl));
  const rawModelRunP90 = Math.max(runP50, logRatioInverse(durRawP90, durBl));

  // Floor the served p90 with the exact-name historical baseline p90 when the
  // model compresses the tail; only metadata_name with enough samples qualifies.
  const guardrail = applyDurationP90Guardrail({
    runP50,
    rawModelP90: rawModelRunP90,
    baseline: durationBaseline,
    minSampleSize: DURATION_P90_GUARDRAIL_MIN_SAMPLE,
  });
  const runP90 = guardrail.finalP90;

  // Audit payload — full enough to reproduce a prediction offline.
  // JSON.stringify drops NaN to null; that's fine for audit.
  const inputFeaturesAudit = {
    enriched_at_predict: enriched,
    row_state: { started_at: row.started_at, resolved_at: row.resolved_at },
    baselines: {
      wait:     waitBaseline,     // {level, p50, p90, sample_size} or null
      duration: durationBaseline,
    },
    throughput,
    queue_context_at_pending: {
      feature_version: QUEUE_CONTEXT_FEATURE_VERSION,
      ...queueContext,
    },
    duration_p90_guardrail: {
      applied:              guardrail.applied,
      raw_model_p90_s:      rawModelRunP90,
      baseline_p90_s:       guardrail.baselineP90,
      final_p90_s:          guardrail.finalP90,
      baseline_level:       guardrail.baselineLevel,
      baseline_sample_size: guardrail.baselineSampleSize,
    },
    wait_p90_guardrail: {
      applied:              waitGuardrail.applied,
      raw_model_p90_s:      rawModelWaitP90,
      baseline_p90_s:       waitGuardrail.baselineP90,
      final_p90_s:          waitGuardrail.finalP90,
      baseline_level:       waitGuardrail.baselineLevel,
      baseline_sample_size: waitGuardrail.baselineSampleSize,
    },
    wait_feature_order:     bundles.wait.schema.feature_order,
    wait_vector:            Array.from(waitVec, (x) => (Number.isFinite(x) ? x : null)),
    duration_feature_order: bundles.duration.schema.feature_order,
    duration_vector:        Array.from(durVec, (x) => (Number.isFinite(x) ? x : null)),
  };

  const ins = await pool.query(INSERT_PREDICTION_SQL, [
    taskId, runId,
    waitP50, waitP90, runP50, runP90,
    bundles.wait.schema.model_version,     bundles.wait.artifact_hash,
    bundles.duration.schema.model_version, bundles.duration.artifact_hash,
    JSON.stringify(inputFeaturesAudit),
  ]);

  return {
    inserted: ins.rowCount === 1,
    reason: ins.rowCount === 1 ? 'ok' : 'duplicate',
    enriched,
  };
}
