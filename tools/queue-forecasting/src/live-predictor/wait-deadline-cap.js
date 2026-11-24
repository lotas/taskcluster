/**
 * Caps served wait p50/p90 at the time left until the task's deadline.
 *
 * A pending task cannot wait past its deadline: Taskcluster resolves it as
 * `deadline-exceeded` instead, so no completed run ever has
 * wait > deadline - pending_at. Any quantile above that bound is pure
 * over-prediction. In practice this happens when the log-ratio inverse
 * extrapolates on a deep, low-priority queue: on 2026-11-16..18 the shadow
 * model put p90 at 9-11 days for very-low tasks on
 * releng-hardware/gecko-t-osx-1500-m4 (2-3k pending, 24h deadlines). That one
 * queue turned the v3 pinball gain negative.
 *
 * Applied after the p90 guardrail, identically to served and shadow models.
 * No-op when the row has no deadline (collected since 2026-11-16, and not set on
 * playground rows), or when the deadline is not after pending_at (bad data).
 */

function toMs(x) {
  if (x == null) return NaN;
  const ms = x instanceof Date ? x.getTime() : Date.parse(x);
  return Number.isFinite(ms) ? ms : NaN;
}

/** Seconds from pending_at to deadline, or null when unknown / not positive. */
export function secondsToDeadline(row) {
  const s = (toMs(row?.deadline) - toMs(row?.pending_at)) / 1000;
  return Number.isFinite(s) && s > 0 ? s : null;
}

export function applyWaitDeadlineCap({ p50, p90, row }) {
  const capS = secondsToDeadline(row);
  if (capS === null) return { applied: false, capS: null, p50, p90 };
  const cp50 = Math.min(p50, capS);
  const cp90 = Math.min(p90, capS);
  return { applied: cp50 !== p50 || cp90 !== p90, capS, p50: cp50, p90: cp90 };
}
