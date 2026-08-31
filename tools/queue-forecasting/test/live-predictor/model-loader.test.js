import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import path from 'path';
import os from 'os';
import crypto from 'crypto';
import { findLatestModelDir, loadBundle } from '../../src/live-predictor/model-loader.js';

function writeJson(p, obj) { fs.writeFileSync(p, JSON.stringify(obj)); }

function makeFixtureDir(stem = 'wait_time_demo') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mloader-'));
  // Use empty files as ONNX stand-ins; loadBundle should not parse them
  // when it's called with skipOnnxLoad=true (test mode).
  fs.writeFileSync(path.join(dir, `${stem}_p50.onnx`), 'p50bytes');
  fs.writeFileSync(path.join(dir, `${stem}_p90.onnx`), 'p90bytes');
  writeJson(path.join(dir, `${stem}_category_mappings.json`), { cat1: ['a', 'b'] });
  writeJson(path.join(dir, `${stem}_feature_schema.json`), {
    model_version: 'v_2026-05-13_abcdef12',
    target: 'wait_time',
    feature_order: ['cat1', 'num1'],
    categorical_features: ['cat1'],
    numeric_features: ['num1'],
    quantile_models: { '0.5': `${stem}_p50.onnx`, '0.9': `${stem}_p90.onnx` },
    category_mappings_file: `${stem}_category_mappings.json`,
    residual: { transform: 'log_ratio', baseline_feature: 'bl_wait_p50' },
    cold_start_code: -1,
  });

  // Compute the expected artifact_hash the same way the trainer does:
  // concat the four serving files in order, sha256, slice 16 hex.
  const h = crypto.createHash('sha256');
  for (const f of [
    `${stem}_p50.onnx`, `${stem}_p90.onnx`,
    `${stem}_category_mappings.json`, `${stem}_feature_schema.json`,
  ]) {
    h.update(fs.readFileSync(path.join(dir, f)));
  }
  const artifact_hash = h.digest('hex').slice(0, 16);

  writeJson(path.join(dir, `${stem}_manifest.json`), {
    artifact_hash,
    wait_model_version: 'unused',
    serving_artifacts: [
      `${stem}_p50.onnx`, `${stem}_p90.onnx`,
      `${stem}_category_mappings.json`, `${stem}_feature_schema.json`,
    ],
  });

  return { dir, stem, artifact_hash };
}

test('loadBundle reads schema, categories, manifest, and verifies artifact_hash', async () => {
  const { dir, stem, artifact_hash } = makeFixtureDir();
  const bundle = await loadBundle(dir, stem, { skipOnnxLoad: true });
  assert.equal(bundle.schema.model_version, 'v_2026-05-13_abcdef12');
  assert.deepEqual(bundle.categories, { cat1: ['a', 'b'] });
  assert.equal(bundle.artifact_hash, artifact_hash);
  assert.equal(bundle.stem, stem);
});

test('loadBundle throws when artifact_hash mismatches', async () => {
  const { dir, stem } = makeFixtureDir();
  // Corrupt one of the serving files.
  fs.writeFileSync(path.join(dir, `${stem}_p50.onnx`), 'tampered');
  await assert.rejects(
    () => loadBundle(dir, stem, { skipOnnxLoad: true }),
    /artifact_hash mismatch/,
  );
});

function writeBundleFiles(dir, stem) {
  fs.mkdirSync(dir, { recursive: true });
  for (const suffix of [
    'p50.onnx',
    'p90.onnx',
    'category_mappings.json',
    'feature_schema.json',
    'manifest.json',
  ]) {
    fs.writeFileSync(path.join(dir, `${stem}_${suffix}`), 'fixture');
  }
}

test('findLatestModelDir falls back when the newest date is incomplete', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'model-dates-'));
  const waitStem = 'wait_time_demo';
  const durationStem = 'run_duration_demo';
  const previous = path.join(root, '2026-08-29');
  const latest = path.join(root, '2026-08-30');

  writeBundleFiles(previous, waitStem);
  writeBundleFiles(previous, durationStem);
  writeBundleFiles(latest, waitStem);

  assert.equal(findLatestModelDir(root, [waitStem, durationStem]), previous);
});

test('findLatestModelDir selects the newest date when every bundle is complete', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'model-dates-'));
  const stems = ['wait_time_demo', 'run_duration_demo'];
  const previous = path.join(root, '2026-08-29');
  const latest = path.join(root, '2026-08-30');

  for (const stem of stems) {
    writeBundleFiles(previous, stem);
    writeBundleFiles(latest, stem);
  }

  assert.equal(findLatestModelDir(root, stems), latest);
});

test('findLatestModelDir returns null when no date has every required bundle', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'model-dates-'));
  writeBundleFiles(path.join(root, '2026-08-30'), 'wait_time_demo');

  assert.equal(
    findLatestModelDir(root, ['wait_time_demo', 'run_duration_demo']),
    null,
  );
});
