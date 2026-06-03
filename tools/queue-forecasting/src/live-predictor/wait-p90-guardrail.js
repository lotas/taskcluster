/**
 * Floors the served wait-time p90 with the historical queue+bucket baseline
 * p90 when the model's p90 underestimates the long tail. Only the strong
 * queue+bucket level applies; weaker fallback levels (queue, priority+bucket)
 * are badly miscalibrated on live data and must not anchor the served p90.
 */

import { applyP90Guardrail } from './p90-guardrail.js';

const STRONG_BASELINE_LEVELS = ['queue+bucket'];

export function applyWaitP90Guardrail({
  waitP50,
  rawModelP90,
  baseline,
  minSampleSize,
}) {
  return applyP90Guardrail({
    p50: waitP50,
    rawModelP90,
    baseline,
    minSampleSize,
    strongLevels: STRONG_BASELINE_LEVELS,
  });
}
