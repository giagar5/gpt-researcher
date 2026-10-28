"""Star5 routes: the new UI on "/" and a small thread API under /api/s5.

The upstream web UI and its HTTP/WebSocket API are removed: only Star5 routes and the report
downloads under /outputs stay. Authentication stays in front of the app (nginx auth_request with
the owners' Google login).
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

from .effort import EFFORTS, install as install_effort
from . import engines
from .fast import fast_research
from .media import Geocoder, process as process_media
from .runner import LANGS, MODES, MAX_QUESTION, TONES, UI_LANGS, Runner, detect_lang, language_configs
from .store import Store
from .uploads import Uploads, clean_location

MAX_ATTACHMENTS = 10

LOG = logging.getLogger("star5")
STATIC = Path(__file__).resolve().parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
       "font-src 'self'; media-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
       "object-src 'none'")
NO_STORE = {"Cache-Control": "no-store"}
OUTPUTS = Path("outputs").resolve()


BRIEF_PROMPT = """You turn a user's question into a research brief for an autonomous web-research agent.
The agent plans its sub-queries and writes the final report from this brief alone, so the brief must
keep the user's real intent and must not drift to side topics.

Write the brief in {language}. Output only the brief: plain text, at most 220 words, these labelled lines:
Question: the user's question, verbatim.
Goal: what the user actually wants to obtain.
Deliverable: the concrete output. When the user asks to find, choose or compare people, professionals,
  businesses, products, places or services, the deliverable is a shortlist of specific named candidates
  (aim for 5 to 10 when they exist) with, for each one: name, where and how to contact or book, how it
  meets each key criterion, and the source; then a reasoned recommendation. Never turn such a request
  into a general explainer.
Key criteria: the criteria implied by the question, most important first (for example qualifications,
  specialisation, methods, experience with the specific problem, location, languages, availability,
  verifiable reputation).
Search hints: languages and local terms to search in, place names, useful directories or registries.
Secondary: topics the agent must NOT research separately (for example prices, reimbursement or
  regulation, unless the user asked about them); the report mentions them only in one short closing
  paragraph, using what it found anyway.
Report shape: the deliverable comes first and is the largest part; background and method take a few lines.
Attachments: when the user attached files, add a line "Attachments:" stating what they show or contain
  (key facts, exact text, product names) and how they relate to the question. When the question refers to
  them ("what is this", "find this", "this document"), they are the subject of the research.
Location: when the user's location is given, use the place name for anything local ("near me", "here",
  "around"), and name the town and region in the Search hints. Never include the coordinates.
Do not answer the question and do not invent facts."""


async def brief(question, language, previous, config_path, extra=""):
    from gpt_researcher.config import Config
    from gpt_researcher.utils.llm import create_chat_completion

    cfg = Config(config_path)
    user = question if not previous else f"{question}\n\n(This is a follow-up to the earlier question: {previous})"
    if extra:
        user += f"\n\n{extra}"
    return await create_chat_completion(
        model=cfg.smart_llm_model, llm_provider=cfg.smart_llm_provider, llm_kwargs=cfg.llm_kwargs,
        messages=[{"role": "system", "content": BRIEF_PROMPT.format(language=language)},
                  {"role": "user", "content": user}],
        temperature=None, max_tokens=cfg.smart_token_limit)


def hybrid_config(config_path, doc_path):
    """Copy of the language config whose DOC_PATH is this turn's attachments folder."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    config["DOC_PATH"] = str(doc_path)
    target = Path(doc_path).with_suffix(".json")
    target.write_text(json.dumps(config), encoding="utf-8")
    return str(target)


async def research(task, report_type, tone, max_results, config_path, sink, doc_path=None):
    from gpt_researcher.utils.enum import Tone
    from server.server_utils import generate_report_files, sanitize_filename
    from server.websocket_manager import run_agent

    source = "web"
    # Deep research always searches the web only (upstream); attachments reach it through the brief.
    if doc_path and report_type != "deep":
        source, config_path = "hybrid", hybrid_config(config_path, doc_path)
    started = time.time()
    try:
        report, researcher = await run_agent(
            task, report_type, source, [], [], Tone[tone], sink, headers={}, query_domains=[],
            config_path=config_path, return_researcher=True, max_search_results=max_results)
    finally:
        drop_event_logs(sanitize_filename(f"task_0_{task}").rsplit("_", 1)[-1], started)
    report = str(report)
    files = await generate_report_files(report, sanitize_filename(f"task_{int(time.time())}_{task[:80]}"))
    sources = [{"url": s.get("url"), "title": s.get("title")} for s in researcher.get_research_sources()
               if isinstance(s, dict)]
    return report, sources, list(researcher.visited_urls), {k: v for k, v in files.items() if v}


def drop_event_logs(task_hash, since):
    """run_agent always writes outputs/task_<time>_<hash>.json for the upstream UI; nothing reads it here."""
    for path in OUTPUTS.glob(f"task_*_{task_hash}.json"):
        try:
            if path.stat().st_mtime >= since - 5:
                path.unlink()
        except OSError:
            pass


def web_results(query):
    """Web search for the report chat: Brave, Bing and DuckDuckGo instead of the upstream Tavily-only tool."""
    hits = engines.search_all(query, 5)[:8]
    active = engines.current()
    if active:
        active.stats.mark_used(hits)
    return {"results": [{"title": h["title"], "url": h["url"], "content": h["snippet"]} for h in hits]}


async def chat(report, messages, config_path):
    from chat.chat import ChatAgentWithMemory

    class Star5Chat(ChatAgentWithMemory):
        def quick_search(self, query):
            try:
                results = web_results(query)
            except Exception as error:
                results = {"error": str(error), "results": []}
            self.search_metadata = self._build_search_metadata(query, results)
            return results

    agent = Star5Chat(report=report or "(no report yet)", config_path=config_path, headers=None)
    content, metadata = await agent.chat(messages, None)
    searched = []
    for call in metadata or []:
        searched += (call.get("search_metadata") or {}).get("sources") or []
    return str(content), searched


def clean_question(value):
    if not isinstance(value, str):
        raise HTTPException(400, "question")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
    if not value or len(value) > MAX_QUESTION:
        raise HTTPException(400, "question")
    return value


def options(body, default_results):
    mode = body.get("mode", "quick")
    tone = body.get("tone", "Objective")
    ui = body.get("ui", "it")
    lang = body.get("lang", "auto")
    results = body.get("max_results", default_results)
    if mode not in MODES or tone not in TONES or ui not in UI_LANGS or (lang != "auto" and lang not in LANGS):
        raise HTTPException(400, "options")
    if type(results) is not int or not 3 <= results <= 15:
        raise HTTPException(400, "max_results")
    return mode, tone, ui, lang, results


def effort_option(body):
    """"" (or absent) = the model's default; otherwise one of EFFORTS."""
    effort = body.get("effort") or ""
    if effort and effort not in EFFORTS:
        raise HTTPException(400, "effort")
    return effort


def install(app: FastAPI, *, data_dir=None, base_config=None, research_fn=research, chat_fn=chat, brief_fn=brief,
            media_fn=process_media, geocode_fn=None, fast_fn=fast_research):
    data_dir = Path(data_dir or os.environ.get("STAR5_DATA", "data"))
    base_config = base_config or os.environ.get("CONFIG_PATH")
    install_effort()
    engines.install_gpt_researcher()
    configs = language_configs(base_config, data_dir / "configs")
    default_results = json.loads(Path(base_config).read_text(encoding="utf-8")).get("MAX_SEARCH_RESULTS_PER_QUERY", 8)
    store = Store(data_dir / "star5.db")
    interrupted = store.fail_interrupted()
    if interrupted:
        LOG.warning("Marked %s interrupted Star5 turns as failed", interrupted)
    uploads = Uploads(store, data_dir / "uploads", media_fn, lambda lang: configs.get(lang, configs["it"]))
    docs_root = data_dir / "turn-docs"
    shutil.rmtree(docs_root, ignore_errors=True)
    runner = Runner(store, research_fn, chat_fn, configs, brief_fn=brief_fn, uploads=uploads, docs_root=docs_root,
                    fast_fn=fast_fn)
    router = APIRouter(prefix="/api/s5")

    def guard(request: Request):
        # A custom header forces a CORS preflight, on top of nginx's Origin check.
        if request.headers.get("x-star5") != "1":
            raise HTTPException(403, "header")

    async def body_of(request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "json")
        if not isinstance(body, dict):
            raise HTTPException(400, "json")
        return body

    def extras(body):
        ids = body.get("attachments") or []
        if not isinstance(ids, list) or len(ids) > MAX_ATTACHMENTS or len(set(map(str, ids))) != len(ids) \
                or not all(isinstance(i, str) and re.fullmatch(r"[0-9a-f]{16}", i) for i in ids):
            raise HTTPException(400, "attachments")
        location = clean_location(body["location"]) if body.get("location") else None
        return ids, location

    def begin(thread_id, kind, question, mode, tone, ui, lang, results, attachments=(), location=None, effort=""):
        language = detect_lang(question, ui) if lang == "auto" else lang
        try:
            turn_id = store.add_turn(thread_id, kind=kind, question=question,
                                     mode=mode if kind == "research" else "chat", lang=language, tone=tone,
                                     max_results=results, attachments=attachments, location=location,
                                     effort=effort)
        except ValueError:
            raise HTTPException(409, "attachments")
        runner.start(turn_id)
        return {"thread": thread_id, "turn": turn_id, "lang": language}

    @router.get("/config")
    async def config():
        return {"modes": list(MODES), "tones": list(TONES), "langs": list(LANGS), "ui": list(UI_LANGS),
                "efforts": list(EFFORTS), "max_results": default_results,
                "engines": list(engines.ENGINES), "engine_timeouts": engines.MODE_TIMEOUTS}

    @router.get("/threads")
    async def threads():
        return {"threads": store.threads()}

    @router.post("/threads")
    async def create(request: Request):
        guard(request)
        body = await body_of(request)
        question = clean_question(body.get("question"))
        mode, tone, ui, lang, results = options(body, default_results)
        effort = effort_option(body)
        attachments, location = extras(body)
        thread_id = store.create_thread(question)
        try:
            return begin(thread_id, "research", question, mode, tone, ui, lang, results, attachments, location, effort)
        except HTTPException:
            store.delete_thread(thread_id)
            raise

    @router.get("/threads/{thread_id}")
    async def thread(thread_id: str):
        found = store.thread(thread_id)
        if found is None:
            raise HTTPException(404, "thread")
        return found

    @router.post("/threads/{thread_id}/turns")
    async def follow_up(thread_id: str, request: Request):
        guard(request)
        body = await body_of(request)
        question = clean_question(body.get("question"))
        kind = body.get("kind", "chat")
        if kind not in ("chat", "research"):
            raise HTTPException(400, "kind")
        mode, tone, ui, lang, results = options(body, default_results)
        effort = effort_option(body)
        attachments, location = extras(body)
        if store.thread(thread_id) is None:
            raise HTTPException(404, "thread")
        return begin(thread_id, kind, question, mode, tone, ui, lang, results, attachments, location, effort)

    @router.delete("/threads/{thread_id}")
    async def delete(thread_id: str, request: Request):
        guard(request)
        found = store.thread(thread_id)
        if found is None:
            raise HTTPException(404, "thread")
        for turn in found["turns"]:
            runner.cancel(turn["id"])
        _, files, upload_ids = store.delete_thread(thread_id)
        uploads.remove(upload_ids)
        for group in files:
            for name in group.values():
                path = (Path.cwd() / unquote(str(name))).resolve()
                if path.parent == OUTPUTS and path.is_file():
                    path.unlink()
        return {"deleted": thread_id}

    @router.post("/turns/{turn_id}/cancel")
    async def cancel(turn_id: str, request: Request):
        guard(request)
        return {"cancelled": runner.cancel(turn_id)}

    @router.get("/turns/{turn_id}/events")
    async def events(turn_id: str, request: Request, after: int = 0):
        last = request.headers.get("last-event-id", "")
        if last.isdigit():
            after = int(last)

        async def stream():
            yield "retry: 3000\n\n"
            async for seq, event in runner.events(turn_id, after):
                if await request.is_disconnected():
                    return
                if event is None:
                    yield ": keepalive\n\n"
                else:
                    yield f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        if store.turn(turn_id) is None:
            raise HTTPException(404, "turn")
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={**NO_STORE, "X-Accel-Buffering": "no"})

    uploads.install(router, guard, UI_LANGS, geocode_fn or Geocoder())
    before = list(app.router.routes)
    app.include_router(router)
    own = [r for r in app.router.routes if not any(r is b for b in before)]
    app.mount("/s5", StaticFiles(directory=STATIC), name="star5")
    OUTPUTS.mkdir(exist_ok=True)
    app.mount("/outputs", StaticFiles(directory=OUTPUTS), name="star5-outputs")

    async def index():
        return FileResponse(STATIC / "index.html", headers={**NO_STORE, "Content-Security-Policy": CSP})

    app.router.routes.insert(0, APIRoute("/", index, methods=["GET"], include_in_schema=False))

    def keep(route):
        # FastAPI >= 0.16x adds one opaque _IncludedRouter per include_router(): match by identity.
        if any(route is r for r in own):
            return True
        path = getattr(route, "path", "")
        if path == "/":
            return getattr(route, "endpoint", None) is index
        if isinstance(route, Mount):
            return path in ("/s5", "/outputs")
        return False

    def prune():
        seen, kept = set(), []
        for route in app.router.routes:
            key = (type(route).__name__, getattr(route, "path", None) or id(route),
                   tuple(sorted(getattr(route, "methods", None) or ())))
            if keep(route) and key not in seen:
                seen.add(key)
                kept.append(route)
        app.router.routes[:] = kept

    # Upstream adds /site, /static and /outputs again while starting up: prune after its lifespan too.
    upstream_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with upstream_lifespan(application) as state:
            prune()
            uploads.resume()

            async def housekeeping():
                while True:
                    try:
                        removed = uploads.sweep()
                        if removed:
                            LOG.info("Removed %s stale attachments", removed)
                    except Exception:
                        LOG.exception("Attachment cleanup failed")
                    await asyncio.sleep(3600)

            sweeper = asyncio.create_task(housekeeping())
            try:
                yield state
            finally:
                sweeper.cancel()

    app.router.lifespan_context = lifespan
    prune()
    app.state.star5 = runner
    app.state.star5_uploads = uploads
    return runner
