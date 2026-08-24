// smoke.js calls resetTables(), which issues unqualified DELETEs against
// queue_forecast_tasks and queue_forecast_task_runs. It must therefore only
// ever run against a throwaway database. This guard is the enforcement, and
// is deliberately separate from smoke.js so it can be unit-tested with no DB.

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

// The host port mapping for the production postgres container.
const PRODUCTION_HOST_PORT = '5433';

// A disposable database must say so in its name.
const DISPOSABLE_NAME = /(^|[_-])(smoke|test)([_-]|$)/;

export function assertDisposableDatabaseUrl(url) {
  if (!url) {
    throw new Error(
      'SMOKE_DATABASE_URL is required. smoke.js deletes all task rows and has no default; ' +
      'point it at a throwaway database, e.g. postgresql://postgres@localhost:5544/forecasting_smoke',
    );
  }

  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`SMOKE_DATABASE_URL could not be parsed as a URL: ${url}`);
  }

  if (parsed.port === PRODUCTION_HOST_PORT) {
    throw new Error(
      `SMOKE_DATABASE_URL uses port ${PRODUCTION_HOST_PORT}, which is the production postgres ` +
      'host mapping. Refusing to run.',
    );
  }

  if (!LOCAL_HOSTS.has(parsed.hostname)) {
    throw new Error(
      `SMOKE_DATABASE_URL host must be local, got "${parsed.hostname}". Refusing to run.`,
    );
  }

  const dbName = parsed.pathname.replace(/^\//, '');
  if (!DISPOSABLE_NAME.test(dbName)) {
    throw new Error(
      `SMOKE_DATABASE_URL database name "${dbName}" is not marked disposable. ` +
      'Use a name containing "smoke" or "test", e.g. forecasting_smoke.',
    );
  }

  return url;
}
