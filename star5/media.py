"""Attachments (photos, videos, audio, documents) and location for Star5 Search.

Everything an attachment contributes to a research is plain text: a description of photos and
video frames written by the vision model, a Whisper transcript of audio, or the text of a document.
Media decoding runs in a separate venv as a subprocess (see media_worker.py); documents are read
with GPT Researcher's own loaders. All functions here are injectable for tests.
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

LOG = logging.getLogger("star5")
MB = 1024 * 1024
KINDS = {
    "image": {"exts": {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp", "tif", "tiff"}, "max": 40 * MB},
    "video": {"exts": {"mp4", "mov", "m4v", "webm", "mkv", "avi", "3gp"}, "max": 800 * MB},
    "audio": {"exts": {"mp3", "m4a", "aac", "wav", "ogg", "oga", "opus", "flac", "amr", "weba"}, "max": 300 * MB},
    "document": {"exts": {"pdf", "docx", "pptx", "xlsx", "csv", "txt", "md", "html", "htm", "epub"},
                 "max": 80 * MB},
}
MAX_TEXT = 400_000          # characters kept per attachment
MAX_MEDIA_SECONDS = 3600    # audio transcribed per file
WORKER_TIMEOUT = 2 * 3600
OCR_PAGES = 8
LANG_NAMES = {"it": "Italian", "fr": "French", "en": "English", "de": "German", "es": "Spanish", "nl": "Dutch"}


def classify(name, mime):
    """Kind and extension from the file name; .webm is audio when the browser says so (recordings)."""
    ext = Path(name).suffix.lower().lstrip(".")
    if ext == "webm" and (mime or "").startswith("audio/"):
        return "audio", ext
    for kind, spec in KINDS.items():
        if ext in spec["exts"]:
            return kind, ext
    return None, ext


def clean_name(name):
    name = Path(str(name)).name
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", name).strip() or "file"
    return name[:120]


# ---------------------------------------------------------------- vision and text models

def data_url(path):
    return "data:image/jpeg;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


async def ask_model(config_path, prompt, images=()):
    from gpt_researcher.config import Config
    from gpt_researcher.utils.llm import create_chat_completion

    cfg = Config(config_path)
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": data_url(path)}} for path in images]
    return await create_chat_completion(
        model=cfg.smart_llm_model, llm_provider=cfg.smart_llm_provider, llm_kwargs=cfg.llm_kwargs,
        messages=[{"role": "user", "content": content}], temperature=None, max_tokens=cfg.smart_token_limit)


IMAGE_PROMPT = """Describe this image for a web-research assistant, in {language}.
Begin with one plain sentence (no heading, no Markdown) saying what the image is.
Then cover, as useful: what it shows; all visible text, transcribed exactly; brands, models, product names
and labels; places, landmarks or signs; objects, plants, animals, food, defects or damage; the setting.
Do not identify people by face; describe them only by role or appearance. Be factual and say when
you are unsure. End with one line "Search terms:" listing precise terms to search the web for."""

VIDEO_PROMPT = """These are {count} frames, in order, from a video of {duration} seconds{audio}.
Describe the video for a web-research assistant, in {language}. Begin with one plain sentence
(no heading, no Markdown) saying what the video is. Then cover what happens, what is shown,
all visible text transcribed exactly, brands, products, places. Do not identify people by face.
Be factual and say when you are unsure. End with one line "Search terms:" listing precise terms."""

OCR_PROMPT = """These are pages {first}-{last} of a scanned document. Transcribe all their text exactly,
keeping headings, lists and tables (as Markdown). Write nothing else."""


# ---------------------------------------------------------------- worker subprocess

def worker_command():
    python = os.environ.get("STAR5_MEDIA_PYTHON", "/opt/star5-media/venv/bin/python")
    return [python, str(Path(__file__).with_name("media_worker.py"))]


async def run_worker(command, source, out_dir, timeout=WORKER_TIMEOUT):
    process = await asyncio.create_subprocess_exec(
        *worker_command(), command, str(source), str(out_dir), "--max-seconds", str(MAX_MEDIA_SECONDS),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    lines = [line for line in stdout.decode("utf-8", "replace").splitlines() if line.startswith("{")]
    result = json.loads(lines[-1]) if lines else {"error": stderr.decode("utf-8", "replace")[-300:] or "no output"}
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


# ---------------------------------------------------------------- documents

def pdf_text(path):
    import pymupdf
    with pymupdf.open(path) as document:
        pages = [page.get_text("text") for page in document]
    return pages


def pdf_render(path, out_dir, limit=OCR_PAGES):
    import pymupdf
    names = []
    with pymupdf.open(path) as document:
        for index, page in enumerate(document):
            if index >= limit:
                break
            name = out_dir / f"page{index + 1:02d}.jpg"
            page.get_pixmap(dpi=130).save(str(name), jpg_quality=80)
            names.append(name)
        total = document.page_count
    return names, total


MAX_SHEET_ROWS = 2000


def docx_text(path):
    import docx
    document = docx.Document(str(path))
    parts = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = "".join(node.text or "" for node in child.iter() if node.tag.endswith("}t")).strip()
            if text:
                parts.append(text)
        elif tag == "tbl":
            for row in child.iter():
                if row.tag.endswith("}tr"):
                    cells = ["".join(t.text or "" for t in cell.iter() if t.tag.endswith("}t")).strip()
                             for cell in row if cell.tag.endswith("}tc")]
                    parts.append(" | ".join(cells))
    return "\n".join(parts)


def pptx_text(path):
    from pptx import Presentation
    slides = []
    for number, slide in enumerate(Presentation(str(path)).slides, 1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines += [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
            elif getattr(shape, "has_table", False) and shape.has_table:
                lines += [" | ".join(cell.text for cell in row.cells) for row in shape.table.rows]
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            lines.append("Notes: " + slide.notes_slide.notes_text_frame.text.strip())
        slides.append(f"[slide {number}]\n" + "\n".join(lines))
    return "\n\n".join(slides)


def xlsx_text(path):
    """Cell values of every sheet (stdlib only: openpyxl is not installed)."""
    import zipfile
    from xml.etree import ElementTree
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in item.iter(f"{{{ns['m']}}}t")) for item in root.findall("m:si", ns)]
        rels = {r.get("Id"): r.get("Target") for r in
                ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels")).iter(f"{rel_ns}Relationship")}
        sheets = []
        for sheet in ElementTree.fromstring(archive.read("xl/workbook.xml")).iter(f"{{{ns['m']}}}sheet"):
            target = rels.get(sheet.get(f"{{{ns['r']}}}id"), "").lstrip("/")
            target = target if target.startswith("xl/") else f"xl/{target}"
            if target not in names:
                continue
            rows = []
            for row in ElementTree.fromstring(archive.read(target)).iter(f"{{{ns['m']}}}row"):
                values = []
                for cell in row.findall("m:c", ns):
                    kind, value = cell.get("t"), cell.find("m:v", ns)
                    if kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter(f"{{{ns['m']}}}t"))
                    elif value is None:
                        text = ""
                    elif kind == "s":
                        index = int(value.text)
                        text = shared[index] if index < len(shared) else ""
                    else:
                        text = value.text or ""
                    values.append(text.strip())
                if any(values):
                    rows.append(" | ".join(values))
                if len(rows) >= MAX_SHEET_ROWS:
                    rows.append("…")
                    break
            sheets.append(f"[sheet {sheet.get('name')}]\n" + "\n".join(rows))
    return "\n\n".join(sheets)


def html_text(markup):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(markup, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n")).strip()


def epub_text(path):
    import posixpath
    import zipfile
    from xml.etree import ElementTree
    with zipfile.ZipFile(path) as archive:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
        opf = ElementTree.fromstring(archive.read(opf_path))
        items = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("}item")}
        order = [items.get(e.get("idref")) for e in opf.iter() if e.tag.endswith("}itemref")]
        folder = posixpath.dirname(opf_path)
        parts = []
        for href in filter(None, order):
            name = posixpath.normpath(posixpath.join(folder, href.split("#")[0]))
            if name in archive.namelist():
                parts.append(html_text(archive.read(name)))
    return "\n\n".join(p for p in parts if p)


def read_plain(path):
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


DOCUMENT_READERS = {
    "docx": docx_text, "pptx": pptx_text, "xlsx": xlsx_text, "epub": epub_text,
    "html": lambda p: html_text(Path(p).read_bytes()), "htm": lambda p: html_text(Path(p).read_bytes()),
    "txt": read_plain, "md": read_plain, "csv": read_plain,
}


async def document_text(path, ext):
    reader = DOCUMENT_READERS.get(ext)
    if reader is None:
        raise ValueError(f"unsupported document type: {ext}")
    return (await asyncio.to_thread(reader, path)).strip()


# ---------------------------------------------------------------- one attachment

def first_line(text, limit=300):
    """A short plain-text summary: the first real paragraph, without Markdown headings or emphasis."""
    paragraphs = [[line.strip() for line in block.splitlines() if line.strip()]
                  for block in re.split(r"\n\s*\n", text or "")]
    paragraphs = [p for p in paragraphs if p]
    heading = re.compile(r"#.*|[*_]{2}.+[*_]{2}:?")
    while len(paragraphs) > 1 and len(paragraphs[0]) == 1 and heading.fullmatch(paragraphs[0][0]):
        paragraphs.pop(0)
    while paragraphs and len(paragraphs[0]) > 1 and heading.fullmatch(paragraphs[0][0]):
        paragraphs[0].pop(0)
    lines = []
    for paragraph in paragraphs:
        lines += paragraph
        if len(" ".join(lines)) >= 60:
            break
    lines = [re.sub(r"^(#+|>|[-*] )\s*", "", line) for line in lines]
    text = re.sub(r"\*\*|__|`", "", " ".join(lines))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


async def process(upload, source, out_dir, config_path, worker=run_worker, model=ask_model):
    """Returns {"text", "summary", "meta", "thumb"} for one finished upload."""
    language = LANG_NAMES.get(upload["lang"], "Italian")
    kind = upload["kind"]
    meta, thumb = {}, False
    if kind == "image":
        result = await worker("image", source, out_dir)
        thumb = bool(result.get("thumb"))
        meta = {k: result[k] for k in ("width", "height", "taken") if result.get(k)}
        text = await model(config_path, IMAGE_PROMPT.format(language=language), [out_dir / result["images"][0]])
        summary = first_line(text)
    elif kind == "video":
        result = await worker("video", source, out_dir)
        thumb = bool(result.get("thumb"))
        transcript = result.get("transcript") or ""
        meta = {k: result[k] for k in ("duration", "language", "truncated") if result.get(k) is not None}
        description = ""
        if result.get("images"):
            audio = f" whose audio says:\n{transcript[:6000]}\n" if transcript else " (no speech found)"
            description = await model(config_path, VIDEO_PROMPT.format(
                count=len(result["images"]), duration=result.get("duration", "?"), audio=audio, language=language),
                [out_dir / name for name in result["images"]])
        text = description + (f"\n\nTranscript of the audio:\n{transcript}" if transcript else "")
        summary = first_line(description or transcript)
    elif kind == "audio":
        result = await worker("audio", source, out_dir)
        text = result.get("transcript") or ""
        meta = {k: result[k] for k in ("duration", "language", "truncated") if result.get(k) is not None}
        summary = first_line(text) or "(no speech found)"
    else:
        if upload["ext"] == "pdf":
            pages = await asyncio.to_thread(pdf_text, source)
            text = "\n\n".join(f"[page {i + 1}]\n{page.strip()}" for i, page in enumerate(pages) if page.strip())
            meta = {"pages": len(pages)}
            if len(re.sub(r"\s", "", text)) < 100 * max(1, min(len(pages), 3)):
                images, total = await asyncio.to_thread(pdf_render, source, out_dir)
                if images:
                    text = await model(config_path, OCR_PROMPT.format(first=1, last=len(images)), images)
                    meta.update({"ocr": True, "ocr_pages": len(images), "pages": total})
        else:
            text = await document_text(source, upload["ext"])
        summary = first_line(text)
    text = (text or "").strip()
    if len(text) > MAX_TEXT:
        text, meta["cut"] = text[:MAX_TEXT], True
    return {"text": text, "summary": summary, "meta": meta, "thumb": thumb}


# ---------------------------------------------------------------- location

class Geocoder:
    """Reverse geocoding with OpenStreetMap Nominatim (max 1 request/s, honest User-Agent).

    Coordinates are rounded to 3 decimals (about 100 m) before leaving the server and the street is
    never used: the research needs the area, not the address."""

    URL = "https://nominatim.openstreetmap.org/reverse"
    AGENT = "Star5-Search/1.0 (+https://search.star5.org)"

    def __init__(self, fetch=None, clock=time.monotonic):
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.cache = {}

    @staticmethod
    async def _fetch(lat, lon, lang):
        import httpx
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": Geocoder.AGENT}) as client:
            response = await client.get(Geocoder.URL, params={
                "format": "jsonv2", "lat": lat, "lon": lon, "zoom": 16, "addressdetails": 1, "accept-language": lang})
            response.raise_for_status()
            return response.json()

    @staticmethod
    def label(data):
        address = data.get("address") or {}
        parts = []
        for keys in (("neighbourhood", "suburb", "quarter", "hamlet"),
                     ("village", "town", "city", "municipality"), ("state", "region", "county"), ("country",)):
            value = next((address[k] for k in keys if address.get(k)), None)
            if value and value not in parts:
                parts.append(value)
        return ", ".join(parts)[:200]

    async def __call__(self, lat, lon, lang):
        key = (round(lat, 3), round(lon, 3), lang)
        if key in self.cache:
            return self.cache[key]
        async with self.lock:
            wait = 1.1 - (self.clock() - self.last)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last = self.clock()
            data = await self.fetch(key[0], key[1], lang)
        label = self.label(data)
        if len(self.cache) > 500:
            self.cache.clear()
        self.cache[key] = label
        return label


def location_text(location):
    if not location:
        return ""
    where = location.get("label") or "unknown area"
    return (f"The user's current location: {where} (latitude {location['lat']:.3f}, longitude {location['lon']:.3f}, "
            f"accuracy about {int(location.get('accuracy') or 0)} m). Use it for anything local, "
            f"\"near me\" or \"here\".")


def attachment_context(uploads, read_text, per_item=2500, total=12000):
    """Short excerpt of every attachment, for the research brief and the planner."""
    parts, used = [], 0
    for upload in uploads:
        if upload["state"] != "ready":
            parts.append(f"- {upload['kind']} \"{upload['name']}\": could not be read ({upload['error'] or 'error'}).")
            continue
        text = read_text(upload["id"])
        room = max(0, min(per_item, total - used))
        excerpt = text[:room] + ("…" if len(text) > room else "")
        used += len(excerpt)
        parts.append(f"- {upload['kind']} \"{upload['name']}\":\n{excerpt}")
    return "Files attached by the user (their content, as text):\n" + "\n".join(parts) if parts else ""
