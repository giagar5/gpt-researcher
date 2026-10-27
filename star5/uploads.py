"""Chunked uploads, background processing of attachments and the location endpoint."""

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from .media import KINDS, MB, classify, clean_name
from .store import UPLOAD_PUBLIC
from .tokens import metered

LOG = logging.getLogger("star5")
CHUNK = 4 * MB                  # below nginx (20 MB) and NPM limits; Cloudflare allows 100 MB per request
MAX_PENDING = 3 * 1024 * MB     # unattached uploads waiting for a question
MIN_FREE = 2 * 1024 * MB
UNBOUND_TTL = 24 * 3600
UPLOADING_TTL = 6 * 3600
SANDBOX = {"Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff"}


def public(upload):
    return {key: upload[key] for key in UPLOAD_PUBLIC} | {"thumb": bool(upload["meta"].get("thumb"))}


class Uploads:
    def __init__(self, store, root, process_fn, config_for, clock=time.time, slots=1):
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.process_fn = process_fn
        self.config_for = config_for
        self.clock = clock
        self.slot = asyncio.Semaphore(slots)
        self.tasks = {}

    def dir(self, upload_id):
        return self.root / upload_id

    def original(self, upload):
        return self.dir(upload["id"]) / f"original.{upload['ext']}"

    def text(self, upload_id):
        path = self.dir(upload_id) / "derived.txt"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def remove(self, ids):
        for upload_id in ids:
            task = self.tasks.pop(upload_id, None)
            if task:
                task.cancel()
            shutil.rmtree(self.dir(upload_id), ignore_errors=True)

    # ---- processing ----
    def schedule(self, upload_id):
        if upload_id not in self.tasks or self.tasks[upload_id].done():
            self.tasks[upload_id] = asyncio.create_task(self._process(upload_id))

    async def _process(self, upload_id):
        async with self.slot:
            upload = self.store.upload(upload_id)
            if upload is None or upload["state"] != "processing":
                return
            work = self.dir(upload_id)
            with metered() as meter:
                try:
                    result = await self.process_fn(upload, self.original(upload), work,
                                                   self.config_for(upload["lang"]))
                    (work / "derived.txt").write_text(result["text"], encoding="utf-8")
                    meta = {**result.get("meta", {}), "thumb": bool(result.get("thumb"))}
                    fields = {"state": "ready", "summary": result.get("summary", "")[:400],
                              "chars": len(result["text"]), "meta": meta}
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    LOG.exception("Attachment %s failed", upload_id)
                    fields = {"state": "error", "error": f"{type(error).__name__}: {error}"[:300]}
                if self.store.upload(upload_id) is not None:
                    self.store.set_upload(upload_id, **fields, **meter.snapshot())

    def resume(self):
        """After a restart, finish attachments that were fully received but not processed."""
        for upload_id in self.store.processing_uploads():
            self.schedule(upload_id)

    def sweep(self):
        stale = self.store.stale_uploads(self.clock() - UNBOUND_TTL, self.clock() - UPLOADING_TTL)
        self.store.forget_uploads(stale)
        self.remove(stale)
        known = self.store.all_upload_ids()
        orphans = [p.name for p in self.root.iterdir() if p.is_dir() and p.name not in known]
        self.remove(orphans)
        return len(stale) + len(orphans)

    async def wait(self, turn_id, on_progress, timeout=3 * 3600, every=2):
        """Wait until the turn's attachments are processed; returns them."""
        deadline = self.clock() + timeout
        last = None
        while True:
            uploads = self.store.turn_uploads(turn_id)
            done = sum(u["state"] in ("ready", "error") for u in uploads)
            if done != last:
                last = done
                await on_progress(done, len(uploads))
            if done == len(uploads) or self.clock() > deadline:
                return uploads
            for upload in uploads:
                if upload["state"] == "processing":
                    self.schedule(upload["id"])
            await asyncio.sleep(every)

    def tokens(self, turn_id):
        uploads = self.store.turn_uploads(turn_id)
        return {key: sum(u[key] for u in uploads) for key in ("tokens_in", "tokens_out", "llm_calls")}

    # ---- routes ----
    def install(self, router, guard, ui_langs, geocode):
        @router.post("/uploads")
        async def create(request: Request):
            guard(request)
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(400, "json")
            name = clean_name(body.get("name") or "")
            mime = str(body.get("mime") or "")[:100]
            size = body.get("size")
            lang = body.get("ui") if body.get("ui") in ui_langs else "it"
            kind, ext = classify(name, mime)
            if kind is None:
                raise HTTPException(415, "unsupported")
            if type(size) is not int or size <= 0:
                raise HTTPException(400, "size")
            if size > KINDS[kind]["max"]:
                raise HTTPException(413, f"too_large:{KINDS[kind]['max'] // MB}")
            if self.store.pending_bytes() + size > MAX_PENDING or shutil.disk_usage(self.root).free < size + MIN_FREE:
                raise HTTPException(507, "storage")
            upload_id = self.store.create_upload(kind=kind, name=name, mime=mime, ext=ext, lang=lang, size=size)
            self.dir(upload_id).mkdir(mode=0o700)
            self.original({"id": upload_id, "ext": ext}).touch(mode=0o600)
            return {"id": upload_id, "kind": kind, "chunk": CHUNK}

        @router.put("/uploads/{upload_id}")
        async def chunk(upload_id: str, request: Request, offset: int):
            guard(request)
            upload = self.store.upload(upload_id)
            if upload is None:
                raise HTTPException(404, "upload")
            length = request.headers.get("content-length", "")
            if not length.isdigit() or not 0 < int(length) <= CHUNK:
                raise HTTPException(413, "chunk")
            data = bytearray()
            async for part in request.stream():
                data += part
                if len(data) > CHUNK:
                    raise HTTPException(413, "chunk")
            if len(data) != int(length):
                raise HTTPException(400, "length")
            if offset != upload["received"] or offset + len(data) > upload["size"]:
                raise HTTPException(409, f"offset:{upload['received']}")
            with open(self.original(upload), "r+b") as handle:
                handle.seek(offset)
                handle.write(data)
            if not self.store.add_received(upload_id, offset, len(data)):
                raise HTTPException(409, "offset")
            return {"received": offset + len(data)}

        @router.post("/uploads/{upload_id}/complete")
        async def complete(upload_id: str, request: Request):
            guard(request)
            upload = self.store.upload(upload_id)
            if upload is None:
                raise HTTPException(404, "upload")
            if upload["state"] == "uploading":
                if upload["received"] != upload["size"] or self.original(upload).stat().st_size != upload["size"]:
                    raise HTTPException(409, "incomplete")
                self.store.set_upload(upload_id, state="processing")
                self.schedule(upload_id)
            return public(self.store.upload(upload_id))

        @router.get("/uploads/{upload_id}")
        async def status(upload_id: str):
            upload = self.store.upload(upload_id)
            if upload is None:
                raise HTTPException(404, "upload")
            return public(upload)

        @router.delete("/uploads/{upload_id}")
        async def delete(upload_id: str, request: Request):
            guard(request)
            if not self.store.delete_upload(upload_id):
                raise HTTPException(409, "attached")
            self.remove([upload_id])
            return {"deleted": upload_id}

        @router.get("/uploads/{upload_id}/thumb")
        async def thumb(upload_id: str):
            path = self.dir(upload_id) / "thumb.jpg"
            if len(upload_id) != 16 or not path.is_file():
                raise HTTPException(404, "thumb")
            return FileResponse(path, media_type="image/jpeg", headers=SANDBOX)

        @router.get("/uploads/{upload_id}/file")
        async def original(upload_id: str):
            upload = self.store.upload(upload_id)
            if upload is None or upload["state"] == "uploading":
                raise HTTPException(404, "upload")
            # Always a download, never rendered on this origin (an uploaded HTML file must not run here).
            return FileResponse(self.original(upload), media_type="application/octet-stream",
                                filename=upload["name"], headers=SANDBOX)

        @router.post("/geocode")
        async def locate(request: Request):
            guard(request)
            body = await request.json()
            location = clean_location(body)
            lang = body.get("ui") if body.get("ui") in ui_langs else "it"
            try:
                location["label"] = await geocode(location["lat"], location["lon"], lang)
            except Exception as error:
                LOG.warning("Reverse geocoding failed (%s)", type(error).__name__)
                location["label"] = ""
            return location


def clean_location(body):
    if not isinstance(body, dict):
        raise HTTPException(400, "location")
    lat, lon, accuracy = body.get("lat"), body.get("lon"), body.get("accuracy", 0)
    if not all(type(v) in (int, float) for v in (lat, lon, accuracy)):
        raise HTTPException(400, "location")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 <= accuracy <= 100_000):
        raise HTTPException(400, "location")
    label = body.get("label", "")
    label = label.strip()[:200] if isinstance(label, str) else ""
    return {"lat": round(float(lat), 4), "lon": round(float(lon), 4), "accuracy": round(float(accuracy)),
            "label": label}
