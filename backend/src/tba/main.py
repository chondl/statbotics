import os
from typing import Any, Optional, Tuple, Union

from requests import Session

from src.tba import cache as tba_cache
from src.tba.constants import AUTH_KEY
from src.tba.utils import dump_cache, load_cache

read_prefix = "https://www.thebluealliance.com/api/v3/"

# Local pickle-cache root. Resolved once at import; override via TBA_CACHE_DIR.
TBA_CACHE_DIR = os.getenv("TBA_CACHE_DIR", "/tmp/tba-cache")

# TBA_AUTH_KEY (from Secret Manager on staging) overrides the hardcoded public
# key, keeping the real key out of source. Falls back to AUTH_KEY when unset.
session = Session()
session.headers.update(
    {"X-TBA-Auth-Key": os.getenv("TBA_AUTH_KEY") or AUTH_KEY, "X-TBA-Auth-Id": ""}
)


def _get_tba(
    url: str, etag: Optional[str] = None
) -> Tuple[Union[Any, bool], Optional[str]]:
    # Conditional headers are per-request: requests merges them with the
    # session headers for this call only, so the shared session is never
    # left carrying a stale If-None-Match.
    headers = {} if etag is None else {"If-None-Match": etag}
    response = session.get(read_prefix + url, headers=headers)
    if etag is not None and response.status_code == 304:
        return True, etag
    if response.status_code == 200:
        return response.json(), response.headers.get("ETag")
    return False, None


def get_tba(
    url: str, etag: Optional[str] = None, cache: bool = True
) -> Tuple[Union[Any, bool], Optional[str]]:
    cache_path = os.path.join(TBA_CACHE_DIR, url)
    has_pickle = os.path.exists(cache_path + "/data.p")
    if cache and has_pickle:
        # Cache Hit: no network, no manifest change
        return load_cache(cache_path), None

    # Manifest-backed conditional GET (design §2.2): when the caller passes
    # no etag and the cached pickle exists to satisfy a 304, revalidate with
    # the stored etag instead of refetching unconditionally. Explicit-etag
    # callers (partial cycles) are untouched — dual-write with objs[5]/DB.
    send_etag = etag
    from_manifest = False
    if etag is None and has_pickle:
        send_etag = tba_cache.stored_etag(url)
        from_manifest = send_etag is not None

    data, new_etag = _get_tba(url, send_etag)

    # Either Etag (304) or Invalid
    if type(data) is bool:
        if data is True:
            tba_cache.record_not_modified(url, new_etag)
            if from_manifest:
                # The caller sent no etag and expects data; serve the pickle
                # the 304 just validated.
                return load_cache(cache_path), new_etag
        return data, new_etag

    # Cache Miss: rewrite pickle + manifest etag state
    dump_cache(cache_path, data)
    tba_cache.record_success(url, new_etag)
    return data, new_etag
