/**
 * ONNX smoke test — verifies that trainer-produced serving artifacts round-trip
 * correctly through onnxruntime-node.
 *
 * Finds the latest trainer/data/models/<YYYY-MM-DD>/ directory, loads every
 * *_feature_schema.json it contains, and for each:
 *   1. Recomputes artifact_hash from the four serving files and asserts it
 *      matches the manifest.
 *   2. Constructs a synthetic feature vector and runs p50 + p90 inference.
 *   3. For residual configs, applies the log_ratio inverse and prints the result.
 *
 * Does NOT write to the database or require the collector.  Exit code 0 on
 * success, non-zero on any error.
 *
 * Usage:  node test/onnx-smoke.js
 */

import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import { fileURLToPath } from 'url';
import * as ort from 'onnxruntime-node';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.join(__dirname, '..');
const MODELS_DIR = path.join(PROJECT_ROOT, 'trainer', 'data', 'models');

// ── Helpers ──────────────────────────────────────────────────────────────────

function latestModelsDir() {
  if (!fs.existsSync(MODELS_DIR)) return null;
  const dates = fs.readdirSync(MODELS_DIR)
    .filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
    .sort()
    .reverse();
  return dates[0] ? path.join(MODELS_DIR, dates[0]) : null;
}

function fileHash(filePath) {
  const h = crypto.createHash('sha256');
  h.update(fs.readFileSync(filePath));
  return h.digest('hex');
}

function artifactHashFromFiles(files) {
  const h = crypto.createHash('sha256');
  for (const f of files) {
    h.update(fs.readFileSync(f));
  }
  return h.digest('hex').slice(0, 16);
}

// Apply the log_ratio inverse: exp(raw) * (baseline + 1) - 1
function logRatioInverse(raw, baseline) {
  return Math.exp(raw) * (baseline + 1.0) - 1.0;
}

// ── Per-schema smoke run ──────────────────────────────────────────────────────

async function smokeSchema(dir, schemaFile) {
  const schemaPath = path.join(dir, schemaFile);
  const schema = JSON.parse(fs.readFileSync(schemaPath, 'utf8'));

  const stem = schemaFile.replace('_feature_schema.json', '');
  const p50File = path.join(dir, `${stem}_p50.onnx`);
  const p90File = path.join(dir, `${stem}_p90.onnx`);
  const catFile = path.join(dir, schema.category_mappings_file);
  const manifestFile = path.join(dir, `${stem}_manifest.json`);

  // 1. Verify all serving files exist.
  for (const f of [p50File, p90File, catFile, schemaPath]) {
    if (!fs.existsSync(f)) throw new Error(`Missing serving file: ${f}`);
  }

  // 2. Recompute artifact_hash and compare to manifest.
  if (fs.existsSync(manifestFile)) {
    const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
    if (manifest.artifact_hash) {
      const computed = artifactHashFromFiles([p50File, p90File, catFile, schemaPath]);
      if (computed !== manifest.artifact_hash) {
        throw new Error(
          `artifact_hash mismatch for ${stem}:\n  manifest: ${manifest.artifact_hash}\n  computed: ${computed}`
        );
      }
      console.log(`  artifact_hash OK: ${computed}`);
    }
  }

  // 3. Build a synthetic input vector.
  const categories = JSON.parse(fs.readFileSync(catFile, 'utf8'));
  const featureOrder = schema.feature_order;
  const catSet = new Set(schema.categorical_features || []);

  const inputData = new Float32Array(featureOrder.length);
  for (let i = 0; i < featureOrder.length; i++) {
    const feat = featureOrder[i];
    if (catSet.has(feat)) {
      // Use code 0 (first known category) as synthetic input.
      inputData[i] = 0.0;
    } else {
      // Plausible non-zero defaults for key numeric features.
      if (feat === 'bl_wait_p50' || feat === 'bl_duration_p50') {
        inputData[i] = 60.0;
      } else if (feat === 'max_run_time_s') {
        inputData[i] = 3600.0;
      } else if (feat === 'queue_pending') {
        inputData[i] = 10.0;
      } else if (feat === 'hour_cos' || feat === 'day_cos') {
        inputData[i] = 1.0;
      } else {
        inputData[i] = 0.0;
      }
    }
  }

  const inputTensor = new ort.Tensor('float32', inputData, [1, featureOrder.length]);

  // 4. Run p50 and p90 inference.
  const sess50 = await ort.InferenceSession.create(p50File);
  const sess90 = await ort.InferenceSession.create(p90File);

  const inputName50 = sess50.inputNames[0];
  const inputName90 = sess90.inputNames[0];

  const out50 = await sess50.run({ [inputName50]: inputTensor });
  const out90 = await sess90.run({ [inputName90]: inputTensor });

  const raw50 = out50[sess50.outputNames[0]].data[0];
  const raw90 = out90[sess90.outputNames[0]].data[0];

  if (!Number.isFinite(raw50)) throw new Error(`p50 output is not finite: ${raw50}`);
  if (!Number.isFinite(raw90)) throw new Error(`p90 output is not finite: ${raw90}`);

  // 5. Apply inverse transform for residual configs.
  let p50 = raw50;
  let p90 = raw90;
  const residual = schema.residual;
  if (residual && residual.transform === 'log_ratio') {
    const blFeat = residual.baseline_feature;
    const blIdx = featureOrder.indexOf(blFeat);
    const baseline = blIdx >= 0 ? inputData[blIdx] : 0.0;
    p50 = logRatioInverse(raw50, baseline);
    p90 = logRatioInverse(raw90, baseline);
    console.log(`  raw(p50,p90)=(${raw50.toFixed(4)}, ${raw90.toFixed(4)})  ` +
                `inverse(p50,p90)=(${p50.toFixed(2)}s, ${p90.toFixed(2)}s)  [log_ratio, bl=${baseline}]`);
  } else {
    console.log(`  p50=${raw50.toFixed(2)}s  p90=${raw90.toFixed(2)}s`);
  }

  if (p50 > p90 + 0.01) {
    // Soft warning only — quantile ordering can legitimately invert on synthetic inputs.
    console.warn(`  WARNING: p50 (${p50.toFixed(2)}) > p90 (${p90.toFixed(2)}) on synthetic input`);
  }
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  const dir = latestModelsDir();
  if (!dir) {
    console.error(`No model directories found under ${MODELS_DIR}`);
    console.error('Run a training pass first: ./scripts/run_training.sh <config>');
    process.exit(1);
  }

  console.log(`Scanning: ${dir}`);

  const schemaFiles = fs.readdirSync(dir)
    .filter(f => f.endsWith('_feature_schema.json'))
    .sort();

  if (schemaFiles.length === 0) {
    console.error('No *_feature_schema.json files found — re-run training to produce ONNX artifacts.');
    process.exit(1);
  }

  let errors = 0;
  for (const sf of schemaFiles) {
    const stem = sf.replace('_feature_schema.json', '');
    console.log(`\n[${stem}]`);
    try {
      await smokeSchema(dir, sf);
      console.log(`  OK`);
    } catch (err) {
      console.error(`  FAIL: ${err.message}`);
      errors++;
    }
  }

  if (errors > 0) {
    console.error(`\n${errors} schema(s) failed.`);
    process.exit(1);
  }
  console.log(`\nAll ${schemaFiles.length} schema(s) passed.`);
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
