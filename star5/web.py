"""Star5 routes: the new UI on "/" and a small thread API under /api/s5.

Authentication stays in front of the app (nginx auth_request with the owners' Google login).
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from .runner import LANGS, MODES, MAX_QUESTION, TONES, UI_LANGS, Runner, detect_lang, language_configs
from .store import Store

LOG = logging.getLogger("star5")
STATIC = Path(__file__).resolve().parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")
NO_STORE = {"Cache-Control": "no-store"}
OUTPUTS = Path("outputs").resolve()


async def research(task, report_type, tone, max_results, config_path, sink):
    from gpt_researcher.utils.enum import Tone
    from server.server_utils import generate_report_files, sanitize_filename
    from server.websocket_manager import run_agent

    report, researcher = await run_agent(
        task, report_type, "web", [], [], Tone[tone], sink, headers={}, query_domains=[],
        config_path=config_path, return_researcher=True, max_search_results=max_results)
    report = str(report)
    files = await generate_report_files(report, sanitize_filename(f"task_{int(time.time())}_{task[:80]}"))
    sources = [{"url": s.get("url"), "title": s.get("title")} for s in researcher.get_research_sources()
               if isinstance(s, dict)]
    return report, sources, list(researcher.visited_urls), {k: v for k, v in files.items() if v}


def brave_results(query):
    """Web search for the report chat: Brave instead of the upstream Tavily-only tool."""
    if not os.environ.get("BRAVE_API_KEY"):
        return {"error": "Web search is not configured", "results": []}
    from gpt_researcher.retrievers import BraveSearch
    hits = BraveSearch(query).search(max_results=5)
    return {"results": [{"title": h.get("title", ""), "url": h.get("href", ""), "content": h.get("body", "")}
                        for h in hits]}


async def chat(report, messages, config_path):
    from chat.chat import ChatAgentWithMemory

    class Star5Chat(ChatAgentWithMemory):
        def quick_search(self, query):
            try:
                results = brave_results(query)
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


def install(app: FastAPI, *, data_dir=None, base_config=None, research_fn=research, chat_fn=chat):
    data_dir = Path(data_dir or os.environ.get("STAR5_DATA", "data"))
    base_config = base_config or os.environ.get("CONFIG_PATH")
    configs = language_configs(base_config, data_dir / "configs")
    default_results = json.loads(Path(base_config).read_text(encoding="utf-8")).get("MAX_SEARCH_RESULTS_PER_QUERY", 8)
    store = Store(data_dir / "star5.db")
    interrupted = store.fail_interrupted()
    if interrupted:
        LOG.warning("Marked %s interrupted Star5 turns as failed", interrupted)
    runner = Runner(store, research_fn, chat_fn, configs)
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

    def begin(thread_id, kind, question, mode, tone, ui, lang, results):
        language = detect_lang(question, ui) if lang == "auto" else lang
        turn_id = store.add_turn(thread_id, kind=kind, question=question, mode=mode if kind == "research" else "chat",
                                 lang=language, tone=tone, max_results=results)
        runner.start(turn_id)
        return {"thread": thread_id, "turn": turn_id, "lang": language}

    @router.get("/config")
    async def config():
        return {"modes": list(MODES), "tones": list(TONES), "langs": list(LANGS), "ui": list(UI_LANGS),
                "max_results": default_results}

    @router.get("/threads")
    async def threads():
        return {"threads": store.threads()}

    @router.post("/threads")
    async def create(request: Request):
        guard(request)
        body = await body_of(request)
        question = clean_question(body.get("question"))
        mode, tone, ui, lang, results = options(body, default_results)
        thread_id = store.create_thread(question)
        return begin(thread_id, "research", question, mode, tone, ui, lang, results)

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
        if store.thread(thread_id) is None:
            raise HTTPException(404, "thread")
        return begin(thread_id, kind, question, mode, tone, ui, lang, results)

    @router.delete("/threads/{thread_id}")
    async def delete(thread_id: str, request: Request):
        guard(request)
        found = store.thread(thread_id)
        if found is None:
            raise HTTPException(404, "thread")
        for turn in found["turns"]:
            runner.cancel(turn["id"])
        _, files = store.delete_thread(thread_id)
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

    app.include_router(router)
    app.mount("/s5", StaticFiles(directory=STATIC), name="star5")

    async def index():
        return FileResponse(STATIC / "index.html", headers={**NO_STORE, "Content-Security-Policy": CSP})

    # Take "/" ahead of the upstream page, which stays reachable at /site/index.html.
    app.router.routes.insert(0, APIRoute("/", index, methods=["GET"], include_in_schema=False))
    app.state.star5 = runner
    return runner
