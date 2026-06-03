/**
 * Floors the served wait-time p90 with the historical queue+priority+bucket
 * baseline p90 when the model's p90 underestimates the long tail. Only this
 * strongest, priority-aware level applies.
 *
 * The priority-blind queue+bucket level is deliberately excluded: in a deep
 * queue, wait time is dominated by priority, so flooring (say) a very-high
 * task to the all-priority pooled p90 over-inflates it by many multiples.
 * The weaker fallback levels (queue, priority+bucket) are likewise too
 * miscalibrated to anchor the served p90.
 */

import { applyP90Guardrail } from './p90-guardrail.js';

const STRONG_BASELINE_LEVELS = ['queue+priority+bucket'];

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
