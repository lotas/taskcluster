export function normalizeMetadataName(name) {
  if (!name) return null;
  return name.replace(/@[0-9a-f]{12,}$/i, '').trim();
}

export function extractImageName(taskDef) {
  const image = taskDef?.payload?.image;
  if (!image) return null;
  if (typeof image === 'string') return image;
  if (typeof image === 'object' && image.namespace) return image.namespace;
  return null;
}
