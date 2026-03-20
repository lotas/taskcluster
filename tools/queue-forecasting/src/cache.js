import QuickLRU from 'quick-lru';

export function createTaskCache(maxSize = 50000) {
  return new QuickLRU({ maxSize });
}
