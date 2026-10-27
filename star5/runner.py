"""Background research jobs that outlive the browser tab, with resumable progress events."""

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from .media import attachment_context, location_text
from .tokens import metered

LOG = logging.getLogger("star5")

LANGS = {"it": "italian", "fr": "french", "en": "english", "de": "german", "es": "spanish", "nl": "dutch"}
UI_LANGS = ("it", "fr", "en")
MODES = {"quick": "research_report", "detailed": "detailed_report", "deep": "deep"}
TONES = ("Objective", "Explanatory", "Analytical", "Critical", "Comparative", "Formal", "Simple", "Casual")
MAX_QUESTION = 2000
KEEP_FINISHED = 600
MAX_LOGS = 300

PHASES = {
    "plan": {"starting_research", "planning_research", "research_plan", "agent_generated", "subqueries",
             "generating_subtopics", "subtopics_generated"},
    "search": {"running_subquery_research", "researching", "fetching_query_content", "research_progress",
               "running_subquery_with_vectorstore_research"},
    "read": {"scraping_urls", "scraping_content", "scraping_complete", "added_source_url", "context_combined",
             "scraping_images"},
    "write": {"writing_report", "writing_introduction", "writing_conclusion", "generating_draft_sections",
              "draft_sections_generated", "introduction_written", "conclusion_written",
              "fetching_relevant_written_content"},
}
PHASE_ORDER = ("files", "plan", "search", "read", "write")
URL = re.compile(r"https?://[^\s<>\"')\]]+")
QUOTED = re.compile(r"'(.+)'")


def language_configs(base_path, out_dir):
    """One config file per report language: GPT Researcher reads LANGUAGE only from its config."""
    base = json.loads(Path(base_path).read_text(encoding="utf-8"))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for code, name in LANGS.items():
        path = out / f"star5-config-{code}.json"
        path.write_text(json.dumps({**base, "LANGUAGE": name}, indent=1), encoding="utf-8")
        paths[code] = str(path)
    return paths


def detect_lang(text, fallback):
    """Answer in the question's language (like Perplexity); short or unknown text keeps the UI language."""
    if len(text.strip()) < 25:
        return fallback
    try:
        from langdetect import DetectorFactory, detect
        DetectorFactory.seed = 0
        code = detect(text)
    except Exception:
        return fallback
    return code if code in LANGS else fallback


def domain(url):
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def build_sources(answer, research_sources, visited):
    """Cited sources first, in order of appearance, then the other pages that were read."""
    titles = {}
    for item in research_sources or []:
        if isinstance(item, dict) and item.get("url"):
            titles.setdefault(item["url"], (item.get("title") or "").strip()[:200])
    ordered, seen = [], set()
    for url in URL.findall(answer or ""):
        url = url.rstrip(".,;:")
        if url not in seen and urlsplit(url).hostname:
            seen.add(url)
            ordered.append({"url": url, "title": titles.get(url, ""), "domain": domain(url), "cited": True})
    for url in list(titles) + sorted(visited or []):
        if url not in seen and urlsplit(url).hostname:
            seen.add(url)
            ordered.append({"url": url, "title": titles.get(url, ""), "domain": domain(url), "cited": False})
    return ordered[:80]


def normalize(data):
    """GPT Researcher stream messages -> small UI events."""
    if not isinstance(data, dict):
        return []
    kind = data.get("type")
    if kind == "report":
        text = data.get("output")
        return [{"t": "chunk", "text": text}] if isinstance(text, str) and text else []
    if kind != "logs":
        return []
    key = data.get("content") or ""
    output = str(data.get("output") or "").strip()
    events = []
    for phase in PHASE_ORDER:
        if key in PHASES.get(phase, ()):
            events.append({"t": "phase", "phase": phase})
            break
    if key == "subqueries" and isinstance(data.get("metadata"), list):
        events.append({"t": "queries", "items": [str(q)[:300] for q in data["metadata"]][:12]})
    elif key == "running_subquery_research":
        match = QUOTED.search(output)
        if match:
            events.append({"t": "query", "q": match.group(1)[:300]})
    elif key == "added_source_url":
        match = URL.search(output)
        if match:
            url = match.group(0).rstrip(".,;:")
            events.append({"t": "source", "url": url, "domain": domain(url)})
    if output:
        events.append({"t": "log", "text": output[:400]})
    return events


class Job:
    def __init__(self, turn_id):
        self.turn_id = turn_id
        self.events = []
        self.cond = asyncio.Condition()
        self.task = None
        self.done = False
        self.finished_at = None
        self.phase = None
        self.logs = 0

    async def emit(self, event):
        if event["t"] == "phase":
            if event["phase"] == self.phase or (
                    self.phase and PHASE_ORDER.index(event["phase"]) < PHASE_ORDER.index(self.phase)):
                return
            self.phase = event["phase"]
        if event["t"] == "log":
            self.logs += 1
            if self.logs > MAX_LOGS:
                return
        async with self.cond:
            self.events.append(event)
            self.cond.notify_all()


class Sink:
    """Stands in for the websocket that GPT Researcher streams to."""

    def __init__(self, job):
        self.job = job

    async def send_json(self, data):
        for event in normalize(data):
            await self.job.emit(event)


class Runner:
    def __init__(self, store, research_fn, chat_fn, configs, clock=time.time,
                 research_slots=1, chat_slots=2, brief_fn=None, uploads=None, docs_root=None):
        self.store = store
        self.research_fn = research_fn
        self.chat_fn = chat_fn
        self.brief_fn = brief_fn
        self.uploads = uploads
        self.docs_root = Path(docs_root) if docs_root else None
        self.configs = configs
        self.clock = clock
        self.jobs = {}
        self.slots = {"research": asyncio.Semaphore(research_slots), "chat": asyncio.Semaphore(chat_slots)}
        self.tick_every = 3

    def start(self, turn_id):
        self._forget_old()
        job = Job(turn_id)
        self.jobs[turn_id] = job
        job.task = asyncio.create_task(self._run(job))
        return job

    def cancel(self, turn_id):
        job = self.jobs.get(turn_id)
        if job is None or job.done or job.task is None:
            return False
        job.task.cancel()
        return True

    def _forget_old(self):
        now = self.clock()
        for turn_id in [k for k, j in self.jobs.items() if j.done and now - j.finished_at > KEEP_FINISHED]:
            del self.jobs[turn_id]

    async def _run(self, job):
        turn = self.store.turn(job.turn_id)
        status, fields = "error", {}
        with metered() as meter:
            extra = {"tokens_in": 0, "tokens_out": 0, "llm_calls": 0}
            ticker = asyncio.create_task(self._tick(job, meter, self.tick_every, extra))
            try:
                async with self.slots[turn["kind"]]:
                    self.store.update_turn(job.turn_id, status="running", started=self.clock())
                    await job.emit({"t": "status", "status": "running"})
                    config = self.configs[turn["lang"]]
                    context = await self._context(job, turn)
                    if self.uploads is not None and turn["attachments"]:
                        extra.update(self.uploads.tokens(job.turn_id))
                    if turn["kind"] == "chat":
                        fields = await self._chat(turn, config, context)
                    else:
                        await job.emit({"t": "phase", "phase": "plan"})
                        fields = await self._research(job, turn, config, context)
                    status = "done"
            except asyncio.CancelledError:
                status, fields = "cancelled", {"error": "cancelled"}
            except Exception as error:
                LOG.exception("Star5 turn %s failed", job.turn_id)
                status, fields = "error", {"error": f"{type(error).__name__}: {error}"[:500]}
            finally:
                ticker.cancel()
                if self.docs_root is not None:
                    shutil.rmtree(self.docs_root / job.turn_id, ignore_errors=True)
                    (self.docs_root / f"{job.turn_id}.json").unlink(missing_ok=True)
                used = meter.snapshot()
                self.store.update_turn(job.turn_id, status=status, finished=self.clock(), **fields,
                                       **{key: used[key] + extra[key] for key in used})
                final = self.store.turn(job.turn_id)
                async with job.cond:
                    job.events.append({"t": "done", "turn": final})
                    job.done = True
                    job.finished_at = self.clock()
                    job.cond.notify_all()

    async def _tick(self, job, meter, every=3, extra=None):
        """Live token counter while the job runs: one event whenever the totals change."""
        last = None
        while True:
            await asyncio.sleep(every)
            current = meter.snapshot()
            if extra:
                current = {key: current[key] + extra[key] for key in current}
            if current != last and current["llm_calls"]:
                last = current
                await job.emit({"t": "tokens", **current})

    async def _context(self, job, turn):
        """Waits for the turn's attachments; returns their text, the location and a folder of documents."""
        uploads = []
        if self.uploads is not None and turn["attachments"]:
            await job.emit({"t": "phase", "phase": "files"})

            async def progress(done, total):
                await job.emit({"t": "files", "done": done, "total": total})

            uploads = await self.uploads.wait(job.turn_id, progress)
        docs = None
        ready = [u for u in uploads if u["state"] == "ready" and u["chars"]]
        if ready and self.docs_root is not None:
            docs = self.docs_root / job.turn_id
            docs.mkdir(parents=True, exist_ok=True)
            for index, upload in enumerate(ready):
                name = re.sub(r"[^\w.-]+", "_", upload["name"])[:80]
                (docs / f"{index + 1:02d}-{name}.txt").write_text(
                    f"Attached by the user: {upload['kind']} \"{upload['name']}\"\n\n" + self.uploads.text(upload["id"]),
                    encoding="utf-8")
        return {
            "attachments": attachment_context(uploads, self.uploads.text) if uploads else "",
            "location": location_text(turn["location"]),
            "docs": docs,
            "full": "\n\n".join(f"[{u['kind']} \"{u['name']}\"]\n{self.uploads.text(u['id'])}" for u in ready),
        }

    @staticmethod
    def _extra(context):
        return "\n\n".join(part for part in (context["attachments"], context["location"]) if part)

    async def _research(self, job, turn, config, context):
        previous = [t for t in self.store.history(turn["thread_id"], turn["seq"]) if t["kind"] == "research"]
        earlier = previous[-1]["question"] if previous else ""
        extra = self._extra(context)
        task = turn["question"]
        brief = ""
        if self.brief_fn is not None:
            try:
                brief = (await self.brief_fn(turn["question"], LANGS[turn["lang"]], earlier, config, extra)).strip()
            except Exception:
                LOG.exception("Research brief failed for %s; using the question as is", job.turn_id)
            if brief:
                self.store.update_turn(job.turn_id, brief=brief)
                await job.emit({"t": "brief", "text": brief})
                task = brief
        if not brief:
            if earlier:
                task = (f"{task}\n\nContext: this is a follow-up to the earlier research question \"{earlier}\".")
            if extra:
                task = f"{task}\n\n{extra[:4000]}"
        answer, research_sources, visited, files = await self.research_fn(
            task, MODES[turn["mode"]], turn["tone"], turn["max_results"], config, Sink(job), context["docs"])
        await job.emit({"t": "phase", "phase": "write"})
        return {"answer": answer, "sources": build_sources(answer, research_sources, visited), "files": files}

    async def _chat(self, turn, config, context):
        history = self.store.history(turn["thread_id"], turn["seq"])
        report = next((t["answer"] for t in reversed(history) if t["kind"] == "research" and t["answer"]), "")
        messages = []
        for item in history:
            if item["kind"] == "chat" and item["status"] == "done":
                messages += [{"role": "user", "content": item["question"]},
                             {"role": "assistant", "content": item["answer"]}]
        language = LANGS.get(turn["lang"], "italian")
        content = turn["question"]
        if context["full"]:
            content += f"\n\nFiles attached to this message (their content, as text):\n{context['full'][:40000]}"
        if context["location"]:
            content += f"\n\n{context['location']}"
        messages.append({"role": "user", "content": f"{content}\n\n(Answer in {language}.)"})
        answer, searched = await self.chat_fn(report, messages, config)
        return {"answer": answer, "sources": build_sources(answer, searched, [])}

    async def events(self, turn_id, after=0):
        """Replay then follow a job's events; a finished turn not in memory yields only its final state."""
        job = self.jobs.get(turn_id)
        if job is None:
            turn = self.store.turn(turn_id)
            if turn is not None:
                yield 1, {"t": "done", "turn": turn}
            return
        index = max(0, after)
        while True:
            async with job.cond:
                if index >= len(job.events) and not job.done:
                    try:
                        await asyncio.wait_for(job.cond.wait(), 15)
                    except asyncio.TimeoutError:
                        pass
                pending = job.events[index:]
            if not pending:
                if job.done:
                    return
                yield None, None
                continue
            for event in pending:
                index += 1
                yield index, event
                if event["t"] == "done":
                    return
