# Serving the statbotics blob bucket through Cloudflare

This is a how-to for putting your public GCS site bucket (`site_v1`) behind a
Cloudflare-proxied hostname such as `blobs.statbotics.io`, so the browser reads
blobs through Cloudflare's edge cache instead of hitting `storage.googleapis.com`
directly. It uses the Cloudflare **dashboard** (web UI) throughout — no API
tokens, no command line except the verification checks at the end. Everything
here works on Cloudflare's **free plan**.

## Why do this

The frontend fetches a small `manifest.json` (once a minute per visitor) and then
a set of immutable, content-addressed blobs (`v2/<name>.<hash>`). Today every one
of those reads goes straight to Google Cloud Storage, and every byte is GCS
egress you pay for.

Cloudflare has a point of presence (PoP) physically near most users. During an
event, the people watching your site — teams, scouts, spectators in the same
venue and region — resolve to the **same PoP**. The first visitor to request a
given immutable blob pulls it from GCS once and warms that PoP's cache; every
visitor after that is served from Cloudflare, never touching GCS. You pay GCS
egress roughly once per blob per PoP per cache lifetime instead of once per
visitor. Because the blobs are immutable (a new version gets a new hash, hence a
new URL), they can sit in the edge cache for a year with no risk of going stale.

## What you'll build

```
browser ──▶ blobs.statbotics.io ──▶ [Cloudflare Worker + edge cache] ──▶ storage.googleapis.com/site_v1/<path>
```

- A **proxied DNS record** for `blobs.statbotics.io` (orange cloud).
- A tiny **Worker** that rewrites the path onto your bucket and lets Cloudflare
  cache each object according to the `Cache-Control` header GCS already sets.
- A one-line **config change** in the frontend so it reads from the new hostname.

## Prerequisites

- You own `statbotics.io` and can change its nameservers or DNS.
- Your GCS bucket (`site_v1`) is public-read (it already is — the site fetches
  from it in the browser today).
- A free Cloudflare account.

---

## Step 1 — Put `statbotics.io` on Cloudflare (skip if it already is)

Cloudflare has to be answering DNS for the zone before it can proxy anything.

1. Sign in at <https://dash.cloudflare.com>.
2. **Add a site** → enter `statbotics.io` → choose the **Free** plan.
3. Cloudflare scans your existing DNS and shows the records it found. Confirm your
   apex/`www` records are correct so the main site keeps working.
4. Cloudflare gives you **two nameservers**. Set these at your domain registrar
   (where you bought `statbotics.io`), replacing the current ones.
5. Wait for the zone status to flip to **Active** (Cloudflare emails you). Until
   it's Active, nothing below will take effect.

If `statbotics.io` is already on Cloudflare, go straight to Step 2.

---

## Step 2 — Create the Worker

1. In the dashboard sidebar: **Workers & Pages** → **Create** → **Create Worker**.
2. Name it `statbotics-blob-proxy`. Click **Deploy** (it deploys the default
   hello-world; you'll replace it next).
3. Click **Edit code**.
4. Delete the sample code and paste this in. **Change one line** — set `BUCKET`
   to your bucket name:

   ```js
   export default {
     async fetch(request) {
       const BUCKET = "site_v1"; // ← your GCS bucket name
       const ORIGIN = "https://storage.googleapis.com";

       const url = new URL(request.url);
       // Map blobs.statbotics.io/<path> → storage.googleapis.com/<BUCKET>/<path>
       const originUrl = ORIGIN + "/" + BUCKET + url.pathname + url.search;

       // Forward the visitor's request (it carries If-None-Match, so ETag
       // revalidation / 304s work) with the Host set to GCS by the new URL.
       const originReq = new Request(originUrl, request);

       // Edge-cache everything, honoring the Cache-Control GCS already sets:
       // ~1 year for the immutable v2/<name>.<hash> blobs, and max-age=60 for
       // the short-lived manifest.json. For the manifest we also rewrite the
       // client-facing Cache-Control back to max-age=60 — see the note below.
       const res = await fetch(originReq, { cf: { cacheEverything: true } });
       if (url.pathname === "/manifest.json") {
         const out = new Response(res.body, res);
         out.headers.set("Cache-Control", "public, max-age=60");
         return out;
       }
       return res;
     },
   };
   ```

5. Click **Deploy**.

That's the whole Worker. It rewrites the path onto your bucket, forwards the
conditional-request headers so revalidation keeps working, tells Cloudflare to
cache everything according to the origin's own `Cache-Control`, and re-asserts a
60-second browser TTL on the manifest.

> **Why `cacheEverything`?** Your blobs have no file extension and a generic
> content type, so Cloudflare's default rules wouldn't cache them.
> `cacheEverything: true` tells Cloudflare to cache them anyway, honoring the
> `max-age` GCS sends — a year for the immutable blobs, 60 seconds for the
> manifest. Edge-caching the manifest too means co-located event visitors share
> one manifest fetch per Cloudflare PoP instead of each paying a round trip to
> GCS.
>
> **Why rewrite the manifest's `Cache-Control`?** Some zones apply a **Browser
> Cache TTL** floor (e.g. 4 hours) that raises the *client-facing* `max-age` of
> any edge-cached object to the floor — which would make browsers hold a stale
> manifest for hours. Setting the header on the Worker's own returned `Response`
> wins over that floor, so browsers keep revalidating the manifest every 60
> seconds while the edge still serves it from cache. If your zone has no such
> floor (the default), the rewrite is a harmless no-op.

---

## Step 3 — Attach the hostname

Give the Worker its public hostname. Using a **Custom Domain** is the simplest
path: Cloudflare creates the proxied DNS record and the route for you.

1. Still in **Workers & Pages**, open your `statbotics-blob-proxy` Worker.
2. **Settings** → **Domains & Routes** → **Add** → **Custom domain**.
3. Enter `blobs.statbotics.io` → **Add domain**.

Cloudflare adds a **proxied** (orange-cloud) DNS record for `blobs` and points it
at the Worker. Give it a minute, then confirm under **DNS → Records** that
`blobs` shows an orange cloud. (If you'd rather do it by hand: create an `AAAA`
record `blobs` → `100::`, **Proxied**, then under the Worker add a **Route**
`blobs.statbotics.io/*`. The Custom Domain route above does both for you.)

---

## Step 4 — Point the site at the new hostname

The frontend reads blobs from a base URL called `BUCKET_URL`, currently
`https://storage.googleapis.com/site_v1`. Change it to the proxied hostname:

```
BUCKET_URL = https://blobs.statbotics.io
```

`BUCKET_URL` is **inlined at build time** (it's baked into the JavaScript bundle
during `next build`, via the Next.js `env` block), so changing it means a
**rebuild + redeploy** of the frontend, not just a config flip. Set the new value
wherever your build gets it (the Vercel/Cloud Run env var or build arg) and
redeploy the frontend. No backend change is needed — the backend keeps writing to
the same bucket.

Before you point real traffic at it, run the checks in the next section against
`blobs.statbotics.io` directly.

---

## What to expect from the cache

- **First request for a blob at a PoP:** `CF-Cache-Status: MISS` — Cloudflare
  fetches it from GCS once and stores it.
- **Every later request for that blob at that PoP:** `CF-Cache-Status: HIT`,
  served from the edge, no GCS egress. Because the blob URL is content-addressed
  and immutable, this HIT can last up to a year.
- **The manifest** IS edge-cached, but only for ~60 seconds (its origin
  `max-age`). Repeat requests within that window are served from the edge
  (`CF-Cache-Status: HIT`, `Age` climbing); once the edge copy expires the
  Worker revalidates it against GCS with the `ETag` (`304` if unchanged, new
  bytes when a cycle publishes). Browsers, seeing the Worker-asserted
  `max-age=60`, revalidate every 60 seconds on their own. Net staleness stays
  bounded at roughly 60 s edge + 60 s browser, and — unlike the immutable
  blobs — the manifest is small (a single gzip fetch), so edge-caching it mainly
  removes an origin round trip from the start of every cold page load rather than
  saving egress.
  - *Note:* if your `manifest.json` is stored with `Content-Encoding: gzip`,
    plain `curl` probes (which don't send `Accept-Encoding: gzip`) get a
    GCS-transcoded body with a weak ETag, and GCS answers their revalidations
    with `200` instead of `304`. Browsers always send `Accept-Encoding: gzip`,
    get the strong ETag, and revalidate to `304` normally — add `--compressed`
    to curl checks to match browser behavior.
- **Cache warming is per-PoP.** A cold PoP (a region no one has visited yet) pays
  one MISS per blob; after that it's warm. Event audiences clustered on one PoP
  share that warming, which is exactly the case you care about.

---

## Verification checklist

Run these from a terminal once Steps 2–3 are live (before or right after the
frontend redeploy). Replace the sample blob path with a real
`v2/<name>.<hash>` from your manifest.

```bash
HOST=https://blobs.statbotics.io

# 1. Blob is served and byte-identical to GCS
curl -s "$HOST/v2/event/2026arc.<hash>" | shasum
curl -s "https://storage.googleapis.com/site_v1/v2/event/2026arc.<hash>" | shasum
#   → the two hashes match

# 2. Immutable blob caches at the edge: MISS then HIT
curl -sI "$HOST/v2/event/2026arc.<hash>" | grep -i cf-cache-status   # MISS (first time)
curl -sI "$HOST/v2/event/2026arc.<hash>" | grep -i cf-cache-status   # HIT  (repeat)
#   → also check: cache-control: public, max-age=31536000, immutable

# 3. Manifest edge-caches for ~60s but stays browser-fresh (repeat within 60s)
curl -sI --compressed "$HOST/manifest.json" | grep -iE 'cache-control|cf-cache-status|^age'
curl -sI --compressed "$HOST/manifest.json" | grep -iE 'cache-control|cf-cache-status|^age'
#   → cache-control: public, max-age=60   and   cf-cache-status: HIT (age advancing)
#     (first fetch may be MISS/REVALIDATED; browsers still revalidate every 60s)

# 4. ETag revalidation returns 304 (use a real strong ETag, e.g. from a v2 blob)
ETAG=$(curl -sI "$HOST/v2/event/2026arc.<hash>" | awk -F': ' 'tolower($1)=="etag"{print $2}' | tr -d '\r')
curl -s -o /dev/null -w "%{http_code}\n" -H "If-None-Match: $ETAG" "$HOST/v2/event/2026arc.<hash>"
#   → 304
```

If all four pass, point `BUCKET_URL` at `blobs.statbotics.io` and redeploy the
frontend (Step 4). Load the site and open the browser **Network** tab: blob
requests should now go to `blobs.statbotics.io`, and a reload should show them
served from the Cloudflare cache (`cf-cache-status: HIT`).

---

## Rollback

Nothing here touches the bucket or the backend, so rollback is trivial and
instant:

- **Fastest:** set `BUCKET_URL` back to `https://storage.googleapis.com/site_v1`
  and redeploy the frontend. The site reads straight from GCS again; the Worker
  and DNS record can stay (they're just unused).
- **Or** turn off the proxy without redeploying: **DNS → Records** → edit the
  `blobs` record → click the orange cloud to make it **grey** (DNS-only). Traffic
  stops flowing through the Worker. (Grey-cloud won't serve the bucket by itself,
  so pair this with the `BUCKET_URL` revert — it's mainly a quick "stop
  proxying" switch.)
- **Full removal:** delete the `statbotics-blob-proxy` Worker and the `blobs`
  DNS record under **DNS → Records**.

---

## Optional: API rate limiting at the edge

If the API hostname is also proxied through Cloudflare (the same pattern as the
blob hostname, pointed at the App Engine/Cloud Run origin), Cloudflare can
rate-limit `/v3` scrapers before they ever reach your servers. This is included
in the **free plan**: one rate limiting rule, counted per IP over a fixed
10-second window, with a 10-second block.

Dashboard path: **Security → WAF → Rate limiting rules → Create rule**:

- **If incoming requests match:** Hostname equals `api.statbotics.io` AND URI
  Path starts with `/v3`
- **Rate:** e.g. 60 requests per 10 seconds (per IP — that is 6 req/s sustained
  per client, generous for humans, restrictive for scrapers)
- **Then:** Block for 10 seconds (the free-plan fixed duration; blocked clients
  receive HTTP 429)

Verified on the staging deployment with exactly this configuration: 100
parallel requests from one IP → 78 served, 22 blocked with `429`, and normal
service resumed automatically after the 10-second window. Legitimate browsing
never comes close to the threshold (a full page view is a handful of requests),
and the site itself is unaffected because pages are served from the blob
hostname, not the API.

This complements (not replaces) application-level API keys: the edge rule stops
volumetric abuse cheaply, while keys handle per-consumer quotas and
attribution.
