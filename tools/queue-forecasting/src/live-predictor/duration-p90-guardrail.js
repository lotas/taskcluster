/**
 * Floors the served run-duration p90 with the historical exact-name baseline
 * p90 when the model's p90 underestimates the long tail. Only the strong
 * metadata_name level applies; weaker fallback levels are poorly calibrated.
 */

import { applyP90Guardrail } from './p90-guardrail.js';

const STRONG_BASELINE_LEVELS = ['metadata_name'];

export function applyDurationP90Guardrail({
  runP50,
  rawModelP90,
  baseline,
  minSampleSize,
}) {
  return applyP90Guardrail({
    p50: runP50,
    rawModelP90,
    baseline,
    minSampleSize,
    strongLevels: STRONG_BASELINE_LEVELS,
  });
}
