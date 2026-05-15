/**
 * JS port of Python FeatureBuilder (trainer/src/features.py).
 *
 * Produces a Float32Array in the order specified by feature_schema.feature_order.
 * - Categoricals: integer code from category_mappings; cold_start_code (-1) for unseen/null.
 * - Numerics: cast to float; null/undefined/non-finite → NaN, NOT 0.0.
 *   LightGBM bakes a missing-value split direction at train time, so NaN
 *   and 0 are semantically different; coercing to 0 collapses them.
 * - Derived features:
 *     cyclical_time: hour_sin/hour_cos (24-period) and day_sin/day_cos (7-period)
 *                    from row[source] (UTC). day-of-week uses Python convention: 0=Mon.
 *     build_type_regex: first capture group of spec.pattern against row[source].
 * - Tags: dotted names like "tags.kind" are read from row.tags[key].
 */

function toFloat32(v) {
  if (v === null || v === undefined) return NaN;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : NaN;
}

function readTagValue(row, dottedName) {
  const key = dottedName.slice('tags.'.length);
  if (!row.tags || typeof row.tags !== 'object') return null;
  return row.tags[key] ?? null;
}

function encodeCategorical(value, vocab, coldStartCode) {
  if (value === null || value === undefined) return coldStartCode;
  const idx = vocab.indexOf(value);
  return idx === -1 ? coldStartCode : idx;
}

function applyDerived(row, schema) {
  const derived = schema.derived_features || {};
  const out = {};

  if (derived.cyclical_time) {
    const ts = row[derived.cyclical_time.source];
    const d = ts instanceof Date ? ts : new Date(ts);
    const hour = d.getUTCHours();
    // Python pandas .dt.dayofweek: 0=Mon…6=Sun. JS .getUTCDay(): 0=Sun…6=Sat.
    const dowPy = (d.getUTCDay() + 6) % 7;
    out.hour_sin = Math.sin(2 * Math.PI * hour / 24);
    out.hour_cos = Math.cos(2 * Math.PI * hour / 24);
    out.day_sin  = Math.sin(2 * Math.PI * dowPy / 7);
    out.day_cos  = Math.cos(2 * Math.PI * dowPy / 7);
  }

  if (derived.build_type_regex) {
    const spec = derived.build_type_regex;
    const src = row[spec.source];
    if (typeof src === 'string') {
      const m = src.match(new RegExp(spec.pattern));
      out.build_type = m ? m[1] : null;
    } else {
      out.build_type = null;
    }
  }

  return out;
}

/**
 * Build the float32 input tensor for an ONNX model.
 *
 * @param {object} row  DB row (task + task_run joined).
 * @param {object} liveFeatures  Extra fields not in the row: bl_wait_p50, throughput, etc.
 * @param {object} schema  feature_schema.json contents.
 * @param {object} categories  category_mappings.json contents.
 * @returns {Float32Array}
 */
export function buildFeatureVector(row, liveFeatures, schema, categories) {
  const coldStart = schema.cold_start_code ?? -1;
  const derived = applyDerived(row, schema);

  // Merge sources: derived overrides liveFeatures overrides row.
  const sources = { ...row, ...liveFeatures, ...derived };

  // Resolve tags.* features from row.tags before the main loop.
  for (const f of [...(schema.categorical_features || []), ...(schema.numeric_features || [])]) {
    if (f.startsWith('tags.')) sources[f] = readTagValue(row, f);
  }

  const cats = new Set(schema.categorical_features || []);
  const out = new Float32Array(schema.feature_order.length);
  for (let i = 0; i < schema.feature_order.length; i++) {
    const name = schema.feature_order[i];
    if (cats.has(name)) {
      out[i] = encodeCategorical(sources[name], categories[name] || [], coldStart);
    } else {
      out[i] = toFloat32(sources[name]);
    }
  }
  return out;
}
