import asyncio
import json
import os
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import star5.web as web
from star5.runner import build_sources, detect_lang, normalize
from star5.store import Store
from star5.web import drop_event_logs, install

H = {"X-Star5": "1"}


class Fakes:
    def __init__(self):
        self.calls = []
        self.chats = []
        self.gate = None

    async def research(self, task, report_type, tone, max_results, config_path, sink):
        self.calls.append({"task": task, "type": report_type, "tone": tone, "max": max_results,
                           "lang": json.loads(Path(config_path).read_text())["LANGUAGE"]})
        await sink.send_json({"type": "logs", "content": "subqueries", "output": "plan",
                              "metadata": ["query uno", "query due"]})
        await sink.send_json({"type": "logs", "content": "running_subquery_research",
                              "output": "\n🔍 Running research for 'query uno'..."})
        await sink.send_json({"type": "logs", "content": "added_source_url",
                              "output": "✅ Added source url to research: https://www.sqlite.org/wal.html\n"})
        if self.gate is not None:
            await self.gate.wait()
        await sink.send_json({"type": "report", "output": "# Titolo\n"})
        answer = "# Titolo\nVedi [WAL](https://www.sqlite.org/wal.html) e [blog](https://example.com/x)."
        Path("outputs").mkdir(exist_ok=True)
        Path("outputs/t1.md").write_text(answer)
        return (answer, [{"url": "https://www.sqlite.org/wal.html", "title": "Write-Ahead Logging"}],
                ["https://other.org/read"], {"md": "outputs/t1.md"})

    async def chat(self, report, messages, config_path):
        self.chats.append({"report": report, "messages": messages})
        return "Risposta dal rapporto.", [{"url": "https://brave.example/r", "title": "R"}]


class Star5Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)
        base = Path(self.tmp.name) / "config.json"
        base.write_text(json.dumps({"LANGUAGE": "italian", "MAX_SEARCH_RESULTS_PER_QUERY": 8}))
        self.fakes = Fakes()
        site = Path(self.tmp.name) / "site"
        site.mkdir()
        (site / "index.html").write_text("OLD UI")

        @asynccontextmanager
        async def upstream_lifespan(app):
            app.mount("/site", StaticFiles(directory=site), name="frontend")
            app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")
            yield

        Path("outputs").mkdir()
        self.app = FastAPI(lifespan=upstream_lifespan)
        self.app.mount("/static", StaticFiles(directory=site), name="static")

        @self.app.get("/", response_class=HTMLResponse)
        async def upstream():
            return "UPSTREAM"

        @self.app.get("/api/reports")
        async def reports():
            return {"reports": []}

        @self.app.websocket("/ws")
        async def ws(socket: WebSocket):
            await socket.accept()

        import star5.web as web
        self.addCleanup(setattr, web, "OUTPUTS", web.OUTPUTS)
        web.OUTPUTS = (Path(self.tmp.name) / "outputs").resolve()
        self.runner = install(self.app, data_dir=Path(self.tmp.name) / "data", base_config=str(base),
                              research_fn=self.fakes.research, chat_fn=self.fakes.chat)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def events(self, turn_id, after=0):
        out = []
        with self.client.stream("GET", f"/api/s5/turns/{turn_id}/events?after={after}") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            for line in response.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    out.append(event)
                    if event["t"] == "done":
                        break
        return out

    def start(self, question="Spiega in italiano quando usare la modalità WAL di SQLite", **extra):
        response = self.client.post("/api/s5/threads", headers=H, json={"question": question, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class ApiTests(Star5Case):
    def test_index_replaces_upstream_page_with_strict_csp(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("UPSTREAM", response.text)
        self.assertNotIn("/site/", response.text)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        for asset in ("/s5/app.js", "/s5/vendor/showdown.min.js", "/s5/vendor/purify.min.js"):
            self.assertEqual(self.client.get(asset).status_code, 200, asset)

    def test_old_ui_and_its_api_are_gone_after_startup(self):
        for path in ("/site/index.html", "/static/index.html", "/api/reports", "/ws", "/docs", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        Path("outputs/report.md").write_text("ok")
        self.assertEqual(self.client.get("/outputs/report.md").text, "ok")
        paths = [getattr(r, "path", "") for r in self.app.router.routes]
        self.assertEqual(paths.count("/outputs"), 1)
        self.assertEqual(paths.count("/"), 1)
        self.assertEqual(self.client.get("/api/s5/config").status_code, 200)

    def test_research_runs_in_background_and_streams_progress(self):
        started = self.start(mode="detailed", tone="Critical", max_results=10)
        self.assertEqual(started["lang"], "it")
        events = self.events(started["turn"])
        kinds = [e["t"] for e in events]
        self.assertIn("queries", kinds)
        self.assertIn({"t": "query", "q": "query uno"}, events)
        self.assertIn({"t": "source", "url": "https://www.sqlite.org/wal.html", "domain": "sqlite.org"}, events)
        self.assertIn({"t": "chunk", "text": "# Titolo\n"}, events)
        phases = [e["phase"] for e in events if e["t"] == "phase"]
        self.assertEqual(phases, ["plan", "search", "read", "write"])
        done = events[-1]["turn"]
        self.assertEqual(done["status"], "done")
        self.assertEqual([s["url"] for s in done["sources"]],
                         ["https://www.sqlite.org/wal.html", "https://example.com/x", "https://other.org/read"])
        self.assertEqual(done["sources"][0]["title"], "Write-Ahead Logging")
        self.assertEqual([s["cited"] for s in done["sources"]], [True, True, False])
        self.assertEqual(self.fakes.calls[0], {"task": "Spiega in italiano quando usare la modalità WAL di SQLite",
                                               "type": "detailed_report", "tone": "Critical", "max": 10,
                                               "lang": "italian"})
        thread = self.client.get(f"/api/s5/threads/{started['thread']}").json()
        self.assertEqual(thread["turns"][0]["files"], {"md": "outputs/t1.md"})
        listing = self.client.get("/api/s5/threads").json()["threads"]
        self.assertEqual(listing[0]["status"], "done")

    def test_resume_after_reconnect_and_finished_turn_replays_final_state(self):
        started = self.start()
        events = self.events(started["turn"])
        tail = self.events(started["turn"], after=len(events) - 1)
        self.assertEqual([e["t"] for e in tail], ["done"])
        self.runner.jobs.clear()
        again = self.events(started["turn"])
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["turn"]["status"], "done")

    def test_report_language_follows_the_question_or_explicit_choice(self):
        french = self.start("Explique quand utiliser le mode WAL de SQLite et ses limites", ui="it")
        self.assertEqual(french["lang"], "fr")
        self.events(french["turn"])
        self.assertEqual(self.fakes.calls[-1]["lang"], "french")
        forced = self.start("Explique quand utiliser le mode WAL de SQLite et ses limites", lang="en")
        self.assertEqual(forced["lang"], "en")
        short = self.start("WAL?", ui="fr")
        self.assertEqual(short["lang"], "fr")

    def test_follow_up_chat_uses_report_and_previous_chat(self):
        started = self.start()
        self.events(started["turn"])
        first = self.client.post(f"/api/s5/threads/{started['thread']}/turns", headers=H,
                                 json={"question": "E con NFS?", "kind": "chat"}).json()
        self.assertEqual(self.events(first["turn"])[-1]["turn"]["answer"], "Risposta dal rapporto.")
        second = self.client.post(f"/api/s5/threads/{started['thread']}/turns", headers=H,
                                  json={"question": "Riassumi", "kind": "chat"}).json()
        done = self.events(second["turn"])[-1]["turn"]
        self.assertEqual(done["mode"], "chat")
        self.assertEqual(done["sources"][0]["url"], "https://brave.example/r")
        last = self.fakes.chats[-1]
        self.assertIn("sqlite.org/wal.html", last["report"])
        self.assertEqual([m["role"] for m in last["messages"]], ["user", "assistant", "user"])
        self.assertIn("Riassumi", last["messages"][-1]["content"])

    def test_follow_up_research_carries_previous_question(self):
        started = self.start()
        self.events(started["turn"])
        follow = self.client.post(f"/api/s5/threads/{started['thread']}/turns", headers=H,
                                  json={"question": "E rispetto a PostgreSQL?", "kind": "research"}).json()
        self.events(follow["turn"])
        self.assertIn("follow-up", self.fakes.calls[-1]["task"])
        self.assertIn("SQLite", self.fakes.calls[-1]["task"])

    def test_cancel_marks_turn_cancelled(self):
        self.fakes.gate = asyncio.Event()
        started = self.start()
        response = self.client.post(f"/api/s5/turns/{started['turn']}/cancel", headers=H)
        self.assertTrue(response.json()["cancelled"])
        done = self.events(started["turn"])[-1]["turn"]
        self.assertEqual(done["status"], "cancelled")

    def test_delete_removes_thread_and_only_its_output_files(self):
        started = self.start()
        self.events(started["turn"])
        keep = Path("outputs/other.md")
        keep.write_text("x")
        response = self.client.delete(f"/api/s5/threads/{started['thread']}", headers=H)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Path("outputs/t1.md").exists())
        self.assertTrue(keep.exists())
        self.assertEqual(self.client.get(f"/api/s5/threads/{started['thread']}").status_code, 404)

    def test_validation_and_csrf_header(self):
        bad = [
            ({"question": ""}, H), ({"question": "x" * 2001}, H), ({"question": "ok", "mode": "turbo"}, H),
            ({"question": "ok", "max_results": 99}, H), ({"question": "ok", "max_results": "8"}, H),
            ({"question": "ok", "tone": "Evil"}, H), ({"question": "ok", "lang": "xx"}, H),
            ({"question": "ok", "ui": "de"}, H),
        ]
        for body, headers in bad:
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/s5/threads", headers=headers, json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/s5/threads", json={"question": "ok"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/s5/threads/x").status_code, 403)
        self.assertEqual(self.client.post("/api/s5/threads/missing/turns", headers=H,
                                          json={"question": "ok"}).status_code, 404)
        self.assertEqual(self.client.get("/api/s5/turns/missing/events").status_code, 404)


class UnitTests(unittest.TestCase):
    def test_restart_fails_interrupted_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "s.db")
            thread = store.create_thread("q")
            turn = store.add_turn(thread, kind="research", question="q", mode="quick", lang="it",
                                  tone="Objective", max_results=8)
            self.assertEqual(Store(Path(tmp) / "s.db").fail_interrupted(), 1)
            self.assertEqual(store.turn(turn)["status"], "error")
            self.assertEqual(store.turn(turn)["error"], "interrupted")

    def test_normalize_ignores_images_and_unknown(self):
        self.assertEqual(normalize({"type": "images", "output": "[]"}), [])
        self.assertEqual(normalize("x"), [])
        self.assertEqual(normalize({"type": "logs", "content": "writing_report", "output": "w"}),
                         [{"t": "phase", "phase": "write"}, {"t": "log", "text": "w"}])

    def test_build_sources_dedupes_and_strips_punctuation(self):
        sources = build_sources("a https://a.org/x. b https://a.org/x", [], ["https://b.org"])
        self.assertEqual([s["url"] for s in sources], ["https://a.org/x", "https://b.org"])

    def test_detect_lang_fallback(self):
        self.assertEqual(detect_lang("ok", "fr"), "fr")
        self.assertEqual(detect_lang("Quali sono i vantaggi della modalità WAL in SQLite?", "en"), "it")

    def test_drop_event_logs_only_removes_this_runs_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = web.OUTPUTS
            web.OUTPUTS = Path(tmp)
            try:
                since = time.time()
                mine, other, report = (Path(tmp) / "task_1_abc.json", Path(tmp) / "task_1_zzz.json",
                                       Path(tmp) / "task_1_abc.md")
                for path in (mine, other, report):
                    path.write_text("x")
                earlier = Path(tmp) / "task_0_abc.json"
                earlier.write_text("x")
                os.utime(earlier, (since - 3600, since - 3600))
                drop_event_logs("abc", since)
                self.assertFalse(mine.exists())
                self.assertTrue(other.exists() and report.exists() and earlier.exists())
            finally:
                web.OUTPUTS = old


if __name__ == "__main__":
    unittest.main()
