# Snapshot pickle+zstd — design

**Status: draft for review (2026-07-20).** User-approved decision (2026-07-20):
change the pipeline snapshot format from json+zlib to **pickle (protocol 5) +
zstd**. This is the zero-coordination first piece of the serialization lever
from [PERF-REPROCESS.md](../rig/PERF-REPROCESS.md) — the backend is the
snapshot's only writer and only reader, so the format flips in one deploy.
Companion specs: [TBA persistent cache](2026-07-20-tba-cache-design.md),
[DB retirement completion](2026-07-20-db-retirement-completion-design.md).

## 1. Problem

The snapshot is the pipeline's working-state carrier: `write_snapshot`
(`backend/src/google/snapshot.py:100`) uploads one blob `state/snapshot.{year}`
to GCS at the end of every current-year cycle (`src/data/main.py:96`),
carrying teams plus the objs tuple (year, team_years, events, team_events,
matches, etags) — ~39,000 attrs objects. Two readers, both backend:
`update_curr_year` warm-starts partial cycles from it (`src/data/main.py:183`)
and the freshness probe reads events+etags DB-less (`src/data/router.py:69`).

Today `serialize` (`snapshot.py:62-77`) walks every object through recursive
`attr.asdict` (lines 52, 67, 69), builds a 185 MB JSON string, and
zlib-compresses it to 24.7 MB (live 2026 numbers). `deserialize`
(`snapshot.py:80-97`) pays it all back: `json.loads` alone is ~1 s, then
`_load`/`_load_values` reconstruct every object via `from_dict` plus per-field
enum coercion (`snapshot.py:40-48`). This tax lands on every hourly cycle and
every probe.

Pickling the attrs objects directly skips **both** conversions — no asdict on
write, no dict→object rebuild on read — which is why pickle was chosen over
orjson. Accepted trade-off: the blob is no longer human-inspectable. Precedent:
the TBA fetch cache already pickles raw responses (`src/tba/utils.py`).

Verified picklability: the public model classes (`Team`, `Year`, …, `ETag`)
are statically-defined subclasses of the `attr.make_class(..., slots=True)`
bases (e.g. `src/db/models/team.py:42-45`), so pickle resolves them by normal
module import — instances round-trip today with **no model changes**. (Only
the raw `_Team`-style generated bases are unpicklable; the pipeline never
holds those.)

## 2. Design

### 2.1 Format

```python
payload = {
    "schema": 2,                        # bumped from SNAPSHOT_SCHEMA = 1
    "fingerprint": _models_fingerprint(),
    "year": objs[0].year,
    "teams": sorted(teams, key=lambda t: t.team),   # Team objects, as-is
    "objs": objs,                       # the 6-tuple, objects as-is
}
blob = zstandard.ZstdCompressor(level=3).compress(
    pickle.dumps(payload, protocol=5)
)
```

- **Objects directly** — no `attr.asdict`, no `from_dict`, no enum coercion
  (pickle preserves `Enum` members natively, retiring the
  `_enum_fields`/`_load` machinery at `snapshot.py:32-48` with the legacy path).
- **Sorting**: the per-dict `pk()` sort in `_dump_values` (`snapshot.py:52`)
  goes away with `_dump_values` itself — the objs dicts pickle as-is; only
  `teams` (a list) keeps its sort for deterministic bytes.
- **zstd level 3** (the library default): compression is not the bottleneck
  and level 3 on ~150 MB of pickle is well under a second while compressing
  comparably to today's zlib-6. Tune only if measurements say otherwise.
- **Blob key and content-type unchanged**: `state/snapshot.{year}`,
  `application/octet-stream` (`snapshot.py:105`). The atomic tmp-key +
  `copy_blob` write (`snapshot.py:103-108`) is untouched.
- **New dependency**: `zstandard = "^0.23"` in `backend/pyproject.toml`
  (wheels cover the `>=3.9,<3.12` range; prod runs 3.11 per the Dockerfile).

### 2.2 Schema/version safety

Two layers, both funneling into the existing fallback — `read_snapshot`
already wraps `deserialize` in try/except and returns `None` on any failure
(`snapshot.py:116-120`), which sends the caller to a full rebuild
(self-healing, and cheap once the TBA-cache spec lands):

1. **Unpickle guard.** attrs model changes can make `pickle.loads` raise
   (verified: a field added to a slotted attrs class raises `AttributeError`
   at load). Any such exception is caught by the existing wrapper — it never
   surfaces to the caller.
2. **Fingerprint gate.** Unpickling can also *silently succeed* with a stale
   object shape (a removed field lands in the subclass `__dict__`), so the
   payload carries an explicit fingerprint checked after load:

   ```python
   def _models_fingerprint() -> str:
       fields = {
           cls.__name__: [f.name for f in attr.fields(cls)]
           for cls in (Year, TeamYear, Event, TeamEvent, Match, ETag, Team)
       }
       return hashlib.sha256(
           json.dumps(fields, sort_keys=True).encode()
       ).hexdigest()[:16]
   ```

   Mismatch (or `schema != 2`) raises `ValueError` → rejection → rebuild.
   This replaces manual schema bumps for model edits; the integer `schema`
   remains for deliberate payload-shape changes (e.g. when the TBA-cache spec
   retires `objs[5]`, bump to 3).

No side header outside the pickle: nothing ever needs to inspect the version
without doing a full read, so a JSON preamble would be complexity for free.

### 2.3 Migration: dual-read, single-write

First deploy must warm-start from the previous json+zlib blob, so
`deserialize` sniffs the leading bytes (~10 lines):

- zstd frame magic `28 B5 2F FD` → new path;
- `0x78` (zlib header) → legacy `zlib.decompress` + `json.loads` path,
  unchanged;
- anything else → raise → existing fallback.

Writes emit only the new format from day one. The legacy read path (and the
`_dump_values`/`_load*`/`_enum_fields` helpers it needs) is deleted in a
follow-up PR once one warm start from a new-format blob is observed on
staging. Rejected alternative — accept one cold rebuild — saves those ~10
lines but burns a full-rebuild cycle for nothing.

### 2.4 Security

The pickle is only ever loaded from our own bucket (`state/` prefix), written
by our own single-writer pipeline under our service account; no external or
user-supplied bytes ever reach `pickle.loads`. This matches the existing TBA
cache precedent.

### 2.5 Freshness probe

The probe (`src/data/router.py:69`) keeps reading the full snapshot for
events+etags. Splitting out a small side blob is not worth the second write
path — the full read gets several times cheaper with this change, and the
probe is already rate-limited by the ping cooldown.

## 3. Expected performance (estimates — verify in §5)

| Step | Today | After (est.) |
|---|---|---|
| Serialize (39k asdicts + 185 MB `json.dumps` + zlib-6) | ~8–12 s | ~1–2 s (`pickle.dumps` + zstd-3) |
| Deserialize (unzlib + `json.loads` ~1 s + 39k `from_dict`/enum) | ~3–4 s | ~0.5–1.5 s (`pickle.loads` only) |
| Blob size | 24.7 MB | ~20–30 MB (similar) |

These are estimates from the measured 2026 snapshot (185 MB JSON, ~1 s
`json.loads`); the verification step records real numbers.

## 4. Implementation order

Two small PRs to `cph-staging`:

1. **Format flip**: `zstandard` dep; new `serialize`/`deserialize` (pickle+zstd
   write, magic-sniff dual read); `schema = 2` + fingerprint gate; unit tests
   (§5). Deploy, verify warm start both ways, record timings.
2. **Legacy removal**: delete the json+zlib read path and the now-unused
   asdict/`_load` helpers after step 1 is observed healthy in production.

## 5. Verification

- **Round-trip equality test**: build a representative objs tuple + teams,
  `deserialize(serialize(...))`, compare every object via
  `attr.asdict(a) == attr.asdict(b)` — **not** `==`, since `Model.__eq__`
  compares `pk()` only (`src/db/models/main.py:31-34`).
- **Legacy-read test**: bytes produced by the old json+zlib `serialize` must
  deserialize through the sniff path to equal objects.
- **Rejection test**: corrupt bytes, wrong schema int, and a wrong fingerprint
  each make `read_snapshot` return `None`, never raise.
- **Staging**: deploy per [DEPLOY.md](../rig/deploy/DEPLOY.md); first partial
  cycle warm-starts from the legacy blob (dual read), second from the new
  format. Compare the `Read Snapshot` / `Write Snapshot` timer lines before
  and after; record cycle timings in [RIG.md](../rig/RIG.md).

## 6. Open questions

- None blocking. zstd level and whether `teams` sorting is worth keeping can
  be revisited once real timings are in RIG.md.
