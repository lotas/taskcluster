/**
 * Load a single ONNX serving bundle: feature schema + category mappings +
 * two ONNX models + manifest (for artifact_hash verification).
 *
 * Each bundle corresponds to one trainer config (one config stem, e.g.
 * "wait_time_residual_throughput_filtered_baseline").
 */
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

// Lazy-load onnxruntime-node so unit tests can opt out of native binding load.
let ortPromise = null;
function getOrt() {
  if (!ortPromise) ortPromise = import('onnxruntime-node');
  return ortPromise;
}

function readJson(p) {
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

function computeArtifactHash(files) {
  const h = crypto.createHash('sha256');
  for (const f of files) h.update(fs.readFileSync(f));
  return h.digest('hex').slice(0, 16);
}

function bundleFiles(dir, stem) {
  return {
    p50File: path.join(dir, `${stem}_p50.onnx`),
    p90File: path.join(dir, `${stem}_p90.onnx`),
    catFile: path.join(dir, `${stem}_category_mappings.json`),
    schemaFile: path.join(dir, `${stem}_feature_schema.json`),
    manifestFile: path.join(dir, `${stem}_manifest.json`),
  };
}

/**
 * @param {string} dir   Directory containing the bundle files (a per-day model dir).
 * @param {string} stem  Filename stem (config name without _manifest.json).
 * @param {object} [opts]
 * @param {boolean} [opts.skipOnnxLoad]  Skip ORT session creation (test-only).
 * @returns {Promise<{stem, schema, categories, manifest, artifact_hash, sessionP50, sessionP90}>}
 */
export async function loadBundle(dir, stem, opts = {}) {
  const { p50File, p90File, catFile, schemaFile, manifestFile } = bundleFiles(dir, stem);

  for (const f of [p50File, p90File, catFile, schemaFile, manifestFile]) {
    if (!fs.existsSync(f)) {
      throw new Error(`Missing bundle file: ${f}`);
    }
  }

  const schema = readJson(schemaFile);
  const categories = readJson(catFile);
  const manifest = readJson(manifestFile);

  const computed = computeArtifactHash([p50File, p90File, catFile, schemaFile]);
  if (manifest.artifact_hash && computed !== manifest.artifact_hash) {
    throw new Error(
      `artifact_hash mismatch for ${stem}: manifest=${manifest.artifact_hash} computed=${computed}`,
    );
  }

  let sessionP50 = null;
  let sessionP90 = null;
  if (!opts.skipOnnxLoad) {
    const ort = await getOrt();
    sessionP50 = await ort.InferenceSession.create(p50File);
    sessionP90 = await ort.InferenceSession.create(p90File);
  }

  return {
    stem,
    schema,
    categories,
    manifest,
    artifact_hash: computed,
    sessionP50,
    sessionP90,
  };
}

/**
 * Find the latest complete models directory under trainer/data/models/.
 *
 * Training writes each config's bundle directly into the date directory. A
 * later config can fail after an earlier one succeeds, leaving the newest date
 * valid as walk-forward resume state but incomplete for live serving. Skip
 * those partial dates rather than making a live-predictor restart fail.
 *
 * @param {string} modelsRoot  Absolute path to trainer/data/models.
 * @param {string[]} requiredStems  Every bundle the caller needs to serve.
 * @returns {string|null}  Absolute path to the latest complete day dir, or null.
 */
export function findLatestModelDir(modelsRoot, requiredStems) {
  if (!Array.isArray(requiredStems) || requiredStems.length === 0) {
    throw new TypeError('requiredStems must contain at least one model stem');
  }
  if (!fs.existsSync(modelsRoot)) return null;
  const dates = fs.readdirSync(modelsRoot)
    .filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d))
    .sort()
    .reverse();
  for (const date of dates) {
    const dir = path.join(modelsRoot, date);
    const complete = requiredStems.every((stem) =>
      Object.values(bundleFiles(dir, stem)).every((file) => fs.existsSync(file))
    );
    if (complete) return dir;
  }
  return null;
}
