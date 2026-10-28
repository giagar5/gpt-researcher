<p align="center">
  <img src="docs/star5/star5-search-ocean.jpg" alt="Star5 Search: research, answered" width="100%">
</p>

<h1 align="center"><img src="docs/star5/icon.svg" alt="" width="44" align="center">&nbsp;Star5 Search</h1>

<p align="center">
  <b>A private, self-hosted research assistant for the web.</b><br>
  Ask a question and get a cited answer, from a 30-second reply to a multi-level investigation.
</p>

---

Star5 Search reads the web for you. It plans its searches, reads the pages, and writes an
answer with numbered citations. Every claim can be checked against its source. It runs on
your own server behind your own login. Your questions, history and files stay with you.

## Three levels of depth

| Mode | Typical time | Use it for |
|---|---|---|
| ⚡ **Veloce** (Fast) | **~30 s** | Direct answers: facts, prices, opening hours, "find me a good … near me" |
| 📚 **Standard** | **2-3 min** | A complete, sourced report across many sources |
| 🔭 **Approfondita** (Deep) | **5-8 min** | Complex questions explored over several levels of sub-questions |

- **Veloce** is a purpose-built pipeline with no agent loop:
  1. A small model writes 2-3 sharp queries.
  2. The searches run in parallel.
  3. The best pages are read within 8 seconds.
  4. One strong model writes a concise answer with citations.

  Measured: 21-29 s, with 6-10 cited sources.
- **Standard** and **Approfondita** use the [GPT Researcher](https://github.com/assafelovic/gpt-researcher)
  agent. Before the search starts, a research brief turns the question into a goal, a
  deliverable and search hints. "Find me a good psychologist" therefore yields a shortlist of
  named people, not an essay on prices.

## What you get

- **Answers with sources.** Sources appear as cards, cited sources first, and likely
  official sites are flagged. The answer has numbered citations and a table of contents. You
  can download it as PDF, Word or Markdown.
- **Conversations.** Ask follow-ups on the report ("from the report") or start a new web
  search with the previous context. The history is always visible: the latest searches are
  on the home page and the full history is in the sidebar, the same on every device.
- **Attachments.**
  - Photos, including HEIC: the vision model reads visible text, brands and places.
  - Audio and voice recordings: transcribed locally with Whisper.
  - Video: frames plus the transcript of the soundtrack.
  - Documents: PDF (OCR when scanned), Word, PowerPoint, Excel, EPUB, HTML and text.

  You can ask "where can I buy this?" with a photo, or "is this quote fair?" with a PDF.
- **Your location, on request.** GPS is used only for the question you attach it to. It is
  rounded to about 100 m before reverse geocoding, so "near me" means near you.
- **Your choice of reasoning.** Pick automatic, low, medium, high, very high or maximum.
  The choice is remembered for the next searches.
- **Live progress.** You see the plan, the queries, the pages read and the writing step.
  Closing the tab does not stop a search, and you can come back to it later.
- **Token counter.** Each answer shows the model tokens it used, and each conversation
  shows its total.
- **Multilingual.** The interface is in Italian, French and English. The answer follows the
  language of the question, or the one you choose.

## Privacy and security

- **Self-hosted.** It runs in your own container behind your own sign-in (nginx
  `auth_request`, Google login restricted to the owners).
- **No shared caching.** Every response is `Cache-Control: private, no-store`.
- **Locked-down pages.** The CSP is strict, with no inline code. Page text is inserted as
  text and Markdown is sanitised.
- **Safe uploads.** Originals are served only as sandboxed downloads. Unused uploads are
  deleted automatically.
- **Public sites only.** The fast reader fetches only public internet hosts, never LAN or
  loopback addresses.

## How it is built

```
browser ──▶ nginx (login, no-store) ──▶ FastAPI app (star5/)
                                          ├─ Veloce pipeline (star5/fast.py)
                                          ├─ GPT Researcher agent (Standard, Approfondita)
                                          ├─ attachments: media worker + Whisper (separate venv)
                                          └─ SQLite: threads, turns, uploads
models:  any OpenAI-compatible endpoint (here an Azure proxy with "fast" and "frontier" aliases)
search:  Brave Search API, with a DuckDuckGo fallback
```

All Star5 code lives in [`star5/`](star5) and [`star5_main.py`](star5_main.py), on top of an
unmodified GPT Researcher, so upstream improvements can still be merged. The upstream web
UI and API are not served.

## Running it

Requirements: Python 3.12, an OpenAI-compatible model endpoint and a
[Brave Search API](https://brave.com/search/api/) key.

```bash
pip install -r requirements.txt
export CONFIG_PATH=/path/to/config.json   # models, retriever = brave, token limits
export BRAVE_API_KEY=...                  # and the model endpoint variables
uvicorn star5_main:app --host 127.0.0.1 --port 8000
```

Put it behind a reverse proxy that authenticates users.

Attachments need a second environment (`av`, `faster-whisper`, `pillow-heif`) pointed to by
`STAR5_MEDIA_PYTHON`.

Tests: `python -m unittest star5.tests.test_star5`.

## Credits

Star5 Search is built on [GPT Researcher](https://github.com/assafelovic/gpt-researcher) by
Assaf Elovic and contributors, released under the Apache 2.0 license (see [LICENSE](LICENSE)).
The original project README is kept in [UPSTREAM-README.md](UPSTREAM-README.md).

<p align="center">
  <img src="docs/star5/star5-search-emerald.jpg" alt="Star5 Search, emerald edition" width="70%">
</p>
