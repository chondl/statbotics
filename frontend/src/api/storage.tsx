import { get, set } from "idb-keyval";
import pako from "pako";

import { BACKEND_URL, BUCKET_URL, DISABLE_GCS } from "../constants";
import { log, round } from "../utils";

export const version = "v4";

const MANIFEST_TTL_MS = 60 * 1000;

interface Manifest {
  schema?: number;
  cycle?: string;
  hist_epoch?: number;
  blobs?: { [logicalPath: string]: string };
}

let manifestPromise: { promise: Promise<Manifest | null>; fetchedAt: number } | null = null;

async function fetchManifest(): Promise<Manifest | null> {
  try {
    const res = await fetch(`${BUCKET_URL}/manifest.json`, { next: { revalidate: 0 } });
    if (res.ok) {
      return (await res.json()) as Manifest;
    }
  } catch (e) {
    log("manifest fetch failed", e);
  }
  return null;
}

async function getManifest(): Promise<Manifest | null> {
  if (DISABLE_GCS) return null;
  const now = Date.now();
  if (!manifestPromise || now - manifestPromise.fetchedAt > MANIFEST_TTL_MS) {
    const promise = fetchManifest();
    manifestPromise = { promise, fetchedAt: now };
    // Don't pin every client to the uncached legacy path for a full TTL after one
    // blipped fetch: drop a null result from the cache so the next call retries.
    promise.then((manifest) => {
      if (manifest === null && manifestPromise?.promise === promise) {
        manifestPromise = null;
      }
    });
  }
  return manifestPromise.promise;
}

function toLogicalPath(apiPath: string): string {
  return apiPath.replace(/[?&]/g, ".").replace(/^\//, "");
}

function resolveBucketUrl(logicalPath: string, manifest: Manifest | null): string {
  if (manifest) {
    const versioned = manifest.blobs?.[logicalPath];
    if (versioned) {
      return `${BUCKET_URL}/${versioned}`;
    }
    if (manifest.hist_epoch != null) {
      return `${BUCKET_URL}/hist/${manifest.hist_epoch}/${logicalPath}`;
    }
  }
  return `${BUCKET_URL}/${logicalPath}?t=${Date.now() / 1000 / 60}`;
}

async function setWithExpiry(key: string, value: any, ttl: number) {
  const now = new Date();

  try {
    await set(`${key}_expiry`, now.getTime() + 1000 * ttl);
    await set(key, value);
  } catch (e: any) {
    log("Error setting", e);
  }
}

async function getWithExpiry(key: string) {
  const expiry = await get(`${key}_expiry`);
  if (!expiry) {
    return null;
  }
  const now = new Date();
  if (now.getTime() > expiry) {
    return null;
  }
  return get(key);
}

async function getStale(key: string) {
  const expiry = await get(`${key}_expiry`);
  if (!expiry) {
    return null;
  }
  return get(key);
}

export function decompress(buffer: any) {
  const strData = pako.inflate(buffer, { to: "string" });
  const data = JSON.parse(strData);
  return data;
}

const bucketInFlight: { [logicalPath: string]: Promise<any> } = {};

async function fetchBucketDataImpl(logicalPath: string): Promise<any> {
  const manifest = await getManifest();
  const url = resolveBucketUrl(logicalPath, manifest);
  const res = await fetch(url, { next: { revalidate: 0 } });
  if (!res.ok) {
    throw new Error(`Failed to fetch from bucket: ${res.status}`);
  }
  return decompress(await res.arrayBuffer());
}

export async function fetchBucketData(logicalPath: string): Promise<any> {
  if (!bucketInFlight[logicalPath]) {
    bucketInFlight[logicalPath] = fetchBucketDataImpl(logicalPath).finally(() => {
      delete bucketInFlight[logicalPath];
    });
  }
  return bucketInFlight[logicalPath];
}

const inFlight: { [storageKey: string]: Promise<any> } = {};

async function fetchAndStore(
  storageKey: string,
  apiPath: string,
  checkBucket: boolean,
  expiry: number
) {
  const start = performance.now();

  let buffer = null;
  try {
    if (!checkBucket || DISABLE_GCS) {
      throw new Error("Skip bucket check");
    }
    const logicalPath = toLogicalPath(apiPath);
    const manifest = await getManifest();
    const url = resolveBucketUrl(logicalPath, manifest);
    const res = await fetch(url, { next: { revalidate: 0 } });
    log(`${logicalPath} (bucket) took ${round(performance.now() - start, 0)}ms`);
    if (res.ok) {
      buffer = decompress(await res.arrayBuffer());
    } else {
      throw new Error(`Failed to fetch from bucket: ${res.status}`);
    }
  } catch (e) {
    try {
      const res = await fetch(`${BACKEND_URL}${apiPath}`, { next: { revalidate: 0 } });
      log(`${apiPath} (backend) took ${round(performance.now() - start, 0)}ms`);
      if (res.ok) {
        buffer = await res.json();
      }
    } catch (apiErr) {
      log(`${apiPath} (backend) failed`, apiErr);
    }
  }

  if (buffer) {
    await setWithExpiry(storageKey, buffer, expiry);
    return buffer;
  }
}

async function query(
  storageKey: string,
  apiPath: string,
  checkBucket: boolean,
  minLength: number,
  expiry: number
) {
  const cacheData = await getWithExpiry(storageKey);
  if (cacheData && (minLength === 0 || cacheData?.length > minLength)) {
    log(`Used Local Storage: ${storageKey}`);
    return cacheData;
  }

  if (!inFlight[storageKey]) {
    inFlight[storageKey] = fetchAndStore(storageKey, apiPath, checkBucket, expiry).finally(() => {
      delete inFlight[storageKey];
    });
  }

  const buffer = await inFlight[storageKey];
  if (buffer) {
    return buffer;
  }

  const stale = await getStale(storageKey);
  if (stale && (minLength === 0 || stale?.length > minLength)) {
    log(`Served stale cache: ${storageKey}`);
    return stale;
  }
}

export default query;
