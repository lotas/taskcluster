/**
 * Floors the served run-duration p90 with the historical exact-name baseline
 * p90 when the model's p90 underestimates the long tail. Only the strong
 * metadata_name level applies; weaker fallback levels are poorly calibrated.
 */

const STRONG_BASELINE_LEVEL = 'metadata_name';

export function applyDurationP90Guardrail({
  runP50,
  rawModelP90,
  baseline,
  minSampleSize,
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
    baselineLevel === STRONG_BASELINE_LEVEL &&
    Number.isFinite(baselineP90) &&
    baselineSampleSize !== null &&
    baselineSampleSize >= minSampleSize;

  const guardedP90 = eligible ? Math.max(rawModelP90, baselineP90) : rawModelP90;
  const finalP90 = Math.max(runP50, guardedP90);

  return {
    applied: eligible,
    rawModelP90,
    baselineP90: Number.isFinite(baselineP90) ? baselineP90 : null,
    baselineLevel,
    baselineSampleSize,
    finalP90,
  };
}
