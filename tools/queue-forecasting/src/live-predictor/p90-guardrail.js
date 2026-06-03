/**
 * Floors a served p90 with the historical baseline p90 when the model's p90
 * underestimates the long tail. Only "strong" baseline levels apply; weaker
 * fallback levels are poorly calibrated and would inject noisy quantiles.
 *
 * Run-duration and wait-time share this logic but differ in which baseline
 * level counts as strong (`metadata_name` vs `queue+bucket`), so the strong
 * levels are passed in by the per-target wrappers.
 */

export function applyP90Guardrail({
  p50,
  rawModelP90,
  baseline,
  minSampleSize,
  strongLevels,
}) {
  const baselineP90 = baseline && Number.isFinite(Number(baseline.p90))
    ? Number(baseline.p90)
    : NaN;
  const baselineLevel = baseline ? baseline.level ?? null : null;
  const baselineSampleSize = baseline && Number.isFinite(Number(baseline.sample_size))
    ? Number(baseline.sample_size)
    : null;

  const eligible =
    baseline !== null &&
    strongLevels.includes(baselineLevel) &&
    Number.isFinite(baselineP90) &&
    baselineSampleSize !== null &&
    baselineSampleSize >= minSampleSize;

  const guardedP90 = eligible ? Math.max(rawModelP90, baselineP90) : rawModelP90;
  const finalP90 = Math.max(p50, guardedP90);

  return {
    applied: eligible,
    rawModelP90,
    baselineP90: Number.isFinite(baselineP90) ? baselineP90 : null,
    baselineLevel,
    baselineSampleSize,
    finalP90,
  };
}
