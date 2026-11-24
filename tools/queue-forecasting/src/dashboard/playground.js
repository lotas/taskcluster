// Playground page script (served as /playground.js; copied by dashboard-gen).
//
// Security rules for this file: every value from the API or the URL is
// rendered with textContent / createTextNode, never innerHTML; ids are checked
// against the slugid pattern before they are used in a URL; links are only
// built from validated ids. The page is served with a CSP that allows
// script-src 'self' only, so no inline script or handler would run anyway.
(function () {
  'use strict';

  // Taskcluster's v4 slugid pattern (same as TC_SLUGID_RE in src/playground.js).
  var SLUGID = /^[A-Za-z0-9_-]{8}[Q-T][A-Za-z0-9_-][CGKOSWaeimquy26-][A-Za-z0-9_-]{10}[AQgw]$/;
  var TC_ROOT = 'https://firefox-ci-tc.services.mozilla.com';
  var MAX_TASK_ROWS = 500;
  var out = document.getElementById('pg-result');
  var seq = 0;

  // ── DOM helpers ─────────────────────────────────────────────────────────

  function el(tag, opts) {
    var n = document.createElement(tag);
    opts = opts || {};
    if (opts.cls) n.className = opts.cls;
    if (opts.title) n.title = String(opts.title);
    if (opts.href) n.href = opts.href; // only ever from tcUrl()/hashUrl()
    if (opts.text != null) n.textContent = String(opts.text);
    var kids = opts.kids || [];
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i];
      if (k == null) continue;
      n.appendChild(typeof k === 'string' ? document.createTextNode(k) : k);
    }
    return n;
  }

  function table(headers, rows) {
    var thead = el('thead', { kids: [el('tr', { kids: headers.map(function (h) { return el('th', { text: h }); }) })] });
    var tbody = el('tbody', {
      kids: rows.map(function (r) {
        return el('tr', { kids: r.map(function (c) { return c instanceof Node ? el('td', { kids: [c] }) : el('td', { text: c == null ? '—' : c }); }) });
      }),
    });
    return el('table', { kids: [thead, tbody] });
  }

  function tcUrl(kind, id) {
    if (!SLUGID.test(id)) return null;
    return TC_ROOT + (kind === 'group' ? '/tasks/groups/' : '/tasks/') + encodeURIComponent(id);
  }

  function hashUrl(kind, id) {
    return SLUGID.test(id) ? '#' + kind + '=' + id : null;
  }

  function link(href, text) {
    return href ? el('a', { cls: 'task-link', href: href, text: text }) : el('span', { text: text });
  }

  function badge(text, kind) {
    return el('span', { cls: 'badge ' + (kind || ''), text: text });
  }

  // ── Formatting ──────────────────────────────────────────────────────────

  function fmtDur(s) {
    if (s == null || !isFinite(s)) return '—';
    s = Math.round(Number(s));
    if (s < 60) return s + 's';
    var m = Math.round(s / 60);
    if (m < 60) return m + 'm';
    var h = Math.floor(m / 60);
    m = m % 60;
    if (h < 48) return h + 'h ' + (m < 10 ? '0' : '') + m + 'm';
    return (h / 24).toFixed(1) + 'd';
  }

  function fmtTs(t) {
    if (!t) return '—';
    var d = new Date(t);
    if (isNaN(d.getTime())) return '—';
    var rel = (d.getTime() - Date.now()) / 1000;
    var r = Math.abs(rel) < 30 ? 'now' : rel > 0 ? 'in ' + fmtDur(rel) : fmtDur(-rel) + ' ago';
    return d.toISOString().replace('T', ' ').slice(0, 19) + 'Z (' + r + ')';
  }

  function sourceBadge(body) {
    if (body.source === 'stored') return badge('stored', 'badge-ok');
    if (body.mode === 'at-pending') return badge('computed · at pending time', 'badge-warn');
    if (body.mode === 'hypothetical-now') return badge('computed · hypothetical (if pending now)', 'badge-warn');
    return badge('computed · ' + (body.mode || 'on demand'), 'badge-warn');
  }

  // ── Rendering ───────────────────────────────────────────────────────────

  function show(nodes) {
    out.replaceChildren.apply(out, nodes);
  }

  function predictionBlock(p, actual) {
    if (!p || !p.served) return [el('p', { cls: 'muted', text: 'No served prediction for this run.' })];
    var s = p.served;
    var rows = [
      ['Wait (pending → start)', fmtDur(s.wait_p50_s), fmtDur(s.wait_p90_s), actual ? fmtDur(actual.wait_s) : '—', s.wait_model_version],
      ['Run (start → resolved)', fmtDur(s.run_p50_s), fmtDur(s.run_p90_s), actual ? fmtDur(actual.run_s) : '—', s.run_model_version],
    ];
    if (p.shadow) rows.push(['Wait, shadow model', fmtDur(p.shadow.wait_p50_s), fmtDur(p.shadow.wait_p90_s), actual ? fmtDur(actual.wait_s) : '—', p.shadow.wait_model_version]);
    var nodes = [table(['', 'p50', 'p90', 'actual', 'model'], rows)];
    var e = p.expected;
    if (e) {
      nodes.push(el('h3', { text: 'Expected times' }));
      nodes.push(table(['', 'p50', 'p90'], [
        ['Start', fmtTs(e.start_p50), fmtTs(e.start_p90)],
        ['Finish', fmtTs(e.finish_p50), fmtTs(e.finish_p90)],
      ]));
    }
    return nodes;
  }

  function runsTable(runs) {
    if (!runs || !runs.length) return el('p', { cls: 'muted', text: 'No runs yet (unscheduled: waiting on dependencies or not yet scheduled).' });
    return table(['Run', 'State', 'Pending', 'Started', 'Resolved', 'Wait', 'Run time'], runs.map(function (r) {
      return [String(r.runId), r.state, fmtTs(r.pending), fmtTs(r.started), fmtTs(r.resolved),
        fmtDur(r.actual && r.actual.wait_s), fmtDur(r.actual && r.actual.run_s)];
    }));
  }

  function renderTask(b) {
    var nodes = [];
    nodes.push(el('h2', { text: b.name || b.taskId }));
    nodes.push(el('div', { cls: 'meta', kids: [
      sourceBadge(b), ' ',
      'task ', link(tcUrl('task', b.taskId), b.taskId),
      b.taskGroupId ? ' · group ' : null, b.taskGroupId ? link(hashUrl('group', b.taskGroupId), b.taskGroupId) : null,
      ' · queue ', el('span', { cls: 'mono', text: b.taskQueueId || '—' }),
      ' · family ', el('span', { cls: 'mono', text: b.repoFamily || '—' }),
    ] }));
    if (b.note) nodes.push(el('p', { cls: 'muted', text: b.note }));

    var runs, pred, actual;
    if (b.source === 'stored') {
      runs = b.runs || [];
      var last = runs[runs.length - 1];
      pred = last ? { served: last.served, shadow: last.shadow, expected: last.expected } : null;
      actual = last && last.actual;
      nodes.push(el('h3', { text: 'Prediction for run ' + (last ? last.runId : '—') }));
    } else {
      runs = b.runs || [];
      pred = b.prediction;
      var lr = runs[runs.length - 1];
      actual = b.mode === 'at-pending' && lr ? lr.actual : null;
      nodes.push(el('h3', { text: 'Prediction' }));
      if (b.anchor) {
        nodes.push(el('div', { cls: 'meta', text: 'Anchored at ' + fmtTs(b.anchor.pendingAt) +
          ' · queue pending ' + (b.anchor.queuePending == null ? 'unknown' : b.anchor.queuePending) +
          ' · priority ' + (b.anchor.priority || 'unknown') }));
      }
    }
    nodes = nodes.concat(predictionBlock(pred, actual));

    if (b.featureCoverage) {
      var fc = b.featureCoverage;
      nodes.push(el('div', { cls: 'meta', text: 'Missing features: wait ' + fc.wait.missing + '/' + fc.wait.total +
        ', run ' + fc.duration.missing + '/' + fc.duration.total +
        (b.baselines && b.baselines.wait ? ' · wait baseline level ' + b.baselines.wait.level : '') +
        (b.baselines && b.baselines.duration ? ' · run baseline level ' + b.baselines.duration.level : '') }));
    }
    if (b.mode === 'hypothetical-now' && runs.length) {
      nodes.push(el('p', { cls: 'muted', text: 'Actual runs are shown for reference; they are not what the hypothetical predicts.' }));
    }
    nodes.push(el('h3', { text: 'Runs' }));
    nodes.push(runsTable(runs));
    show(nodes);
  }

  function renderGroup(b) {
    var nodes = [];
    nodes.push(el('h2', { text: 'Task group ' + b.taskGroupId }));
    nodes.push(el('div', { cls: 'meta', kids: [
      sourceBadge(b), ' ',
      link(tcUrl('group', b.taskGroupId), 'open in Taskcluster'),
      ' · family ', el('span', { cls: 'mono', text: b.repoFamily || '—' }),
      b.status ? ' · ' + b.status.tasks + ' tasks, ' + b.status.resolved + ' resolved' : null,
    ] }));
    var eta = b.eta;
    if (!eta) {
      nodes.push(el('p', { cls: 'muted', text: 'No group estimate.' }));
    } else {
      if (eta.note) nodes.push(el('p', { cls: 'muted', text: eta.note }));
      if (eta.skipped) nodes.push(el('p', { cls: 'text-warn', text: 'Not estimated: ' + eta.skipped }));
      if (eta.estimates && eta.estimates.length) {
        if (eta.definition) nodes.push(el('div', { cls: 'meta', text: eta.definition }));
        nodes.push(el('div', { cls: 'meta', text: 't0 ' + fmtTs(eta.t0) + ' · prediction time ' + fmtTs(eta.predictionTime) +
          (eta.originalGraph ? ' · original graph ' + eta.originalGraph.tasks + ' tasks, ' + eta.originalGraph.state +
            (eta.originalGraph.actualFinish ? ', finished ' + fmtTs(eta.originalGraph.actualFinish) : '') : '') }));
        var actualSpan = eta.originalGraph && eta.originalGraph.actualFinish && eta.t0
          ? (Date.parse(eta.originalGraph.actualFinish) - Date.parse(eta.t0)) / 1000 : null;
        nodes.push(table(['Estimator', 'Span p50', 'Span p90', 'Finish p50', 'Finish p90', 'Actual span', 'Tasks known', 'Model'],
          eta.estimates.map(function (e) {
            return [e.estimator + (e.estimator === eta.recommended ? ' (default)' : ''), fmtDur(e.p50_s), fmtDur(e.p90_s), fmtTs(e.finish_p50), fmtTs(e.finish_p90),
              fmtDur(actualSpan), String(e.tasksKnown), e.modelVersion];
          })));
      }
    }

    var tasks = b.tasks || [];
    nodes.push(el('h3', { text: 'Tasks' + (tasks.length > MAX_TASK_ROWS ? ' (first ' + MAX_TASK_ROWS + ' of ' + tasks.length + ')' : '') }));
    if (b.truncated) nodes.push(el('p', { cls: 'text-warn', text: 'The task list was truncated.' }));
    nodes.push(el('p', { cls: 'muted', text: 'Click a task to see (or compute) its prediction.' }));
    nodes.push(table(['Task', 'Queue', 'Created', 'State', 'Served wait p50/p90'], tasks.slice(0, MAX_TASK_ROWS).map(function (t) {
      var lr = t.latestRun;
      var sv = lr && lr.served;
      return [link(hashUrl('task', t.taskId), t.name || t.taskId), t.taskQueueId, fmtTs(t.created),
        lr ? lr.state : 'unscheduled', sv ? fmtDur(sv.wait_p50_s) + ' / ' + fmtDur(sv.wait_p90_s) : '—'];
    })));
    show(nodes);
  }

  function renderError(status, body, retryAfter) {
    var msg = (body && typeof body.error === 'string') ? body.error : 'request failed (HTTP ' + status + ')';
    var nodes = [el('p', { cls: 'text-warn', text: msg })];
    if (body && typeof body.note === 'string') nodes.push(el('p', { cls: 'muted', text: body.note }));
    if (retryAfter && /^\d+$/.test(retryAfter)) nodes.push(el('p', { cls: 'muted', text: 'Try again in ' + retryAfter + 's.' }));
    show(nodes);
  }

  // ── Fetch + routing ─────────────────────────────────────────────────────

  function load(kind, id) {
    if ((kind !== 'task' && kind !== 'group') || !SLUGID.test(id)) {
      renderError(400, { error: 'Expected a 22-character Taskcluster id (letters, digits, - and _).' });
      return;
    }
    var mine = ++seq;
    show([el('p', { cls: 'muted', text: 'Loading ' + kind + ' ' + id + '… (on-demand: a few seconds for a task, up to ~30s for a group of thousands of tasks)' })]);
    fetch('/api/v0/playground/' + kind + '/' + encodeURIComponent(id), { headers: { Accept: 'application/json' }, credentials: 'omit' })
      .then(function (res) {
        return res.json().catch(function () { return null; }).then(function (body) {
          if (mine !== seq) return;
          if (!res.ok || !body) return renderError(res.status, body, res.headers.get('Retry-After'));
          if (kind === 'task') renderTask(body); else renderGroup(body);
        });
      })
      .catch(function () {
        if (mine === seq) renderError(0, { error: 'Network error; try again.' });
      });
  }

  function fromHash() {
    var m = /^#(task|group)=(.*)$/.exec(location.hash);
    if (!m) return;
    var id = m[2];
    var input = document.getElementById(m[1] === 'task' ? 'pg-task' : 'pg-group');
    if (SLUGID.test(id)) input.value = id;
    load(m[1], id);
  }

  function bind(formId, inputId, kind) {
    document.getElementById(formId).addEventListener('submit', function (ev) {
      ev.preventDefault();
      var id = document.getElementById(inputId).value.trim();
      if (!SLUGID.test(id)) {
        renderError(400, { error: 'Expected a 22-character Taskcluster id (letters, digits, - and _).' });
        return;
      }
      var h = '#' + kind + '=' + id;
      if (location.hash === h) load(kind, id); else location.hash = h;
    });
  }

  bind('pg-task-form', 'pg-task', 'task');
  bind('pg-group-form', 'pg-group', 'group');
  window.addEventListener('hashchange', fromHash);
  fromHash();
})();
