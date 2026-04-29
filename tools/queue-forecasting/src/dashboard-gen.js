/**
 * Dashboard generator — queries Postgres + reads trainer manifests,
 * writes a static index.html to OUTPUT_DIR every INTERVAL_MS.
 */
import pg from 'pg';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUTPUT_DIR = process.env.DASHBOARD_OUTPUT_DIR || path.join(__dirname, '..', 'dashboard-out');
const MODELS_DIR = path.join(__dirname, '..', 'trainer', 'data', 'models');
const INTERVAL_MS = parseInt(process.env.DASHBOARD_INTERVAL_MS || '900000', 10); // 15 min
const DATABASE_URL = process.env.DATABASE_URL || 'postgresql://postgres@localhost:5433/forecasting';

// ─── DB Queries ──────────────────────────────────────────────────────────────

async function queryTableHealth(pool) {
  const { rows } = await pool.query(`
    SELECT 'queue_forecast_tasks' AS table_name,
           count(*)::bigint AS row_count,
           max(enriched_at) AS newest_ts
      FROM queue_forecast_tasks
    UNION ALL
    SELECT 'queue_forecast_task_runs',
           count(*)::bigint,
           greatest(max(pending_at), max(started_at), max(resolved_at))
      FROM queue_forecast_task_runs
    UNION ALL
    SELECT 'queue_forecast_worker_counts',
           count(*)::bigint,
           max(sampled_at)
      FROM queue_forecast_worker_counts
    UNION ALL
    SELECT 'queue_forecast_daily_health',
           count(*)::bigint,
           max(computed_at)
      FROM queue_forecast_daily_health
    UNION ALL
    SELECT 'queue_forecast_run_predictions',
           count(*)::bigint,
           max(predicted_at)
      FROM queue_forecast_run_predictions
    UNION ALL
    SELECT 'queue_forecast_worker_pools',
           count(*)::bigint,
           max(refreshed_at)
      FROM queue_forecast_worker_pools
    ORDER BY table_name;
  `);
  return rows;
}

async function queryFreshness(pool) {
  const { rows } = await pool.query(`
    SELECT
      now() AS checked_at,
      (SELECT max(pending_at)  FROM queue_forecast_task_runs) AS latest_pending,
      (SELECT max(started_at)  FROM queue_forecast_task_runs) AS latest_started,
      (SELECT max(resolved_at) FROM queue_forecast_task_runs) AS latest_resolved,
      (SELECT max(enriched_at) FROM queue_forecast_tasks)     AS latest_enriched,
      (SELECT max(sampled_at)  FROM queue_forecast_worker_counts) AS latest_worker_sample,
      (SELECT max(computed_at) FROM queue_forecast_daily_health)  AS latest_daily_health;
  `);
  return rows[0];
}

async function queryIngestion(pool) {
  const { rows } = await pool.query(`
    WITH windows(label, span) AS (
      VALUES
        ('5m',  interval '5 minutes'),
        ('15m', interval '15 minutes'),
        ('1h',  interval '1 hour'),
        ('6h',  interval '6 hours'),
        ('24h', interval '24 hours')
    )
    SELECT
      w.label,
      count(*) FILTER (WHERE r.pending_at  >= now() - w.span) AS pending_rows,
      count(*) FILTER (WHERE r.started_at  >= now() - w.span) AS started_rows,
      count(*) FILTER (WHERE r.resolved_at >= now() - w.span) AS resolved_rows
    FROM windows w
    CROSS JOIN queue_forecast_task_runs r
    GROUP BY w.label, w.span
    ORDER BY w.span;
  `);
  return rows;
}

async function queryOpenIssues(pool) {
  const { rows } = await pool.query(`
    SELECT 'unenriched_tasks' AS metric, count(*)::bigint AS value
      FROM queue_forecast_tasks WHERE metadata_name IS NULL
    UNION ALL
    SELECT 'unresolved_runs', count(*)::bigint
      FROM queue_forecast_task_runs WHERE resolved_at IS NULL
    UNION ALL
    SELECT 'unresolved_runs_older_than_2h', count(*)::bigint
      FROM queue_forecast_task_runs
     WHERE resolved_at IS NULL AND pending_at < now() - interval '2 hours'
    UNION ALL
    SELECT 'worker_samples_last_30m', count(DISTINCT sampled_at)::bigint
      FROM queue_forecast_worker_counts WHERE sampled_at >= now() - interval '30 minutes'
    UNION ALL
    SELECT 'daily_health_rows_last_7d', count(*)::bigint
      FROM queue_forecast_daily_health WHERE sample_date >= current_date - 7
    ORDER BY metric;
  `);
  return rows;
}

async function queryDailyHealth(pool) {
  const { rows } = await pool.query(`
    SELECT
      sample_date,
      n_total, n_completed, n_failed, n_exception,
      completion_rate, exception_rate, stuck_pending_rate,
      wait_p99_s, run_p99_s,
      total_capacity_p50, total_running_p50, utilization_p50,
      n_worker_samples,
      flag_exception_spike, flag_stuck_pending_spike,
      flag_wait_p99_spike, flag_volume_anomaly, flag_low_completion,
      flag_capacity_drop, flag_capacity_spike, flag_low_utilization,
      flag_sampler_offline,
      is_anomalous, anomaly_reasons
    FROM queue_forecast_daily_health
    WHERE sample_date >= current_date - 14
    ORDER BY sample_date DESC;
  `);
  return rows;
}

async function queryRecentResolutions(pool) {
  const { rows } = await pool.query(`
    SELECT reason_resolved, count(*) AS n
      FROM queue_forecast_task_runs
     WHERE resolved_at >= now() - interval '1 hour'
     GROUP BY reason_resolved
     ORDER BY n DESC
     LIMIT 10;
  `);
  return rows;
}

// ─── Manifest Loading ────────────────────────────────────────────────────────

function loadLatestManifests() {
  if (!fs.existsSync(MODELS_DIR)) return [];
  const dates = fs.readdirSync(MODELS_DIR)
    .filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
    .sort()
    .reverse();

  // Load manifests from the most recent date directory
  const latest = dates[0];
  if (!latest) return [];

  const dir = path.join(MODELS_DIR, latest);
  const manifests = [];
  for (const f of fs.readdirSync(dir)) {
    if (f.endsWith('_manifest.json')) {
      try {
        const data = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
        data._filename = f;
        data._date_dir = latest;
        manifests.push(data);
      } catch { /* skip broken files */ }
    }
  }
  return manifests;
}

// ─── HTML Generation ─────────────────────────────────────────────────────────

function esc(s) {
  if (s == null) return '—';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtNum(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('en-US');
}

function fmtPct(v) {
  if (v == null || Number.isNaN(Number(v))) return '—';
  return (Number(v) * 100).toFixed(1) + '%';
}

function fmtDuration(s) {
  if (s == null || Number.isNaN(Number(s))) return '—';
  s = Number(s);
  if (s < 60) return s.toFixed(1) + 's';
  if (s < 3600) return (s / 60).toFixed(1) + 'm';
  return (s / 3600).toFixed(1) + 'h';
}

function fmtAge(ts) {
  if (!ts) return '—';
  const ms = Date.now() - new Date(ts).getTime();
  const s = ms / 1000;
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return (s / 3600).toFixed(1) + 'h ago';
  return (s / 86400).toFixed(1) + 'd ago';
}

function healthBadge(ok, warnText = 'WARN', okText = 'OK') {
  if (ok) return `<span class="badge badge-ok">${okText}</span>`;
  return `<span class="badge badge-warn">${warnText}</span>`;
}

function flagDot(val) {
  return val ? '<span class="dot dot-red" title="flagged"></span>' : '<span class="dot dot-dim"></span>';
}

function renderTableHealth(rows) {
  let html = `<table><thead><tr>
    <th>Table</th><th class="r">Rows</th><th>Newest</th><th>Age</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    html += `<tr>
      <td>${esc(r.table_name)}</td>
      <td class="r">${fmtNum(r.row_count)}</td>
      <td class="ts">${r.newest_ts ? new Date(r.newest_ts).toISOString().replace('T', ' ').slice(0, 19) + 'Z' : '—'}</td>
      <td>${fmtAge(r.newest_ts)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderFreshness(f) {
  const items = [
    ['pending_at', f.latest_pending],
    ['started_at', f.latest_started],
    ['resolved_at', f.latest_resolved],
    ['enriched_at', f.latest_enriched],
    ['worker_sample', f.latest_worker_sample],
    ['daily_health', f.latest_daily_health],
  ];
  let html = '<table><thead><tr><th>Stream</th><th>Latest</th><th>Age</th><th></th></tr></thead><tbody>';
  for (const [name, ts] of items) {
    const ageMs = ts ? Date.now() - new Date(ts).getTime() : null;
    const stale = ageMs != null && ageMs > 30 * 60 * 1000;
    html += `<tr>
      <td>${esc(name)}</td>
      <td class="ts">${ts ? new Date(ts).toISOString().replace('T', ' ').slice(0, 19) + 'Z' : '—'}</td>
      <td>${fmtAge(ts)}</td>
      <td>${ts ? healthBadge(!stale) : healthBadge(false, 'NO DATA')}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderIngestion(rows) {
  let html = `<table><thead><tr>
    <th>Window</th><th class="r">Pending</th><th class="r">Started</th><th class="r">Resolved</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    html += `<tr>
      <td>${esc(r.label)}</td>
      <td class="r">${fmtNum(r.pending_rows)}</td>
      <td class="r">${fmtNum(r.started_rows)}</td>
      <td class="r">${fmtNum(r.resolved_rows)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderOpenIssues(rows) {
  let html = '<table><thead><tr><th>Metric</th><th class="r">Value</th></tr></thead><tbody>';
  for (const r of rows) {
    const warn = r.metric === 'unresolved_runs_older_than_2h' && Number(r.value) > 100;
    html += `<tr>
      <td>${esc(r.metric)}</td>
      <td class="r${warn ? ' text-warn' : ''}">${fmtNum(r.value)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderResolutions(rows) {
  let html = '<table><thead><tr><th>Reason</th><th class="r">Count (1h)</th></tr></thead><tbody>';
  for (const r of rows) {
    html += `<tr><td>${esc(r.reason_resolved)}</td><td class="r">${fmtNum(r.n)}</td></tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderManifests(manifests) {
  if (!manifests.length) return '<p class="muted">No training manifests found.</p>';
  let html = '';
  for (const m of manifests) {
    const ev = m.evaluation?.primary || {};
    const agg = ev.aggregate || {};
    const baseAgg = ev.baseline_aggregate || {};
    const lgbOnlyAgg = ev.lightgbm_only_aggregate || {};
    const w = m.windows || {};
    const isResidual = !!lgbOnlyAgg.mae_s;

    html += `<div class="manifest-card">`;
    html += `<h3>${esc(m._filename?.replace('_manifest.json', ''))}</h3>`;
    html += `<div class="meta">trained ${fmtAge(m.trained_at)} · as_of ${esc(w.as_of_date?.slice(0, 10))} · ${esc(m.model_type)}</div>`;

    // Windows summary
    html += `<div class="meta">train: ${fmtNum(w.train?.rows)} rows (${esc(w.train?.start?.slice(0, 10))}..${esc(w.train?.end?.slice(0, 10))}) · val: ${fmtNum(w.val?.rows)} · holdout: ${fmtNum(w.holdout?.rows)}</div>`;

    // Aggregate metrics table
    html += '<table class="metrics"><thead><tr><th></th>';
    if (isResidual) {
      html += '<th class="r">Baseline</th><th class="r">LGB-only</th><th class="r">Residual</th>';
    } else {
      html += '<th class="r">Model</th><th class="r">Baseline</th><th class="r">Delta</th>';
    }
    html += '</tr></thead><tbody>';

    if (isResidual) {
      html += `<tr><td>MAE</td>
        <td class="r">${fmtDuration(baseAgg.mae_s)}</td>
        <td class="r">${fmtDuration(lgbOnlyAgg.mae_s)}</td>
        <td class="r">${fmtDuration(agg.mae_s)}</td></tr>`;
      html += `<tr><td>within-2x</td>
        <td class="r">${fmtPct(baseAgg.within_2x_rate)}</td>
        <td class="r">${fmtPct(lgbOnlyAgg.within_2x_rate)}</td>
        <td class="r">${fmtPct(agg.within_2x_rate)}</td></tr>`;
      if (agg.p90_coverage_rate != null) {
        html += `<tr><td>p90 coverage</td>
          <td class="r">—</td>
          <td class="r">${fmtPct(lgbOnlyAgg.p90_coverage_rate)}</td>
          <td class="r">${fmtPct(agg.p90_coverage_rate)}</td></tr>`;
      }
    } else {
      const maeDelta = (baseAgg.mae_s && agg.mae_s)
        ? ((agg.mae_s - baseAgg.mae_s) / baseAgg.mae_s * 100).toFixed(1) + '%'
        : '—';
      html += `<tr><td>MAE</td>
        <td class="r">${fmtDuration(agg.mae_s)}</td>
        <td class="r">${fmtDuration(baseAgg.mae_s)}</td>
        <td class="r">${esc(maeDelta)}</td></tr>`;
      html += `<tr><td>within-2x</td>
        <td class="r">${fmtPct(agg.within_2x_rate)}</td>
        <td class="r">${fmtPct(baseAgg.within_2x_rate)}</td>
        <td class="r">—</td></tr>`;
      if (agg.p90_coverage_rate != null) {
        html += `<tr><td>p90 coverage</td>
          <td class="r">${fmtPct(agg.p90_coverage_rate)}</td>
          <td class="r">—</td>
          <td class="r">—</td></tr>`;
      }
    }
    html += '</tbody></table>';

    // Per-bucket breakdown
    const buckets = ev.buckets_aggregate;
    const baseBuckets = ev.baseline_buckets_aggregate;
    if (buckets && Object.keys(buckets).length > 0) {
      html += '<table class="metrics"><thead><tr><th>Bucket</th><th class="r">n</th><th class="r">MAE</th><th class="r">Base MAE</th><th class="r">w/in 2x</th><th class="r">Base w/in 2x</th></tr></thead><tbody>';
      for (const name of ['<1m', '1-5m', '5-30m', '30m+']) {
        const b = buckets[name] || {};
        const bb = (baseBuckets || {})[name] || {};
        html += `<tr>
          <td>${esc(name)}</td>
          <td class="r">${fmtNum(b.mae?.eligible_n)}</td>
          <td class="r">${fmtDuration(b.mae_s)}</td>
          <td class="r">${fmtDuration(bb.mae_s)}</td>
          <td class="r">${fmtPct(b.within_2x_rate)}</td>
          <td class="r">${fmtPct(bb.within_2x_rate)}</td>
        </tr>`;
      }
      html += '</tbody></table>';
    }

    html += '</div>';
  }
  return html;
}

function renderDailyHealth(rows) {
  if (!rows.length) return '<p class="muted">No daily health data.</p>';
  let html = `<table class="health-table"><thead><tr>
    <th>Date</th><th class="r">Total</th><th class="r">Comp%</th><th class="r">Exc%</th>
    <th class="r">Wait p99</th><th class="r">Cap p50</th><th class="r">Util</th>
    <th>Exc</th><th>Stuck</th><th>Wait</th><th>Vol</th><th>Comp</th>
    <th>Cap↓</th><th>Cap↑</th><th>Util↓</th><th>Samp</th>
    <th>Anomaly</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    const cls = r.is_anomalous ? ' class="row-anomaly"' : '';
    html += `<tr${cls}>
      <td>${esc(r.sample_date?.toISOString?.() ? r.sample_date.toISOString().slice(0, 10) : r.sample_date)}</td>
      <td class="r">${fmtNum(r.n_total)}</td>
      <td class="r">${fmtPct(r.completion_rate)}</td>
      <td class="r">${fmtPct(r.exception_rate)}</td>
      <td class="r">${fmtDuration(r.wait_p99_s)}</td>
      <td class="r">${fmtNum(r.total_capacity_p50)}</td>
      <td class="r">${fmtPct(r.utilization_p50)}</td>
      <td>${flagDot(r.flag_exception_spike)}</td>
      <td>${flagDot(r.flag_stuck_pending_spike)}</td>
      <td>${flagDot(r.flag_wait_p99_spike)}</td>
      <td>${flagDot(r.flag_volume_anomaly)}</td>
      <td>${flagDot(r.flag_low_completion)}</td>
      <td>${flagDot(r.flag_capacity_drop)}</td>
      <td>${flagDot(r.flag_capacity_spike)}</td>
      <td>${flagDot(r.flag_low_utilization)}</td>
      <td>${flagDot(r.flag_sampler_offline)}</td>
      <td>${r.is_anomalous
        ? `<span class="badge badge-warn" title="${esc(r.anomaly_reasons?.join(', '))}">${esc(r.anomaly_reasons?.length || 0)} flags</span>`
        : '<span class="badge badge-ok">OK</span>'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function buildPage(data) {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Queue Forecasting Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
    --fg: #e6edf3; --fg2: #8b949e; --fg3: #484f58;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --blue: #58a6ff; --border: #30363d;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    font-size: 13px; line-height: 1.5;
    background: var(--bg); color: var(--fg);
    padding: 20px; max-width: 1400px; margin: 0 auto;
  }
  h1 { font-size: 20px; font-weight: 600; margin-bottom: 4px; color: var(--fg); }
  h2 {
    font-size: 14px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--blue); margin: 28px 0 12px; padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }
  h3 { font-size: 13px; font-weight: 600; color: var(--fg); margin-bottom: 4px; }
  .header-meta { color: var(--fg2); font-size: 12px; margin-bottom: 20px; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 16px; }
  th, td { padding: 5px 10px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--fg2); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  td { color: var(--fg); }
  .r { text-align: right; }
  .ts { font-size: 12px; color: var(--fg2); }
  .meta { font-size: 12px; color: var(--fg2); margin-bottom: 8px; }
  .muted { color: var(--fg3); }
  .text-warn { color: var(--yellow); }
  .badge {
    display: inline-block; padding: 1px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600;
  }
  .badge-ok { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-warn { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
  .dot-red { background: var(--red); }
  .dot-dim { background: var(--fg3); opacity: 0.3; }
  .manifest-card {
    background: var(--bg2); border: 1px solid var(--border); border-radius: 6px;
    padding: 14px 16px; margin-bottom: 12px;
  }
  .manifest-card table { margin-top: 8px; }
  .metrics th, .metrics td { padding: 4px 10px; }
  .row-anomaly { background: rgba(248,81,73,0.08); }
  .health-table th, .health-table td { padding: 4px 6px; font-size: 12px; }
  .health-table th { font-size: 10px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  .section { margin-bottom: 12px; }
  footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--border); color: var(--fg3); font-size: 11px; }
</style>
</head>
<body>
<h1>Queue Forecasting Dashboard</h1>
<div class="header-meta">Generated ${now} · refreshes every 15m</div>

<h2>Data Collection Health</h2>
<div class="grid">
  <div class="section">
    <h3>Table Sizes</h3>
    ${data.tableHealth}
  </div>
  <div class="section">
    <h3>Stream Freshness</h3>
    ${data.freshness}
  </div>
</div>
<div class="grid">
  <div class="section">
    <h3>Ingestion Windows</h3>
    ${data.ingestion}
  </div>
  <div class="section">
    <h3>Open Issues</h3>
    ${data.openIssues}
  </div>
</div>
<div class="section">
  <h3>Recent Resolutions (1h)</h3>
  ${data.resolutions}
</div>

<h2>Training Results</h2>
<div class="meta">${data.manifestsDir}</div>
${data.manifests}

<h2>Daily Health (last 14d)</h2>
${data.dailyHealth}

<footer>queue-forecasting dashboard · generated ${now}</footer>
</body>
</html>`;
}

// ─── Main Loop ───────────────────────────────────────────────────────────────

async function generate() {
  const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 3 });
  try {
    const [tableHealth, freshness, ingestion, openIssues, dailyHealth, resolutions] =
      await Promise.all([
        queryTableHealth(pool),
        queryFreshness(pool),
        queryIngestion(pool),
        queryOpenIssues(pool),
        queryDailyHealth(pool),
        queryRecentResolutions(pool),
      ]);

    const manifests = loadLatestManifests();
    const latestDir = manifests.length ? manifests[0]._date_dir : 'none';

    const html = buildPage({
      tableHealth: renderTableHealth(tableHealth),
      freshness: renderFreshness(freshness),
      ingestion: renderIngestion(ingestion),
      openIssues: renderOpenIssues(openIssues),
      resolutions: renderResolutions(resolutions),
      manifests: renderManifests(manifests),
      manifestsDir: `Latest: trainer/data/models/${latestDir}/`,
      dailyHealth: renderDailyHealth(dailyHealth),
    });

    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
    fs.writeFileSync(path.join(OUTPUT_DIR, 'index.html'), html);
    console.log(`[${new Date().toISOString()}] dashboard written to ${OUTPUT_DIR}/index.html`);
  } finally {
    await pool.end();
  }
}

async function loop() {
  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      await generate();
    } catch (err) {
      console.error(`[${new Date().toISOString()}] dashboard generation failed:`, err.message);
    }
    await new Promise(r => setTimeout(r, INTERVAL_MS));
  }
}

// If --once flag, run once and exit. Otherwise loop.
if (process.argv.includes('--once')) {
  generate().catch(err => { console.error(err); process.exit(1); });
} else {
  loop();
}
