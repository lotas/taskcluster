// Pure repo-family derivation, shared by the collector (forward) and the
// backfill script (historical). Stores only the result + a short evidence
// token, never raw route arrays.
export const REPO_FAMILY_DERIVATION_VERSION = 1;

// hg path fragment -> family. Order matters (most specific first).
const SOURCE_PATTERNS = [
  [/\/try(\/|$)/,                         'try'],
  [/\/integration\/autoland(\/|$)/,       'autoland'],
  [/\/releases\/mozilla-beta(\/|$)/,      'release_beta'],
  [/\/releases\/mozilla-release(\/|$)/,   'release_beta'], // release + beta share a band
  [/\/mozilla-central(\/|$)/,             'central'],
];

// route token -> family.
const ROUTE_PATTERNS = [
  [/\.v2\.try(\.|$)/,             'try'],
  [/\.v2\.autoland(\.|$)/,        'autoland'],
  [/\.v2\.mozilla-beta(\.|$)/,    'release_beta'],
  [/\.v2\.mozilla-release(\.|$)/, 'release_beta'],
  [/\.v2\.mozilla-central(\.|$)/, 'central'],
];

function short(s) {
  const str = String(s ?? '');
  return str.length <= 64 ? str : str.slice(0, 64);
}

export function deriveRepoFamily({ routes = [], metadataSource = null, schedulerId = null } = {}) {
  const v = REPO_FAMILY_DERIVATION_VERSION;

  if (typeof metadataSource === 'string') {
    for (const [re, fam] of SOURCE_PATTERNS) {
      const m = metadataSource.match(re);
      if (m) return { family: fam, source: 'source', evidence: short(m[0]), version: v };
    }
  }

  const routeList = Array.isArray(routes) ? routes : [];
  for (const route of routeList) {
    if (typeof route !== 'string') continue;
    for (const [re, fam] of ROUTE_PATTERNS) {
      const m = route.match(re);
      if (m) return { family: fam, source: 'route', evidence: short(m[0]), version: v };
    }
  }

  // Coarse scheduler fallback. Only level-1 maps reliably (try-dominated);
  // level-3 is mixed (autoland/central/release) -> 'other'. Audited via source.
  if (typeof schedulerId === 'string') {
    if (/-level-1$/.test(schedulerId)) return { family: 'try',   source: 'scheduler', evidence: short(schedulerId), version: v };
    if (/-level-3$/.test(schedulerId)) return { family: 'other', source: 'scheduler', evidence: short(schedulerId), version: v };
  }

  return { family: 'unknown', source: 'unknown', evidence: null, version: v };
}
