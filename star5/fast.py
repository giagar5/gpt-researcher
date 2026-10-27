"""Veloce mode: a Perplexity-style answer with sources in about 30-60 seconds.

One quick planning call on the `fast` alias writes 2-3 short web queries; Brave runs them in
parallel; the best pages are read with a hard deadline; one `frontier` call writes a concise,
cited answer (reasoning effort low unless the user chose another). No GPT Researcher agent,
sub-topics or embeddings, which is where the standard mode spends its minutes.
"""

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import socket
import time
from datetime import date
from urllib.parse import urlsplit

import httpx

from .effort import reasoning

LOG = logging.getLogger("star5")

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
USER_AGENT = "Mozilla/5.0 (compatible; Star5-Search/1.0; +https://search.star5.org)"
PLANNER_MODEL = "fast"
MAX_QUERIES = 3
FETCH_DEADLINE = 8.0     # seconds for reading pages, all in parallel
PAGE_CHARS = 3500        # text kept per page
PAGE_BYTES = 3 * 1024 * 1024
SNIPPET_CHARS = 700
LISTED_RESULTS = 16      # search results shown to the writer
BRAVE_MAX_CHARS, BRAVE_MAX_WORDS = 380, 45   # Brave answers 422 above 400 characters / 50 words

PLAN_PROMPT = """You plan web searches for a research assistant. Today is {today}.
Write 2 or 3 short web-search queries (at most 10 words each) that together find what is needed to
answer the user's question. Rules:
- Local questions ("near me", "here", a town): put the town in the query and write it in the
  language spoken in that place.
- When attached files are described, search for the specific things they show or contain.
- For follow-ups, make every query self-contained.
Reply with JSON only: {{"queries": ["...", "..."]}}"""

ANSWER_PROMPT = """You are Star5 Search. Answer the user's question in {language}. Today is {today}.
Use the numbered web sources below; they are your only source of facts besides the user's files.
- Start with the direct answer in one to three sentences, then only the essential details.
- When the user wants people, places, products or services, give a short list of specific named
  options with why each fits and the address, contact or price when the sources give them.
- Cite every factual claim with a Markdown link to its source, like [Doctolib](https://...), using
  only the URLs listed. Never invent a URL or a fact.
- Be concise: usually 150-400 words. Use a small table only to compare options.
- If the sources do not answer the question, say so plainly and say what is missing.
- The user's location (when given) is for local questions only; never print coordinates."""


# ---------------------------------------------------------------- queries

def short_query(query, chars=BRAVE_MAX_CHARS, words=BRAVE_MAX_WORDS):
    """A query Brave accepts: long texts (a whole research brief) keep their first line only."""
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if len(text) <= chars and len(text.split()) <= words:
        return text
    first = next((line.strip() for line in str(query).splitlines() if line.strip()), text)
    first = re.sub(r"^(question|domanda|requête|query)\s*:\s*", "", first, flags=re.I)
    out = " ".join(first.split()[:words])
    if len(out) > chars:
        out = out[:chars].rsplit(" ", 1)[0]
    return out


def install_brave_guard():
    """GPT Researcher's Brave retriever: short queries (its first search used the whole brief), and
    DuckDuckGo when Brave returns nothing (usage limit, key or network errors)."""
    try:
        from gpt_researcher.retrievers.brave.brave import BraveSearch
    except ImportError:  # pragma: no cover
        return
    if getattr(BraveSearch, "_star5_guard", False):
        return
    original_init, original_search = BraveSearch.__init__, BraveSearch.search

    def __init__(self, query, *args, **kwargs):
        original_init(self, short_query(query), *args, **kwargs)

    def search(self, max_results=7):
        results = original_search(self, max_results)
        if results:
            return results
        try:
            items = _ddg_text(self.query, max(3, min(int(max_results), 20)))
        except Exception as error:
            LOG.warning("Brave and DuckDuckGo both failed for %r: %s", self.query, error)
            return []
        # Same shape as GPT Researcher's Duckduckgo retriever: a short body means "scrape the page".
        results = [{"href": item["href"], "body": (item.get("body") or "")[:100], "title": item.get("title") or ""}
                   for item in items if item.get("href")]
        if results:
            LOG.warning("Brave gave nothing for %r; used DuckDuckGo", self.query)
        return results

    BraveSearch.__init__ = __init__
    BraveSearch.search = search
    BraveSearch._star5_guard = True


def parse_queries(text, question):
    queries = []
    match = re.search(r"\{.*\}", text or "", re.S)
    if match:
        try:
            items = json.loads(match.group(0)).get("queries") or []
        except (ValueError, AttributeError):
            items = []
        for item in items:
            if isinstance(item, str):
                query = short_query(item)
                if query and query.lower() not in (q.lower() for q in queries):
                    queries.append(query)
    return queries[:MAX_QUERIES] or [short_query(question)]


async def plan_queries(question, earlier, extra, config_path):
    from gpt_researcher.config import Config
    from gpt_researcher.utils.llm import create_chat_completion

    cfg = Config(config_path)
    user = question
    if earlier:
        user += f"\n\nEarlier question in this conversation: {earlier}"
    if extra:
        user += f"\n\n{extra[:3000]}"
    try:
        # No reasoning for the planner: 2-3 short queries do not need it (gpt-6-luna accepts "none").
        with reasoning(""):
            text = await create_chat_completion(
                model=PLANNER_MODEL, llm_provider=cfg.smart_llm_provider,
                llm_kwargs={**(cfg.llm_kwargs or {}), "reasoning_effort": "none"},
                messages=[{"role": "system", "content": PLAN_PROMPT.format(today=date.today().isoformat())},
                          {"role": "user", "content": user}],
                temperature=None, max_tokens=2000)
    except Exception:
        LOG.exception("Veloce: query planning failed; searching the question as is")
        text = ""
    return parse_queries(text, question)


# ---------------------------------------------------------------- search and read

def clean_html_text(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


async def brave(client, key, query, count):
    params = {"q": query, "count": max(3, min(int(count), 20)), "extra_snippets": "true"}
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    for attempt in range(3):
        try:
            response = await client.get(BRAVE_URL, params=params, headers=headers)
        except httpx.HTTPError as error:
            LOG.warning("Veloce: Brave request failed for %r: %s", query, error)
            return []
        if response.status_code == 429 and attempt < 2:
            await asyncio.sleep(1.1 * (attempt + 1))
            continue
        if response.status_code != 200:
            LOG.warning("Veloce: Brave answered %s for %r", response.status_code, query)
            return []
        break
    try:
        results = ((response.json().get("web") or {}).get("results")) or []
    except ValueError:
        return []
    found = []
    for item in results:
        url = item.get("url") if isinstance(item, dict) else None
        if not url or urlsplit(url).scheme not in ("http", "https"):
            continue
        snippet = " … ".join(clean_html_text(s) for s in [item.get("description")] + (item.get("extra_snippets") or [])
                             if isinstance(s, str) and s.strip())
        found.append({"url": url, "title": clean_html_text(item.get("title"))[:200],
                      "snippet": snippet[:SNIPPET_CHARS], "age": str(item.get("age") or "")[:40]})
    return found


AD_HOSTS = ("bing.com", "duckduckgo.com")
# Free scrapers fail often ("No results" when rate-limited, a Wikipedia backend that fails the whole
# call for region wt-wt): alternate two configurations, a few seconds at most.
DDG_ATTEMPTS = (("wt-wt", "duckduckgo"), ("us-en", "auto"), ("wt-wt", "duckduckgo"))


def _ddg_text(query, count):
    """Best-effort DuckDuckGo results with ad links dropped; raises the last error if every attempt fails."""
    from ddgs import DDGS
    error = None
    for attempt, (region, backend) in enumerate(DDG_ATTEMPTS):
        if attempt == len(DDG_ATTEMPTS) - 1:
            time.sleep(1.5)
        try:
            items = DDGS().text(query, region=region, backend=backend, max_results=count) or []
        except Exception as exc:
            error = exc
            continue
        items = [item for item in items if isinstance(item, dict)
                 and not (urlsplit(item.get("href") or "").hostname or "").endswith(AD_HOSTS)]
        if items:
            return items
    if error is not None:
        raise error
    return []


async def duckduckgo(query, count, search_fn=_ddg_text):
    """Backup search when Brave is unavailable (no key, usage limit, errors): ddgs, unofficial and keyless."""
    try:
        items = await asyncio.wait_for(asyncio.to_thread(search_fn, query, max(3, min(int(count), 20))), 12)
    except Exception as error:
        LOG.warning("Veloce: DuckDuckGo failed for %r: %s", query, error)
        return []
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("href") or item.get("url") or item.get("link") or ""
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        snippet = item.get("body") or item.get("snippet") or item.get("description") or ""
        found.append({"url": url, "title": clean_html_text(item.get("title"))[:200],
                      "snippet": clean_html_text(snippet)[:SNIPPET_CHARS], "age": ""})
    return found


async def web_search(client, key, query, count, backup=duckduckgo):
    results = await brave(client, key, query, count) if key else []
    if not results and backup is not None:
        results = await backup(query, count)
        if results:
            LOG.warning("Veloce: Brave gave nothing for %r; used DuckDuckGo", query)
    return results


def interleave(lists):
    """Round-robin over the queries' results, first occurrence of each URL wins."""
    merged, seen = [], set()
    for index in range(max((len(items) for items in lists), default=0)):
        for items in lists:
            if index < len(items):
                key = items[index]["url"].split("#")[0].rstrip("/")
                if key not in seen:
                    seen.add(key)
                    merged.append(items[index])
    return merged


_public_cache = {}


async def is_public(host):
    """Only public internet hosts may be fetched (no LAN, container bridge or loopback)."""
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".local", ".lan", ".internal", ".home.arpa")):
        return False
    if host in _public_cache:
        return _public_cache[host]
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return False
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    public = bool(addresses) and all(a.is_global for a in addresses)
    if len(_public_cache) > 2000:
        _public_cache.clear()
    _public_cache[host] = public
    return public


async def _guard_request(request):
    if request.url.scheme not in ("http", "https") or not await is_public(request.url.host):
        raise httpx.RequestError(f"blocked non-public address {request.url.host}", request=request)


def extract_text(body, encoding, is_html, limit=PAGE_CHARS):
    if not is_html:
        text = body.decode(encoding or "utf-8", "replace")
    else:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(body, "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe"]):
            tag.decompose()
        root = soup.find("main") or soup.find("article") or soup.body or soup
        text = root.get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if len(line) > 2)
    return text[:limit]


async def fetch_text(client, url):
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            return ""
        ctype = response.headers.get("content-type", "").lower()
        is_html = "html" in ctype
        if not is_html and not ctype.startswith("text/plain"):
            return ""
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > PAGE_BYTES:
                break
        encoding = response.charset_encoding or "utf-8"
    return await asyncio.to_thread(extract_text, bytes(body), encoding, is_html)


# ---------------------------------------------------------------- answer

def sources_block(results):
    parts = []
    for number, item in enumerate(results[:LISTED_RESULTS], 1):
        lines = [f"[{number}] {item['title'] or item['url']}", f"URL: {item['url']}"]
        if item.get("age"):
            lines.append(f"Date: {item['age']}")
        if item.get("snippet"):
            lines.append(f"Snippet: {item['snippet']}")
        if item.get("text"):
            lines.append(f"Page text:\n{item['text']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def write_answer(question, language, earlier, files_text, location, results, config_path):
    from gpt_researcher.config import Config
    from gpt_researcher.utils.llm import create_chat_completion

    cfg = Config(config_path)
    user = f"Question: {question}"
    if earlier:
        user += f"\n(Follow-up to the earlier question: {earlier})"
    if location:
        user += f"\n\n{location}"
    if files_text:
        user += f"\n\nFiles attached by the user (their content, as text):\n{files_text[:30000]}"
    user += f"\n\nWeb sources:\n\n{sources_block(results)}"
    return await create_chat_completion(
        model=cfg.smart_llm_model, llm_provider=cfg.smart_llm_provider, llm_kwargs=cfg.llm_kwargs,
        messages=[{"role": "system", "content": ANSWER_PROMPT.format(language=language, today=date.today().isoformat())},
                  {"role": "user", "content": user}],
        temperature=None, max_tokens=cfg.smart_token_limit)


async def report_files(answer, question):
    from server.server_utils import generate_report_files, sanitize_filename
    files = await generate_report_files(answer, sanitize_filename(f"task_{int(time.time())}_{question[:80]}"))
    return {k: v for k, v in files.items() if v}


async def log(sink, content, output, **extra):
    await sink.send_json({"type": "logs", "content": content, "output": output, **extra})


async def fast_research(question, language, earlier, extra, files_text, location, max_results, config_path, sink, *,
                        planner=plan_queries, writer=write_answer, files_fn=report_files, transport=None,
                        guard=_guard_request, backup=duckduckgo):
    key = os.environ.get("BRAVE_API_KEY", "")
    queries = await planner(question, earlier, extra, config_path)
    await log(sink, "subqueries", "Search plan: " + " | ".join(queries), metadata=queries)
    timeout = httpx.Timeout(FETCH_DEADLINE, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport,
                                 headers={"User-Agent": USER_AGENT, "Accept-Language": "*"},
                                 event_hooks={"request": [guard]} if guard else None) as client:
        for query in queries:
            await log(sink, "running_subquery_research", f"🔍 Running research for '{query}'...")
        lists = await asyncio.gather(*(web_search(client, key, q, max_results, backup) for q in queries))
        results = interleave(lists)
        if not results:
            raise RuntimeError("The web search returned no results")
        target = max(4, min(int(max_results), 8))
        candidates = [r for r in results if not urlsplit(r["url"]).path.lower().endswith(".pdf")][:target + 3]
        tasks = {asyncio.create_task(fetch_text(client, r["url"])): r for r in candidates}
        done, pending = await asyncio.wait(tasks, timeout=FETCH_DEADLINE) if tasks else (set(), set())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        read = []
        for task, item in tasks.items():
            if task in done and not task.cancelled() and task.exception() is None and task.result():
                item["text"] = task.result()
                read.append(item)
        read = read[:target]
    for item in read:
        await log(sink, "added_source_url", f"✅ Added source url to research: {item['url']}\n")
    await log(sink, "writing_report", "Writing the answer")
    answer = str(await writer(question, language, earlier, files_text, location, results, config_path)).strip()
    if not answer:
        raise RuntimeError("Empty answer from the model")
    await sink.send_json({"type": "report", "output": answer})
    try:
        files = await files_fn(answer, question)
    except Exception:
        LOG.exception("Veloce: report files failed")
        files = {}
    sources = [{"url": r["url"], "title": r["title"]} for r in results]
    return answer, sources, [r["url"] for r in read], files
