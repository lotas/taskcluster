export function normalizeMetadataName(name) {
  if (!name) return null;
  return name.replace(/@[0-9a-f]{12,}$/i, '').trim();
}

export function pendingBucket(queuePending) {
  if (queuePending == null) return null;
  const n = parseInt(queuePending, 10);
  if (isNaN(n)) return null;
  if (n === 0) return 'empty';
  if (n <= 5) return 'low';
  if (n <= 20) return 'moderate';
  if (n <= 50) return 'busy';
  if (n <= 200) return 'heavy';
  if (n <= 500) return 'very-heavy';
  if (n <= 1500) return 'overloaded';
  return 'saturated';
}

export const PENDING_BUCKET_SQL = `CASE
  WHEN r.queue_pending IS NULL THEN NULL
  WHEN r.queue_pending = 0 THEN 'empty'
  WHEN r.queue_pending <= 5 THEN 'low'
  WHEN r.queue_pending <= 20 THEN 'moderate'
  WHEN r.queue_pending <= 50 THEN 'busy'
  WHEN r.queue_pending <= 200 THEN 'heavy'
  WHEN r.queue_pending <= 500 THEN 'very-heavy'
  WHEN r.queue_pending <= 1500 THEN 'overloaded'
  ELSE 'saturated'
END`;

export function extractImageName(taskDef) {
  const image = taskDef?.payload?.image;
  if (!image) return null;
  if (typeof image === 'string') return image;
  if (typeof image === 'object' && image.namespace) return image.namespace;
  return null;
}
