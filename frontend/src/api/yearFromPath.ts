// Year-from-path grammar and the per-year hist-epoch rule. Mirrors
// backend/src/google/year_path.py; both sides check the shared vectors in
// backend/tests/data/year_from_path_vectors.json. No imports, erasable TS only.

const YEAR_PATTERNS: RegExp[] = [
  // event/{key}: the key's leading four digits.
  /^event\/(\d{4})/,
  // events/Y…, team_years/Y…, noteworthy_matches/Y…
  /^(?:events|team_years|noteworthy_matches)\/(\d{4})(?!\d)/,
  // parquet/Y/…
  /^parquet\/(\d{4})\//,
  // team/{num}/Y — exactly three segments, Y four digits.
  /^team\/[^/]+\/(\d{4})$/,
];

export function yearFromPath(path: string): number | null {
  for (const pattern of YEAR_PATTERNS) {
    const match = pattern.exec(path);
    if (match) {
      return parseInt(match[1], 10);
    }
  }
  return null;
}

// Epoch whose hist/{epoch}/… objects serve the path:
// max(hist_epochs[year] ?? 0, hist_epoch). A path with no year uses hist_epoch.
export function resolveHistEpoch(
  manifest: { hist_epoch?: number; hist_epochs?: Record<string, number> },
  logicalPath: string
): number {
  const globalEpoch = manifest.hist_epoch ?? 1;
  const year = yearFromPath(logicalPath);
  if (year === null) {
    return globalEpoch;
  }
  return Math.max(manifest.hist_epochs?.[String(year)] ?? 0, globalEpoch);
}
