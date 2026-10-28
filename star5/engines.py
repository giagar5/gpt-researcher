"""Web search on several engines at once: Brave (API), Bing and DuckDuckGo (both through ddgs).

Every query goes to all engines in parallel, each with the turn's timeout (shorter in Veloce,
longer in Approfondita); results are merged in turn order (one per engine at a time) and
deduplicated, each keeping the list of engines that found it. A per-turn Stats object, held in a
context variable that GPT Researcher's worker threads inherit (asyncio.to_thread copies it), counts
for every engine the queries, failures, timeouts and the sources it contributed.
"""

import concurrent.futures
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx

LOG = logging.getLogger("star5")

ENGINES = ("brave", "bing", "duckduckgo")
MODE_TIMEOUTS = {"fast": 5.0, "quick": 8.0, "detailed": 8.0, "deep": 12.0, "chat": 6.0}
DEFAULT_TIMEOUT = 8.0
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SNIPPET_CHARS = 700
AD_HOSTS = ("bing.com", "duckduckgo.com", "msn.com/aclick")


def clean_text(value):
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def url_key(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}{'?' + parts.query if parts.query else ''}"


def is_ad(url):
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith(("bing.com", "duckduckgo.com")) or "/aclick" in url


# ---------------------------------------------------------------- per-turn statistics

class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.engines = {name: {"queries": 0, "ok": 0, "empty": 0, "timeouts": 0, "errors": 0, "results": 0}
                        for name in ENGINES}
        self.found = {}   # url key -> set of engines that returned it
        self.used = {}    # url key -> set of engines, only for results passed on to the pipeline

    def record(self, engine, status, items=()):
        with self.lock:
            counters = self.engines.setdefault(engine, {"queries": 0, "ok": 0, "empty": 0, "timeouts": 0,
                                                        "errors": 0, "results": 0})
            counters["queries"] += 1
            counters[status] += 1
            counters["results"] += len(items)
            for item in items:
                self.found.setdefault(url_key(item["url"]), set()).add(engine)

    def mark_used(self, items):
        with self.lock:
            for item in items:
                key = url_key(item["url"])
                self.used.setdefault(key, set()).update(item.get("engines") or self.found.get(key, ()))

    def engines_of(self, url):
        key = url_key(url)
        with self.lock:
            return sorted(self.used.get(key) or self.found.get(key) or (), key=ENGINES.index)

    def summary(self, read_urls=(), cited_urls=()):
        """Per engine: sources used (passed to the pipeline), read and cited, plus query outcomes."""
        read = {url_key(u) for u in read_urls}
        cited = {url_key(u) for u in cited_urls}
        with self.lock:
            out = {}
            for name in ENGINES:
                c = self.engines.get(name) or {}
                if not c.get("queries"):
                    continue
                used = {k for k, e in self.used.items() if name in e}
                out[name] = {"used": len(used), "read": len(used & read), "cited": len(used & cited),
                             "queries": c["queries"], "timeouts": c["timeouts"], "errors": c["errors"]}
            return out


class Session:
    def __init__(self, timeout):
        self.timeout = timeout
        self.stats = Stats()


_current = ContextVar("star5_search_session", default=None)


@contextmanager
def session(mode):
    current = Session(MODE_TIMEOUTS.get(mode, DEFAULT_TIMEOUT))
    token = _current.set(current)
    try:
        yield current
    finally:
        _current.reset(token)


def current():
    return _current.get()


# ---------------------------------------------------------------- engines (blocking, run in threads)

def brave(query, count, timeout):
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        raise LookupError("no key")
    params = {"q": query, "count": max(3, min(int(count), 20)), "extra_snippets": "true"}
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    deadline = time.monotonic() + timeout
    response = None
    for attempt in range(2):
        response = httpx.get(BRAVE_URL, params=params, headers=headers,
                             timeout=max(1.0, deadline - time.monotonic()))
        if response.status_code == 429 and attempt == 0 and deadline - time.monotonic() > 2:
            time.sleep(1.1)
            continue
        break
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    found = []
    for item in ((response.json().get("web") or {}).get("results")) or []:
        url = item.get("url") if isinstance(item, dict) else None
        if not url or urlsplit(url).scheme not in ("http", "https"):
            continue
        snippets = [item.get("description")] + (item.get("extra_snippets") or [])
        found.append({"url": url, "title": clean_text(item.get("title"))[:200],
                      "snippet": " … ".join(clean_text(s) for s in snippets if isinstance(s, str) and s.strip())
                      [:SNIPPET_CHARS], "age": str(item.get("age") or "")[:40]})
    return found


def _ddgs(query, count, timeout, backend, regions):
    """One ddgs backend; when it fails or finds nothing, one more try in another region if time allows
    (the free scrapers often answer "No results" to a single request)."""
    from ddgs import DDGS
    deadline = time.monotonic() + timeout
    items, error = [], None
    for region in regions:
        left = deadline - time.monotonic()
        if left < 1.5:
            break
        try:
            items = DDGS(timeout=max(1, int(left))).text(query, region=region, backend=backend,
                                                         max_results=max(3, min(int(count), 20))) or []
        except Exception as exc:
            error, items = exc, []
        if items:
            break
    if not items and error is not None:
        raise error
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("href") or item.get("url") or item.get("link") or ""
        if urlsplit(url).scheme not in ("http", "https") or is_ad(url):
            continue
        found.append({"url": url, "title": clean_text(item.get("title"))[:200],
                      "snippet": clean_text(item.get("body") or item.get("snippet") or "")[:SNIPPET_CHARS],
                      "age": ""})
    return found


def bing(query, count, timeout):
    # Not wt-wt: for that region ddgs also queries wt.wikipedia.org, which does not exist.
    return _ddgs(query, count, timeout, "bing", ("us-en", "uk-en"))


def duckduckgo(query, count, timeout):
    return _ddgs(query, count, timeout, "duckduckgo", ("wt-wt", "us-en"))


ENGINE_FUNCS = {"brave": brave, "bing": bing, "duckduckgo": duckduckgo}


def merge(lists_by_engine):
    """One result per engine at a time, first occurrence of each URL wins and collects its engines."""
    merged, index = [], {}
    names = [name for name in ENGINES if name in lists_by_engine]
    for position in range(max((len(v) for v in lists_by_engine.values()), default=0)):
        for name in names:
            items = lists_by_engine[name]
            if position >= len(items):
                continue
            item = items[position]
            key = url_key(item["url"])
            if key in index:
                engines = index[key]["engines"]
                if name not in engines:
                    engines.append(name)
                if not index[key]["snippet"] and item["snippet"]:
                    index[key]["snippet"] = item["snippet"]
                continue
            entry = {**item, "engines": [name]}
            index[key] = entry
            merged.append(entry)
    return merged


def search_all(query, count, timeout=None, stats=None, engines=None):
    """All engines in parallel; an engine that misses the deadline is left behind and counted."""
    active = current()
    timeout = timeout or (active.timeout if active else DEFAULT_TIMEOUT)
    stats = stats if stats is not None else (active.stats if active else None)
    names = [n for n in (engines or ENGINES) if n != "brave" or os.environ.get("BRAVE_API_KEY")]
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(names) or 1, thread_name_prefix="star5-search")
    futures = {pool.submit(ENGINE_FUNCS[name], query, count, timeout): name for name in names}
    done, pending = concurrent.futures.wait(futures, timeout=timeout)
    pool.shutdown(wait=False, cancel_futures=True)
    lists = {}
    for future, name in futures.items():
        if future in pending:
            status, items = "timeouts", []
            LOG.info("Search: %s timed out after %.0f s for %r", name, timeout, query)
        elif future.exception() is not None:
            status, items = "errors", []
            LOG.info("Search: %s failed for %r: %s", name, query, str(future.exception())[:160])
        else:
            items = future.result() or []
            status = "ok" if items else "empty"
        if stats is not None:
            stats.record(name, status, items)
        lists[name] = items
    return merge(lists)


def install_gpt_researcher():
    """GPT Researcher's configured retriever (Brave) now searches every engine; queries are shortened
    first because its opening search sends the whole research brief, which Brave rejects (HTTP 422)."""
    try:
        from gpt_researcher.retrievers.brave.brave import BraveSearch
    except ImportError:  # pragma: no cover
        return
    if getattr(BraveSearch, "_star5_multi", False):
        return
    from .fast import short_query
    original_init = BraveSearch.__init__

    def __init__(self, query, *args, **kwargs):
        original_init(self, short_query(query), *args, **kwargs)

    def search(self, max_results=7):
        results = search_all(self.query, max_results)
        # Cap the merged list so three engines do not triple the pages to scrape.
        results = results[:max(4, int(max_results) * 2)]
        active = current()
        if active:
            active.stats.mark_used(results)
        # GPT Researcher treats a body over 100 characters as already-fetched content: keep Brave's
        # descriptions as before and let pages found only by Bing or DuckDuckGo be scraped.
        return [{"href": r["url"], "title": r["title"],
                 "body": r["snippet"] if "brave" in r["engines"] else r["snippet"][:100]} for r in results]

    BraveSearch.__init__ = __init__
    BraveSearch.search = search
    BraveSearch._star5_multi = True
