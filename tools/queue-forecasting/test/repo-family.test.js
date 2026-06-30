import { test } from 'node:test';
import assert from 'node:assert';
import { deriveRepoFamily, REPO_FAMILY_DERIVATION_VERSION } from '../src/repo-family.js';

test('metadata.source hg path wins: try', () => {
  const r = deriveRepoFamily({ metadataSource: 'https://hg.mozilla.org/try/file/abc/taskcluster/ci', routes: [], schedulerId: 'gecko-level-1' });
  assert.equal(r.family, 'try');
  assert.equal(r.source, 'source');
  assert.equal(r.version, REPO_FAMILY_DERIVATION_VERSION);
});

test('metadata.source autoland', () => {
  const r = deriveRepoFamily({ metadataSource: 'https://hg.mozilla.org/integration/autoland/file/tip/x', routes: [] });
  assert.equal(r.family, 'autoland');
});

test('metadata.source beta -> release_beta', () => {
  const r = deriveRepoFamily({ metadataSource: 'https://hg.mozilla.org/releases/mozilla-beta/x', routes: [] });
  assert.equal(r.family, 'release_beta');
});

test('routes fallback when source missing', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: ['tc-treeherder.v2.mozilla-central.abc', 'index.gecko.v2.mozilla-central.x'] });
  assert.equal(r.family, 'central');
  assert.equal(r.source, 'route');
  assert.ok(r.evidence.includes('mozilla-central'));
});

test('scheduler coarse fallback: level-1 -> try', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: [], schedulerId: 'gecko-level-1' });
  assert.equal(r.family, 'try');
  assert.equal(r.source, 'scheduler');
});

test('unknown when nothing matches', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: [], schedulerId: null });
  assert.equal(r.family, 'unknown');
  assert.equal(r.source, 'unknown');
});

test('evidence is short, never a full route array', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: ['x'.repeat(500), 'index.gecko.v2.try.1'] });
  assert.ok(r.evidence.length <= 64);
});

test('evidence truncates when matched token exceeds 64 chars', () => {
  const longId = 'gecko-' + 'x'.repeat(80) + '-level-1';
  const r = deriveRepoFamily({ schedulerId: longId });
  assert.equal(r.family, 'try');
  assert.equal(r.source, 'scheduler');
  assert.equal(r.evidence.length, 64);   // slice actually fired
});

test('metadata.source wins over a conflicting scheduler hint', () => {
  const r = deriveRepoFamily({ metadataSource: 'https://hg.mozilla.org/integration/autoland/x', routes: [], schedulerId: 'gecko-level-1' });
  assert.equal(r.family, 'autoland');   // not 'try'
  assert.equal(r.source, 'source');
});

test('routes win over a conflicting scheduler hint', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: ['index.gecko.v2.autoland.x'], schedulerId: 'gecko-level-1' });
  assert.equal(r.family, 'autoland');
  assert.equal(r.source, 'route');
});

test('mozilla-release source collapses to release_beta', () => {
  const r = deriveRepoFamily({ metadataSource: 'https://hg.mozilla.org/releases/mozilla-release/x', routes: [] });
  assert.equal(r.family, 'release_beta');
});

test('scheduler level-3 maps to other', () => {
  const r = deriveRepoFamily({ schedulerId: 'gecko-level-3' });
  assert.equal(r.family, 'other');
  assert.equal(r.source, 'scheduler');
});

test('boundary anchors prevent over-match', () => {
  for (const s of ['https://hg.mozilla.org/projects/try-comm-central/x', 'https://hg.mozilla.org/mozilla-central-foo/x']) {
    assert.equal(deriveRepoFamily({ metadataSource: s }).family, 'unknown');
  }
});

test('null/undefined inputs are safe', () => {
  assert.doesNotThrow(() => deriveRepoFamily());
  assert.doesNotThrow(() => deriveRepoFamily({ routes: null, metadataSource: undefined, schedulerId: undefined }));
  assert.equal(deriveRepoFamily().family, 'unknown');
});
