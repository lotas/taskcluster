/**
 * Dashboard generator — queries Postgres + reads trainer manifests,
 * writes a static index.html + status.html to OUTPUT_DIR every INTERVAL_MS.
 */
import pg from 'pg';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.join(__dirname, '..');
const OUTPUT_DIR = process.env.DASHBOARD_OUTPUT_DIR || path.join(PROJECT_ROOT, 'dashboard-out');
const MODELS_DIR = path.join(PROJECT_ROOT, 'trainer', 'data', 'models');
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
    WHERE sample_date >= current_date - 30
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

async function queryTodayHourly(pool) {
  const { rows } = await pool.query(`
    WITH hours AS (
      SELECT generate_series(
        date_trunc('day', now() AT TIME ZONE 'UTC'),
        date_trunc('hour', now() AT TIME ZONE 'UTC'),
        interval '1 hour'
      ) AS hour_start
    )
    SELECT
      to_char(h.hour_start AT TIME ZONE 'UTC', 'HH24:00') AS hour,
      count(*) FILTER (WHERE r.pending_at  >= h.hour_start AND r.pending_at  < h.hour_start + interval '1 hour') AS pending,
      count(*) FILTER (WHERE r.started_at  >= h.hour_start AND r.started_at  < h.hour_start + interval '1 hour') AS started,
      count(*) FILTER (WHERE r.resolved_at >= h.hour_start AND r.resolved_at < h.hour_start + interval '1 hour') AS resolved,
      count(*) FILTER (WHERE r.resolved_at >= h.hour_start AND r.resolved_at < h.hour_start + interval '1 hour'
                        AND r.reason_resolved = 'completed') AS completed,
      count(*) FILTER (WHERE r.resolved_at >= h.hour_start AND r.resolved_at < h.hour_start + interval '1 hour'
                        AND r.reason_resolved = 'failed') AS failed
    FROM hours h
    CROSS JOIN queue_forecast_task_runs r
    WHERE r.pending_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
       OR r.started_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
       OR r.resolved_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
    GROUP BY h.hour_start
    ORDER BY h.hour_start;
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

// ─── Markdown to HTML (minimal, no deps) ─────────────────────────────────────

function markdownToHtml(md) {
  let html = '';
  const lines = md.split('\n');
  let inCode = false;
  let inTable = false;
  let inList = false;
  let tableRows = [];

  function flushTable() {
    if (!tableRows.length) return;
    html += '<table><thead><tr>';
    const headers = tableRows[0];
    for (const h of headers) html += `<th>${inlineFormat(h.trim())}</th>`;
    html += '</tr></thead><tbody>';
    for (let i = 2; i < tableRows.length; i++) {
      html += '<tr>';
      for (const c of tableRows[i]) html += `<td>${inlineFormat(c.trim())}</td>`;
      html += '</tr>';
    }
    html += '</tbody></table>\n';
    tableRows = [];
    inTable = false;
  }

  function flushList() {
    if (inList) { html += '</ul>\n'; inList = false; }
  }

  function inlineFormat(text) {
    return text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1'); // strip links
  }

  for (const line of lines) {
    // Code blocks
    if (line.startsWith('```')) {
      if (inTable) flushTable();
      if (inList) flushList();
      if (inCode) { html += '</code></pre>\n'; inCode = false; }
      else { html += '<pre><code>'; inCode = true; }
      continue;
    }
    if (inCode) {
      html += line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') + '\n';
      continue;
    }

    // Table rows
    if (line.includes('|') && line.trim().startsWith('|')) {
      if (inList) flushList();
      const cells = line.split('|').slice(1, -1);
      if (cells.every(c => /^[\s:-]+$/.test(c))) {
        // separator row
        tableRows.push(cells);
        inTable = true;
        continue;
      }
      tableRows.push(cells);
      inTable = true;
      continue;
    } else if (inTable) {
      flushTable();
    }

    // Empty line
    if (!line.trim()) {
      if (inList) flushList();
      continue;
    }

    // Headers
    const hMatch = line.match(/^(#{1,6})\s+(.+)/);
    if (hMatch) {
      if (inList) flushList();
      const level = hMatch[1].length;
      html += `<h${level}>${inlineFormat(hMatch[2])}</h${level}>\n`;
      continue;
    }

    // List items
    if (/^\s*[-*]\s/.test(line) || /^\s*\d+\.\s/.test(line)) {
      if (!inList) { html += '<ul>\n'; inList = true; }
      const text = line.replace(/^\s*[-*]\s+/, '').replace(/^\s*\d+\.\s+/, '');
      html += `<li>${inlineFormat(text)}</li>\n`;
      continue;
    }

    // Blockquote
    if (line.startsWith('>')) {
      if (inList) flushList();
      html += `<blockquote>${inlineFormat(line.slice(1).trim())}</blockquote>\n`;
      continue;
    }

    // Horizontal rule
    if (/^---+$/.test(line.trim())) {
      if (inList) flushList();
      html += '<hr>\n';
      continue;
    }

    // Paragraph
    if (inList) flushList();
    html += `<p>${inlineFormat(line)}</p>\n`;
  }

  if (inCode) html += '</code></pre>\n';
  if (inTable) flushTable();
  if (inList) flushList();

  return html;
}

// ─── HTML Helpers ────────────────────────────────────────────────────────────

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

// ─── Section Renderers ───────────────────────────────────────────────────────

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
  const thresholds = {
    pending_at: 30 * 60 * 1000,
    started_at: 30 * 60 * 1000,
    resolved_at: 30 * 60 * 1000,
    enriched_at: 30 * 60 * 1000,
    worker_sample: 30 * 60 * 1000,
    daily_health: 2 * 60 * 60 * 1000,
  };
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
    const stale = ageMs != null && ageMs > (thresholds[name] || 30 * 60 * 1000);
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

function renderTodayHourly(rows) {
  if (!rows.length) return '<p class="muted">No data for today yet.</p>';
  // Totals row
  let totPending = 0, totStarted = 0, totResolved = 0, totCompleted = 0, totFailed = 0;
  for (const r of rows) {
    totPending += Number(r.pending); totStarted += Number(r.started);
    totResolved += Number(r.resolved); totCompleted += Number(r.completed); totFailed += Number(r.failed);
  }

  let html = `<div class="today-summary">
    <span class="today-stat"><strong>${fmtNum(totPending)}</strong> pending</span>
    <span class="today-stat"><strong>${fmtNum(totResolved)}</strong> resolved</span>
    <span class="today-stat"><strong>${fmtNum(totCompleted)}</strong> completed</span>
    <span class="today-stat"><strong>${fmtNum(totFailed)}</strong> failed</span>
  </div>`;

  html += `<table><thead><tr>
    <th>Hour (UTC)</th><th class="r">Pending</th><th class="r">Started</th>
    <th class="r">Resolved</th><th class="r">Completed</th><th class="r">Failed</th>
    <th>Activity</th>
  </tr></thead><tbody>`;
  const maxPending = Math.max(...rows.map(r => Number(r.pending)), 1);
  for (const r of rows) {
    const pct = (Number(r.pending) / maxPending * 100).toFixed(0);
    html += `<tr>
      <td>${esc(r.hour)}</td>
      <td class="r">${fmtNum(r.pending)}</td>
      <td class="r">${fmtNum(r.started)}</td>
      <td class="r">${fmtNum(r.resolved)}</td>
      <td class="r">${fmtNum(r.completed)}</td>
      <td class="r">${fmtNum(r.failed)}</td>
      <td><div class="bar" style="width:${pct}%"></div></td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderManifestsTabs(manifests) {
  if (!manifests.length) return '<p class="muted">No training manifests found.</p>';

  // Tab buttons
  let html = '<div class="tabs">';
  manifests.forEach((m, i) => {
    const name = m._filename?.replace('_manifest.json', '') || `model-${i}`;
    const active = i === 0 ? ' active' : '';
    html += `<button class="tab-btn${active}" onclick="switchTab('manifest', ${i}, this)">${esc(name)}</button>`;
  });
  html += '</div>';

  // Tab panels
  manifests.forEach((m, i) => {
    const ev = m.evaluation?.primary || {};
    const agg = ev.aggregate || {};
    const baseAgg = ev.baseline_aggregate || {};
    const lgbOnlyAgg = ev.lightgbm_only_aggregate || {};
    const w = m.windows || {};
    const isResidual = !!lgbOnlyAgg.mae_s;
    const display = i === 0 ? 'block' : 'none';

    html += `<div class="tab-panel manifest-panel" id="manifest-${i}" style="display:${display}">`;
    html += `<div class="manifest-card">`;
    html += `<div class="meta">trained ${fmtAge(m.trained_at)} · as_of ${esc(w.as_of_date?.slice(0, 10))} · ${esc(m.model_type)}</div>`;
    html += `<div class="meta">train: ${fmtNum(w.train?.rows)} rows (${esc(w.train?.start?.slice(0, 10))}..${esc(w.train?.end?.slice(0, 10))}) · val: ${fmtNum(w.val?.rows)} · holdout: ${fmtNum(w.holdout?.rows)}</div>`;

    // Aggregate metrics
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

    // Per-day holdout breakdown
    const perDay = ev.per_day;
    if (perDay && Object.keys(perDay).length > 0) {
      html += '<details><summary class="meta" style="cursor:pointer;margin-top:8px">Per-day holdout breakdown</summary>';
      html += '<table class="metrics"><thead><tr><th>Day</th><th class="r">n</th><th class="r">MAE</th><th class="r">w/in 2x</th><th class="r">p90 cov</th></tr></thead><tbody>';
      for (const [day, d] of Object.entries(perDay).sort()) {
        const n = d.mae?.eligible_n || 0;
        const mae = n ? d.mae.sum_abs_error / n : NaN;
        const w2x = d.within_2x?.eligible_n ? d.within_2x.hit_n / d.within_2x.eligible_n : NaN;
        const p90 = d.p90_coverage?.eligible_n ? d.p90_coverage.covered_n / d.p90_coverage.eligible_n : NaN;
        html += `<tr>
          <td>${esc(day)}</td>
          <td class="r">${fmtNum(n)}</td>
          <td class="r">${fmtDuration(mae)}</td>
          <td class="r">${fmtPct(w2x)}</td>
          <td class="r">${fmtPct(p90)}</td>
        </tr>`;
      }
      html += '</tbody></table></details>';
    }

    html += '</div></div>';
  });
  return html;
}

function renderDailyHealth(rows) {
  if (!rows.length) return '<p class="muted">No daily health data.</p>';
  let html = `<table class="health-table"><thead><tr>
    <th>Date</th><th class="r">Total</th><th class="r">Comp%</th><th class="r">Exc%</th>
    <th class="r">Wait p99</th><th class="r">Cap p50</th><th class="r">Util</th>
    <th>Exc</th><th>Stuck</th><th>Wait</th><th>Vol</th><th>Comp</th>
    <th>Cap&darr;</th><th>Cap&uarr;</th><th>Util&darr;</th><th>Samp</th>
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
        ? `<span class="badge badge-warn" title="${esc(r.anomaly_reasons?.join(', '))}">${r.anomaly_reasons?.length || 0} flag${(r.anomaly_reasons?.length || 0) === 1 ? '' : 's'}</span>`
        : '<span class="badge badge-ok">OK</span>'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

// ─── CSS ─────────────────────────────────────────────────────────────────────

const CSS = `
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
  .header-meta a { color: var(--blue); text-decoration: none; }
  .header-meta a:hover { text-decoration: underline; }
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
  /* Tabs */
  .tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
  .tab-btn {
    background: var(--bg3); border: 1px solid var(--border); color: var(--fg2);
    padding: 4px 12px; border-radius: 6px 6px 0 0; cursor: pointer;
    font-family: inherit; font-size: 12px; border-bottom: none;
  }
  .tab-btn:hover { color: var(--fg); background: var(--bg2); }
  .tab-btn.active { background: var(--bg2); color: var(--blue); border-color: var(--blue); border-bottom: 1px solid var(--bg2); }
  .tab-panel { display: none; }
  /* Today */
  .today-summary { display: flex; gap: 24px; margin-bottom: 12px; flex-wrap: wrap; }
  .today-stat { color: var(--fg2); font-size: 13px; }
  .today-stat strong { color: var(--fg); font-size: 16px; }
  .bar { height: 12px; background: var(--blue); border-radius: 2px; opacity: 0.6; min-width: 1px; }
`;

const TAB_JS = `
function switchTab(group, idx, btn) {
  document.querySelectorAll('.' + group + '-panel').forEach(p => p.style.display = 'none');
  document.getElementById(group + '-' + idx).style.display = 'block';
  btn.parentElement.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}
`;

// ─── Page Builders ───────────────────────────────────────────────────────────

function buildPage(data) {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Queue Forecasting Dashboard</title>
<style>${CSS}</style>
</head>
<body>
<h1>Queue Forecasting Dashboard</h1>
<div class="header-meta">Generated ${now} · refreshes every 15m · <a href="status.html">Project Status</a></div>

<h2>Today (UTC)</h2>
${data.todayHourly}

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

<h2>Daily Health (last 30d)</h2>
${data.dailyHealth}

<footer>queue-forecasting dashboard · generated ${now}</footer>
<script>${TAB_JS}</script>
</body>
</html>`;
}

function buildStatusPage(mdContent) {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  const body = markdownToHtml(mdContent);
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Project Status — Queue Forecasting</title>
<style>
${CSS}
  /* Markdown overrides */
  .md-body h1 { font-size: 22px; margin: 24px 0 8px; color: var(--fg); }
  .md-body h2 { font-size: 16px; margin: 24px 0 8px; text-transform: none; letter-spacing: normal; }
  .md-body h3 { font-size: 14px; margin: 20px 0 6px; }
  .md-body h4 { font-size: 13px; margin: 16px 0 4px; color: var(--blue); }
  .md-body h5, .md-body h6 { font-size: 12px; margin: 12px 0 4px; color: var(--fg2); }
  .md-body p { margin: 6px 0; }
  .md-body ul { margin: 6px 0 6px 20px; }
  .md-body li { margin: 2px 0; }
  .md-body table { margin: 8px 0 16px; }
  .md-body pre {
    background: var(--bg3); border: 1px solid var(--border); border-radius: 4px;
    padding: 10px; overflow-x: auto; margin: 8px 0;
  }
  .md-body code { background: var(--bg3); padding: 1px 4px; border-radius: 3px; font-size: 12px; }
  .md-body pre code { background: none; padding: 0; }
  .md-body blockquote {
    border-left: 3px solid var(--border); padding: 4px 12px; margin: 8px 0;
    color: var(--fg2); font-style: italic;
  }
  .md-body hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
  .md-body strong { color: var(--fg); }
</style>
</head>
<body>
<h1><a href="/" style="color:var(--fg);text-decoration:none">&larr;</a> Project Status</h1>
<div class="header-meta">Rendered from trainer-phase2-decision.md · generated ${now} · <a href="/">Back to Dashboard</a></div>
<div class="md-body">
${body}
</div>
<footer>queue-forecasting · generated ${now}</footer>
</body>
</html>`;
}

// ─── Main Loop ───────────────────────────────────────────────────────────────

async function generate() {
  const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 3 });
  try {
    const [tableHealth, freshness, ingestion, openIssues, dailyHealth, resolutions, todayHourly] =
      await Promise.all([
        queryTableHealth(pool),
        queryFreshness(pool),
        queryIngestion(pool),
        queryOpenIssues(pool),
        queryDailyHealth(pool),
        queryRecentResolutions(pool),
        queryTodayHourly(pool),
      ]);

    const manifests = loadLatestManifests();
    const latestDir = manifests.length ? manifests[0]._date_dir : 'none';

    const html = buildPage({
      tableHealth: renderTableHealth(tableHealth),
      freshness: renderFreshness(freshness),
      ingestion: renderIngestion(ingestion),
      openIssues: renderOpenIssues(openIssues),
      resolutions: renderResolutions(resolutions),
      manifests: renderManifestsTabs(manifests),
      manifestsDir: `Latest: trainer/data/models/${latestDir}/`,
      dailyHealth: renderDailyHealth(dailyHealth),
      todayHourly: renderTodayHourly(todayHourly),
    });

    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
    fs.writeFileSync(path.join(OUTPUT_DIR, 'index.html'), html);

    // Status page from markdown
    const mdPath = path.join(PROJECT_ROOT, 'trainer-phase2-decision.md');
    if (fs.existsSync(mdPath)) {
      const md = fs.readFileSync(mdPath, 'utf8');
      fs.writeFileSync(path.join(OUTPUT_DIR, 'status.html'), buildStatusPage(md));
    }

    console.log(`[${new Date().toISOString()}] dashboard written to ${OUTPUT_DIR}/`);
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

if (process.argv.includes('--once')) {
  generate().catch(err => { console.error(err); process.exit(1); });
} else {
  loop();
}
