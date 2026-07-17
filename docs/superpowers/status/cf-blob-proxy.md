# Cloudflare blob proxy — statbotics staging

STATUS: LIVE (verified 2026-07-10)

Staging's public GCS bucket (`statbotics-staging-site`) is now fronted by a
Cloudflare-proxied hostname so blob reads are edge-cached instead of hitting GCS
directly:

```
https://blobs-statbotics.iterativerefinement.com/<path>
  → Worker `statbotics-blob-proxy`
  → https://storage.googleapis.com/statbotics-staging-site/<path>
```

## Cloudflare resources created (zone iterativerefinement.com)

| Resource | Name | Config |
|----------|------|--------|
| Worker | `statbotics-blob-proxy` | path rewrite onto the bucket; `cf.cacheEverything: true` for every path (incl. `/manifest.json`, which is edge-cached ~60s with the Worker rewriting its client-facing `Cache-Control` back to `max-age=60`) |
| DNS | AAAA `blobs-statbotics` | `100::`, proxied (orange cloud) — same placeholder pattern as the other statbotics records |
| Route | `blobs-statbotics.iterativerefinement.com/*` | → `statbotics-blob-proxy` |

Deployed worker source (also embedded in `docs/superpowers/rig/deploy/deploy.sh`
`step_dns`, parameterized):

```js
const BUCKET = "statbotics-staging-site";
const ORIGIN = "https://storage.googleapis.com";
addEventListener("fetch", (event) => { event.respondWith(handle(event.request)); });
async function handle(request) {
  const url = new URL(request.url);
  const originUrl = ORIGIN + "/" + BUCKET + url.pathname + url.search;
  const originReq = new Request(originUrl, request);
  const isManifest = url.pathname === "/manifest.json";
  const res = await fetch(originReq, { cf: { cacheEverything: true } });
  if (isManifest) {
    const out = new Response(res.body, res);
    out.headers.set("Cache-Control", "public, max-age=60");
    return out;
  }
  return res;
}
```

## Design notes / gotchas found

- **Zone Browser Cache TTL floor — manifest now edge-cached (updated
  2026-07-10).** The zone applies a 4 h (14400 s) Browser Cache TTL floor at the
  cache layer: a cached object whose *stored* origin `max-age` is below 14400
  gets its client-facing `Cache-Control` raised to 14400. The earlier design
  therefore bypassed the edge for `/manifest.json` (`cacheEverything: false` →
  `cf-cache-status: DYNAMIC`), which made every cold page pay a ~160-260 ms GCS
  RTT for the manifest before any blob URL could be resolved. The floor,
  however, only rewrites the header the edge *stores/serves* — a header the
  Worker sets on its own returned `Response` wins, un-floored (verified live).
  So the manifest is now `cacheEverything: true` (edge-cached, edge TTL = its
  origin `max-age=60`) and the Worker rewrites the returned `Cache-Control` back
  to `public, max-age=60`. Result: co-located event visitors share one
  edge-cached manifest per PoP (`cf-cache-status: HIT`, `age` advancing to ~60
  then `REVALIDATED`) while browsers still revalidate every 60 s. The earlier
  "mixed variants" symptom came from relying on the *stored* Cache-Control
  (subject to the floor) rather than overriding the Worker's own response
  header. Staleness stays bounded at ~60 s edge + 60 s browser.
- **Manifest 304s — encoding-dependent.** Since the blob-gc agent's gzip
  change, GCS stores `manifest.json` with `Content-Encoding: gzip`. Requests
  **with** `Accept-Encoding: gzip` (i.e. every browser) get the stored bytes, a
  **strong** ETag, and `If-None-Match` → **304** — verified both direct against
  GCS and through the proxy. Requests **without** gzip get GCS's transcoded
  (gunzipped) body with a **weak** ETag, which GCS answers with `200`, not
  `304` (a GCS transcoding quirk, not a proxy artifact — reproduced directly
  against storage.googleapis.com). So real browser revalidation works; only
  identity-encoding probes (plain `curl -I` ETags) see 200s. v2 blobs
  (identity-encoded, strong ETags) revalidate to `304` through the proxy
  unconditionally, including from an edge-cache HIT.
- **CORS is cache-safe.** GCS sends `Vary: Origin` and Cloudflare keys the
  worker-subrequest cache on it: per-Origin variants cache separately, each
  with its correct `Access-Control-Allow-Origin`. Verified: no-Origin HIT does
  not poison with-Origin requests; with-Origin repeat = HIT + correct ACAO;
  `OPTIONS` preflight passes through (allow-methods GET,HEAD,OPTIONS).
- **Legacy unversioned paths** (e.g. `/teams/all`) edge-cache under the zone
  floor at `max-age=14400` client-facing. Real readers resolve through the
  manifest to versioned paths, so this only affects direct legacy probes (same
  caveat already noted in staging.md for GCS's own 3600 default).

## Verification evidence (curl, 2026-07-10)

- Immutable v2 blob (`/v2/event/2026arc.56978849c0da`):
  `cf-cache-status: MISS` first fetch → `HIT` on repeats;
  `cache-control: public, max-age=31536000, immutable` preserved; body
  SHA1-identical to the direct GCS object.
- 304 passthrough: `If-None-Match` with the blob's strong ETag → `304` (both
  direct GCS and via proxy, including on an edge-cache HIT); wrong ETag → `200`.
- Manifest (edge-cache change, 2026-07-10): consecutive fetches
  `cf-cache-status: HIT` with `age` advancing (1→2→…→~60 then `REVALIDATED`) +
  `cache-control: public, max-age=60` (not floored to 14400); with-Origin
  (real browser) variant HITs too, `vary: Accept-Encoding, Origin`, correct
  `access-control-allow-origin`; `content-encoding: gzip` passthrough verified
  (`curl --compressed` decodes to valid JSON with keys
  `blobs/cycle/hist_epoch/schema`); browser-style conditional GET
  (`--compressed` + strong ETag) → `304` through the proxy; `OPTIONS` preflight
  → 200 (allow-methods GET,HEAD,OPTIONS). v2 immutable blobs unchanged
  (`HIT`, `max-age=31536000, immutable`).
- Browser (Chrome, statbotics.iterativerefinement.com): home, `/team/254`
  (11 blob fetches), `/event/2026caclv` incl. SOS + Simulation tabs — all blob
  requests on `blobs-statbotics.…`, 0 on `storage.googleapis.com`, no
  CORS/console errors, EPA data renders; repeat curl of the exact
  browser-fetched blob URLs (with the site Origin) → `cf-cache-status: HIT`
  with correct `access-control-allow-origin`.
- Existing services untouched: `statbotics-proxy` worker + its 2 routes intact;
  API `/info` 200 via both the CF hostname and run.app; direct GCS still 200.

## Frontend switchover

`BUCKET_URL` is build-time inlined (Next env block → `frontend/src/constants.tsx`),
injected by `docs/superpowers/rig/deploy/deploy.sh` as a Docker build arg. Changes:

- `deploy.sh`: new `BLOB_DOMAIN` config var
  (`blobs-statbotics.iterativerefinement.com`); frontend build arg
  `BUCKET_URL=https://$BLOB_DOMAIN` (was the storage.googleapis.com URL);
  `step_dns` now also creates the blob worker + DNS record + route.
- No `staging` branch commit was needed: the branch carries only the env
  override *mechanism* (commit `5bc08aa`); the *value* lives in the deploy
  config, which is where it changed.
- Rebuilt + redeployed `statbotics-web` only (see staging.md redeploy log).

## Maintainer deliverable

`docs/superpowers/deliverables/cloudflare-bucket-proxy.md` — dashboard-only
(no API) how-to for putting prod's `site_v1` behind `blobs.statbotics.io`:
zone onboarding, Worker editor code (genericized), custom-domain binding,
expected cache behavior (per-PoP warming for event audiences), curl
verification checklist, rollback. Free plan only.

## Rollback

- Flip `BUCKET_URL` back to `https://storage.googleapis.com/statbotics-staging-site`
  in `deploy.sh` and rebuild/redeploy `statbotics-web`, or
- Grey-cloud / delete the `blobs-statbotics` AAAA record, the route, and the
  `statbotics-blob-proxy` worker (Cloudflare API or dashboard).
