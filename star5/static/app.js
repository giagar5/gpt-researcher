/* Star5 Search front end: vanilla JS, no inline code (strict CSP), all text inserted via textContent. */
"use strict";
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const store = {get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } }};
  const ICON = {
    copy: '<path d="M9 9h10v10H9z"/><path d="M5 15V5h10"/>',
    pdf: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 13h6M9 17h4"/>',
    retry: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/>',
    trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    book: '<path d="M4 5h7a3 3 0 0 1 3 3v12a2 2 0 0 0-2-2H4z"/><path d="M20 5h-4a2 2 0 0 0-2 2"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    bolt: '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>',
    layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
    dive: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4M11 8v6M8 11h6"/>',
  };
  const icon = name => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.innerHTML = ICON[name]; return s; };

  const MODES = ["quick", "detailed", "deep"];
  const MODE_ICON = {quick: "bolt", detailed: "layers", deep: "dive"};
  const TONES = ["Objective", "Explanatory", "Analytical", "Critical", "Comparative", "Formal", "Simple", "Casual"];
  const LANGS = ["it", "fr", "en", "de", "es", "nl"];
  const browser = (navigator.language || "it").slice(0, 2);
  const state = {
    ui: store.get("s5.ui", ["it", "fr", "en"].includes(browser) ? browser : "it"),
    mode: store.get("s5.mode", "quick"), kind: store.get("s5.kind", "chat"),
    lang: store.get("s5.lang", "auto"), tone: store.get("s5.tone", "Objective"),
    sites: Number(store.get("s5.sites", "0")) || 8,
    threads: [], thread: null, streams: new Map(), live: new Map(), timers: new Set(), expired: false, libLimit: 8,
  };
  if (!MODES.includes(state.mode)) state.mode = "quick";
  let T = I18N[state.ui] || I18N.it;
  const md = new showdown.Converter({tables: true, strikethrough: true, simplifiedAutoLink: true,
    excludeTrailingPunctuationFromURLs: true, ghCodeBlocks: true, tasklists: true, noHeaderId: true});

  /* ---------- API ---------- */
  async function api(path, {method = "GET", body} = {}) {
    const headers = {"Accept": "application/json"};
    if (method !== "GET") headers["X-Star5"] = "1";
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let res;
    try {
      res = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin", redirect: "manual", cache: "no-store"});
    } catch (error) { banner(T.offline); throw error; }
    if (res.type === "opaqueredirect" || res.status === 401 || res.status === 302) { expired(); throw new Error("expired"); }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }
  function banner(text, link) {
    const b = $("#banner"); b.replaceChildren(text);
    if (link) { b.append(" "); const a = el("a", "", link.text); a.href = link.href; b.append(a); }
    b.hidden = false;
  }
  function expired() {
    if (state.expired) return;
    state.expired = true;
    banner(T.sessionExpired, {text: T.login, href: "/auth/login"});
  }

  /* ---------- i18n ---------- */
  function applyI18n() {
    T = I18N[state.ui] || I18N.it;
    document.documentElement.lang = state.ui;
    document.querySelectorAll("[data-i18n]").forEach(e => { e.textContent = T[e.dataset.i18n] || ""; });
    document.querySelectorAll("[data-i18n-ph]").forEach(e => { e.placeholder = T[e.dataset.i18nPh] || ""; });
    document.querySelectorAll("[data-i18n-title]").forEach(e => { e.title = T[e.dataset.i18nTitle] || ""; e.setAttribute("aria-label", e.title); });
    document.querySelectorAll("[data-i18n-aria]").forEach(e => e.setAttribute("aria-label", T[e.dataset.i18nAria] || ""));
    $("#uiLang").value = state.ui;
    buildSeg($("#modes"), MODES, state.mode, m => T.modes[m], m => { state.mode = m; store.set("s5.mode", m); hint(); }, MODE_ICON);
    buildSeg($("#kinds"), ["chat", "research"], state.kind, k => T.kinds[k], k => { state.kind = k; store.set("s5.kind", k); }, {chat: "book", research: "globe"});
    fillSelect($("#lang"), ["auto", ...LANGS], state.lang, v => v === "auto" ? T.langAuto : T.langNames[v]);
    fillSelect($("#tone"), TONES, state.tone, v => T.tones[v]);
    hint();
    renderLibrary();
    renderRecent();
    if (state.thread) renderThread();
  }
  function hint() { $("#modeHint").textContent = T.modeHints[state.mode]; }
  function buildSeg(root, values, current, label, onPick, icons) {
    root.replaceChildren();
    values.forEach(v => {
      const b = el("button"); b.type = "button"; b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(v === current));
      if (icons) b.append(icon(icons[v]));
      b.append(label(v));
      b.addEventListener("click", () => {
        root.querySelectorAll("button").forEach(x => x.setAttribute("aria-checked", String(x === b)));
        onPick(v);
      });
      root.append(b);
    });
  }
  function fillSelect(select, values, current, label) {
    select.replaceChildren(...values.map(v => { const o = el("option", "", label(v)); o.value = v; return o; }));
    select.value = current;
  }

  /* ---------- library (sidebar) and recent searches (home) ---------- */
  const LIB_STEP = 8, RECENT = 6;
  async function loadLibrary() {
    try { state.threads = (await api("/api/s5/threads")).threads; } catch { return; }
    renderLibrary(); renderRecent();
  }
  function dayGroup(t) {
    const now = new Date(), day = 86400000, ms = t.updated * 1000;
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    return ms >= start ? "today" : ms >= start - day ? "yesterday" : ms >= start - 6 * day ? "week" : "older";
  }
  function statusDot(t) {
    if (t.status === "running" || t.status === "queued") return el("span", "dot");
    if (t.status === "error") return el("span", "dot err");
    return null;
  }
  function renderLibrary() {
    const root = $("#lib"), filter = fold($("#libFilter").value.trim());
    const matches = state.threads.filter(t => !filter || fold(t.title).includes(filter));
    const items = filter ? matches : matches.slice(0, state.libLimit);
    root.replaceChildren();
    if (!items.length) { root.append(el("div", "lib-empty", T.noThreads)); return; }
    let last = null;
    items.forEach(t => {
      const g = dayGroup(t);
      if (g !== last) { root.append(el("div", "lib-group", T[g])); last = g; }
      const a = el("a"); a.href = `#/t/${t.id}`; a.title = t.title;
      if (state.thread && state.thread.id === t.id) { a.className = "on"; a.setAttribute("aria-current", "page"); }
      a.append(el("span", "t", t.title));
      const dot = statusDot(t); if (dot) a.append(dot);
      root.append(a);
    });
    const hiddenCount = matches.length - items.length;
    if (hiddenCount > 0) {
      const more = el("button", "link-btn lib-more", `${T.more} (${hiddenCount})`); more.type = "button";
      more.addEventListener("click", () => { state.libLimit += 20; renderLibrary(); });
      root.append(more);
    }
  }
  function renderRecent() {
    const root = $("#recent"), title = $("#recentTitle"), all = $("#recentAll");
    root.replaceChildren();
    if (!state.threads.length) {
      title.textContent = T.tryAsking; all.hidden = true;
      T.examples.forEach(text => {
        const b = el("button", "rc sug"); b.type = "button"; b.append(el("span", "rt", text));
        b.addEventListener("click", () => { const q = $("#q"); q.value = text; autosize(q); q.focus(); });
        root.append(b);
      });
      return;
    }
    title.textContent = T.recent;
    all.hidden = state.threads.length <= RECENT;
    all.textContent = `${T.seeAll} (${state.threads.length})`;
    state.threads.slice(0, RECENT).forEach(t => {
      const a = el("a", "rc"); a.href = `#/t/${t.id}`; a.title = t.title;
      a.append(el("span", "rt", t.title));
      const m = el("span", "rm"), dot = statusDot(t);
      if (dot) m.append(dot);
      m.append(el("span", "", [T.modes[t.mode] || "", relTime(t.updated)].filter(Boolean).join(" · ")));
      if (t.turns > 1) m.append(el("span", "", `· ${t.turns}`));
      a.append(m); root.append(a);
    });
  }
  function relTime(sec) {
    const diff = Date.now() / 1000 - sec, rtf = new Intl.RelativeTimeFormat(state.ui, {numeric: "auto"});
    if (diff < 60) return rtf.format(0, "minute");
    if (diff < 3600) return rtf.format(-Math.round(diff / 60), "minute");
    if (diff < 86400) return rtf.format(-Math.round(diff / 3600), "hour");
    if (diff < 7 * 86400) return rtf.format(-Math.round(diff / 86400), "day");
    return new Date(sec * 1000).toLocaleDateString(state.ui, {day: "numeric", month: "short"});
  }
  function openLibrary() {
    state.libLimit = Math.max(state.libLimit, 40); renderLibrary();
    if (matchMedia("(max-width:860px)").matches) { document.body.classList.add("menu"); $("#scrim").hidden = false; }
    $("#libFilter").focus();
  }

  /* ---------- views ---------- */
  function show(view) {
    ["home", "thread", "missing"].forEach(id => { $(`#${id}`).hidden = id !== view; });
    document.body.classList.remove("menu"); $("#scrim").hidden = true;
  }
  async function route() {
    const match = location.hash.match(/^#\/t\/([a-f0-9]{16})$/);
    closeStreams();
    if (!match) {
      state.thread = null; show("home"); renderLibrary(); renderRecent();
      document.title = "Star5 Search";
      setTimeout(() => $("#q").focus(), 0);
      return;
    }
    try { state.thread = await api(`/api/s5/threads/${match[1]}`); }
    catch (error) { if (String(error.message).includes("404")) { state.thread = null; show("missing"); } return; }
    show("thread"); renderThread(); renderLibrary();
    document.title = `${state.thread.title} · Star5 Search`;
    state.thread.turns.filter(active).forEach(t => follow(t.id));
    const lastTurn = $("#turns").lastElementChild;
    if (lastTurn && state.thread.turns.some(active)) lastTurn.scrollIntoView({block: "start"});
  }
  const active = t => t.status === "queued" || t.status === "running";

  function renderThread() {
    const root = $("#turns");
    root.replaceChildren(...state.thread.turns.map((t, i) => renderTurn(t, i === 0)));
    renderTotal();
  }

  /* ---------- a turn: question, progress, sources, answer, actions ---------- */
  function liveOf(id) {
    if (!state.live.has(id)) state.live.set(id, {phase: null, queries: [], current: null, sources: [], text: "", logs: [], seq: 0});
    return state.live.get(id);
  }
  function renderTurn(turn, first) {
    const node = $("#turnTpl").content.firstElementChild.cloneNode(true);
    node.dataset.id = turn.id;
    node.classList.toggle("chat", turn.kind === "chat");
    node.querySelectorAll("[data-i18n]").forEach(e => { e.textContent = T[e.dataset.i18n] || ""; });
    $(".q", node).textContent = turn.question;
    fillTurn(node, turn, first);
    return node;
  }
  function fillTurn(node, turn, first) {
    const live = liveOf(turn.id), running = active(turn);
    const meta = $(".meta", node); meta.replaceChildren();
    meta.append(el("span", "", turn.kind === "chat" ? T.kinds.chat : T.modes[turn.mode] || turn.mode));
    meta.append(el("span", "", T.langNames[turn.lang] || turn.lang));
    if (turn.status !== "done") meta.append(el("span", "", T.status[turn.status] || turn.status));
    const when = new Date((turn.finished || turn.created) * 1000);
    meta.append(el("span", "", when.toLocaleString(state.ui, {dateStyle: "medium", timeStyle: "short"})));
    if (turn.started && turn.finished) meta.append(el("span", "", duration(turn.finished - turn.started)));
    const usage = running && live.tokens ? live.tokens : turn;
    if (usage.llm_calls) {
      const total = (usage.tokens_in || 0) + (usage.tokens_out || 0);
      const chip = el("span", "tok", T.tokens(compact(total)));
      chip.title = T.tokensTip(full(usage.tokens_in), full(usage.tokens_out), usage.llm_calls);
      meta.append(chip);
    }
    const brief = $(".brief", node), briefText = turn.brief || live.brief || "";
    brief.hidden = !briefText;
    $("p", brief).textContent = briefText;

    renderProgress(node, turn, live, running);
    const sources = running ? live.sources.map(s => ({...s, cited: false})) : turn.sources || [];
    renderSources(node, sources, turn);
    const text = running ? live.text : turn.answer;
    const wrap = $(".answer-wrap", node);
    wrap.hidden = !text;
    if (text) renderMarkdown($(".answer", node), text, sources, running, $(".toc", node));
    const failed = $(".failed", node);
    failed.hidden = !(turn.status === "error" || turn.status === "cancelled");
    if (!failed.hidden) failed.textContent = turn.status === "cancelled" ? T.status.cancelled
      : turn.error === "interrupted" ? T.interrupted : `${T.failed} ${turn.error || ""}`.trim();
    renderActions(node, turn, first, running);
  }
  function duration(s) { s = Math.max(0, Math.round(s)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`; }
  const compact = n => new Intl.NumberFormat(state.ui, {notation: "compact", maximumFractionDigits: 1}).format(n || 0);
  const full = n => new Intl.NumberFormat(state.ui).format(n || 0);
  function renderTotal() {
    const box = $("#threadTotal");
    if (!state.thread) { box.hidden = true; return; }
    let input = 0, output = 0, calls = 0;
    state.thread.turns.forEach(t => {
      const live = active(t) && state.live.get(t.id)?.tokens;
      const u = live || t;
      input += u.tokens_in || 0; output += u.tokens_out || 0; calls += u.llm_calls || 0;
    });
    box.hidden = !calls;
    if (!calls) return;
    box.textContent = T.convTotal(compact(input + output), compact(input), compact(output));
    box.title = T.tokensTip(full(input), full(output), calls);
  }

  function renderProgress(node, turn, live, running) {
    const box = $(".progress", node); box.hidden = !running;
    if (!running) return;
    const steps = $(".steps", box); steps.replaceChildren();
    const order = turn.kind === "chat" ? ["chat"] : ["plan", "search", "read", "write"];
    const at = turn.status === "queued" ? -1 : turn.kind === "chat" ? 0 : Math.max(0, order.indexOf(live.phase || "plan"));
    if (turn.status === "queued") steps.append(Object.assign(el("li", "on", T.status.queued)));
    order.forEach((p, i) => {
      const li = el("li", i < at ? "ok" : i === at ? "on" : "", p === "chat" ? T.chatPhase : T.phases[p]);
      if (p === "search" && live.queries.length) li.append(el("span", "n", ` · ${live.queries.length} ${T.queries}`));
      if (p === "read" && live.sources.length) li.append(el("span", "n", ` · ${T.sourcesN(live.sources.length)}`));
      steps.append(li);
    });
    const q = $(".queries", box); q.hidden = !live.queries.length || at > 2;
    q.replaceChildren(...live.queries.slice(0, 8).map(text => el("li", text === live.current ? "on" : "", text)));
    const elapsed = $(".elapsed", box); elapsed.dataset.start = String(turn.started || turn.created);
    tick(elapsed);
    const cancel = $(".cancel", box);
    cancel.onclick = async () => { cancel.disabled = true; try { await api(`/api/s5/turns/${turn.id}/cancel`, {method: "POST"}); } catch { cancel.disabled = false; } };
    const log = $(".log", box); log.hidden = !live.logs.length;
    $("ol", log).replaceChildren(...live.logs.slice(-60).map(t => el("li", "", t)));
  }
  function tick(e) { const start = Number(e.dataset.start); if (start) e.textContent = duration(Date.now() / 1000 - start); }
  setInterval(() => document.querySelectorAll(".progress:not([hidden]) .elapsed").forEach(tick), 1000);

  /* ---------- sources ---------- */
  const COMMON = new Set(["www", "com", "org", "net", "io", "the", "and", "per", "con", "les", "des", "une", "che", "come", "what", "how", "nel", "del", "della", "pour", "avec", "dans"]);
  function fold(s) { return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase(); }
  function officialChecker(question) {
    const words = fold(question).split(/[^a-z0-9]+/).filter(w => w.length >= 2 && !COMMON.has(w));
    const keys = new Set(words.filter(w => w.length >= 4));
    for (let i = 0; i < words.length - 1; i++) { keys.add(words[i] + words[i + 1]); if (i < words.length - 2) keys.add(words[i] + words[i + 1] + words[i + 2]); }
    const publicSuffix = [".gov", ".gouv.fr", ".gov.it", ".gov.uk", ".edu", ".int", "europa.eu", ".belgium.be", ".fgov.be", ".admin.ch", ".bund.de", ".who.int", ".gc.ca"];
    return host => {
      if (/^(community|forum|forums|discuss|discourse|answers|support-community)\./.test(host)) return false;
      if (publicSuffix.some(s => host === s.slice(1) || host.endsWith(s))) return true;
      const parts = host.split(".");
      const second = parts.length > 2 && ["co", "com", "org", "gov", "ac"].includes(parts[parts.length - 2]) ? parts[parts.length - 3] : parts[parts.length - 2] || "";
      const label = fold(second).replace(/[^a-z0-9]/g, "");
      return label.length >= 4 && keys.has(label);
    };
  }
  function avatar(domain) {
    let h = 0; for (const c of domain) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    const a = el("span", `av c${h % 6}`, (domain.replace(/^www\./, "")[0] || "?")); a.setAttribute("aria-hidden", "true");
    return a;
  }
  function renderSources(node, sources, turn) {
    const box = $(".sources", node); box.hidden = !sources.length;
    if (!sources.length) return;
    const isOfficial = officialChecker(turn.question);
    $(".src-title", box).textContent = `${T.sources} · ${T.sourcesN(sources.length)}`;
    const cited = sources.filter(s => s.cited);
    const number = new Map(cited.map((s, i) => [s.url, i + 1]));
    const cards = $(".cards", box), grouped = $(".grouped", box), toggle = $(".all", box);
    cards.replaceChildren(...sources.slice(0, 40).map(s => {
      const a = el("a", "card"); a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.title = s.url;
      a.append(el("span", "ct", s.title || s.url.replace(/^https?:\/\//, "")));
      const d = el("span", "cd"); d.append(avatar(s.domain || ""), el("span", "dn", s.domain || ""));
      if (isOfficial(s.domain || "")) { const b = el("span", "badge", T.official); b.title = T.officialTip; d.append(b); }
      if (number.has(s.url)) d.append(el("span", "num", String(number.get(s.url))));
      a.append(d);
      return a;
    }));
    const groups = new Map();
    sources.forEach(s => { const k = s.domain || "?"; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(s); });
    const sorted = [...groups].sort((a, b) => (isOfficial(b[0]) - isOfficial(a[0])) || b[1].length - a[1].length);
    grouped.replaceChildren(...sorted.map(([domainName, list]) => {
      const g = el("div", "grp"), h = el("div", "grp-h");
      h.append(avatar(domainName), el("span", "", domainName));
      if (isOfficial(domainName)) { const b = el("span", "badge", T.official); b.title = T.officialTip; h.append(b); }
      h.append(el("span", "cnt", T.sourcesN(list.length)));
      const ul = el("ul");
      list.forEach(s => {
        const li = el("li"), a = el("a", "", s.title || s.url); a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
        li.append(a, el("span", "muted", ` · ${s.cited ? T.cited : T.consulted}`));
        ul.append(li);
      });
      g.append(h, ul); return g;
    }));
    toggle.hidden = sources.length < 2;
    const sync = () => { toggle.textContent = grouped.hidden ? T.showAll : T.showCards; cards.hidden = !grouped.hidden; };
    toggle.onclick = () => { grouped.hidden = !grouped.hidden; sync(); };
    sync();
  }

  /* ---------- answer ---------- */
  function slug(text, used) {
    let s = fold(text).replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "s";
    while (used.has(s)) s += "-";
    used.add(s); return s;
  }
  function renderMarkdown(target, text, sources, streaming, toc) {
    const html = DOMPurify.sanitize(md.makeHtml(text), {FORBID_ATTR: ["style"], FORBID_TAGS: ["style", "form", "input"]});
    const tmp = document.createElement("div"); tmp.innerHTML = html;
    const cited = sources.filter(s => s.cited), number = new Map(cited.map((s, i) => [s.url, i + 1]));
    tmp.querySelectorAll("a[href]").forEach(a => {
      const href = a.getAttribute("href");
      if (!/^https?:\/\//i.test(href)) { a.replaceWith(...a.childNodes); return; }
      a.target = "_blank"; a.rel = "noopener noreferrer";
      if (number.has(href)) a.after(el("sup", "num ref", String(number.get(href))));
    });
    tmp.querySelectorAll("img").forEach(i => i.remove());
    const used = new Set(), heads = [...tmp.querySelectorAll("h2,h3")];
    heads.forEach(h => { h.id = `s-${slug(h.textContent, used)}`; });
    target.replaceChildren(...tmp.childNodes);
    target.classList.toggle("streaming", streaming);
    toc.hidden = streaming || heads.length < 3;
    if (!toc.hidden) {
      $("ol", toc).replaceChildren(...heads.map(h => {
        const li = el("li", h.tagName === "H3" ? "h3" : ""), b = el("button", "link-btn", h.textContent);
        b.type = "button"; b.addEventListener("click", () => document.getElementById(h.id)?.scrollIntoView({behavior: "smooth"}));
        li.append(b); return li;
      }));
    }
  }

  /* ---------- actions ---------- */
  function action(name, label, onClick, href) {
    const a = el(href ? "a" : "button", "act"); if (href) { a.href = href; a.download = ""; } else a.type = "button";
    a.append(icon(name), el("span", "", label)); if (onClick) a.addEventListener("click", onClick);
    return a;
  }
  function renderActions(node, turn, first, running) {
    const box = $(".actions", node); box.replaceChildren();
    if (running) { box.hidden = true; return; }
    if (turn.answer) {
      const copy = action("copy", T.copy, async () => {
        try { await navigator.clipboard.writeText(turn.answer); copy.lastChild.textContent = T.copied; setTimeout(() => { copy.lastChild.textContent = T.copy; }, 1500); } catch { /* clipboard denied */ }
      });
      box.append(copy);
      const files = turn.files || {};
      [["pdf", "PDF"], ["docx", "Word"], ["md", "Markdown"]].forEach(([k, label]) => {
        if (files[k]) box.append(action("pdf", label, null, `/${files[k]}`));
      });
    }
    if (turn.kind === "research") box.append(action("retry", T.retry, () => ask(turn.question, "research", turn.mode)));
    if (first) box.append(Object.assign(action("trash", T.delete, async () => {
      if (!confirm(T.confirmDelete)) return;
      try { await api(`/api/s5/threads/${state.thread.id}`, {method: "DELETE"}); } catch { banner(T.genericError); return; }
      location.hash = "#/"; loadLibrary();
    }), {className: "act danger"}));
    box.hidden = !box.childElementCount;
  }

  /* ---------- live events (Server-Sent Events, resumable) ---------- */
  function closeStreams() { state.streams.forEach(s => s.close()); state.streams.clear(); }
  function follow(turnId) {
    if (state.streams.has(turnId)) return;
    const live = liveOf(turnId);
    const source = new EventSource(`/api/s5/turns/${turnId}/events?after=${live.seq}`);
    state.streams.set(turnId, source);
    let pending = null;
    const refresh = () => { if (pending) return; pending = setTimeout(() => { pending = null; redraw(turnId); }, 250); };
    source.onmessage = message => {
      const event = JSON.parse(message.data);
      live.seq = Number(message.lastEventId) || live.seq;
      switch (event.t) {
        case "status": setTurn(turnId, {status: event.status}); fillStart(turnId); break;
        case "phase": live.phase = event.phase; break;
        case "brief": live.brief = event.text; setTurn(turnId, {brief: event.text}); break;
        case "tokens": live.tokens = {tokens_in: event.tokens_in, tokens_out: event.tokens_out, llm_calls: event.llm_calls}; break;
        case "queries": live.queries = event.items; break;
        case "query": live.current = event.q; if (!live.queries.includes(event.q)) live.queries.push(event.q); break;
        case "source": if (!live.sources.some(s => s.url === event.url)) live.sources.push({url: event.url, domain: event.domain, title: ""}); break;
        case "chunk": live.text += event.text; break;
        case "log": live.logs.push(event.text); break;
        case "done":
          source.close(); state.streams.delete(turnId); state.live.delete(turnId);
          setTurn(turnId, event.turn); redraw(turnId); loadLibrary(); return;
      }
      refresh();
    };
    source.onerror = async () => {
      if (source.readyState !== EventSource.CLOSED) return;
      state.streams.delete(turnId);
      try { await api("/api/s5/config"); } catch { return; }
      setTimeout(() => { if (state.thread && state.thread.turns.some(t => t.id === turnId && active(t))) follow(turnId); }, 3000);
    };
  }
  function setTurn(id, fields) {
    if (!state.thread) return;
    const turn = state.thread.turns.find(t => t.id === id);
    if (turn) Object.assign(turn, fields);
  }
  function fillStart(id) {
    const turn = state.thread && state.thread.turns.find(t => t.id === id);
    if (turn && !turn.started) turn.started = Date.now() / 1000;
  }
  function redraw(id) {
    if (!state.thread) return;
    const index = state.thread.turns.findIndex(t => t.id === id);
    const node = document.querySelector(`.turn[data-id="${id}"]`);
    if (index >= 0 && node) fillTurn(node, state.thread.turns[index], index === 0);
    renderTotal();
  }

  /* ---------- asking ---------- */
  function payload(question, kind, mode) {
    return {question, kind, mode: mode || state.mode, ui: state.ui, lang: state.lang, tone: state.tone, max_results: state.sites};
  }
  async function ask(question, kind, mode) {
    if (!state.thread) return;
    try {
      const started = await api(`/api/s5/threads/${state.thread.id}/turns`, {method: "POST", body: payload(question, kind, mode)});
      state.thread = await api(`/api/s5/threads/${state.thread.id}`);
      renderThread(); follow(started.turn);
      document.querySelector(`.turn[data-id="${started.turn}"]`)?.scrollIntoView({behavior: "smooth", block: "start"});
      loadLibrary();
    } catch (error) { if (error.message !== "expired") banner(T.genericError); }
  }
  async function submitHome(event) {
    event.preventDefault();
    const q = $("#q"), question = q.value.trim(); if (!question) return;
    const button = $("#ask .send"); button.disabled = true;
    try {
      const started = await api("/api/s5/threads", {method: "POST", body: payload(question, "research")});
      q.value = ""; autosize(q);
      location.hash = `#/t/${started.thread}`;
      loadLibrary();
    } catch (error) { if (error.message !== "expired") banner(T.genericError); }
    finally { button.disabled = false; }
  }
  async function submitFollow(event) {
    event.preventDefault();
    const f = $("#fq"), question = f.value.trim(); if (!question) return;
    f.value = ""; autosize(f);
    await ask(question, state.kind, state.kind === "research" ? state.mode : "quick");
  }
  function autosize(t) { t.style.height = "auto"; t.style.height = `${Math.min(t.scrollHeight, window.innerHeight * 0.4)}px`; }
  function enterSubmits(t, form) {
    t.addEventListener("input", () => autosize(t));
    t.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer:fine)").matches) { e.preventDefault(); form.requestSubmit(); }
    });
  }

  /* ---------- wiring ---------- */
  function init() {
    $("#ask").addEventListener("submit", submitHome);
    $("#follow").addEventListener("submit", submitFollow);
    enterSubmits($("#q"), $("#ask")); enterSubmits($("#fq"), $("#follow"));
    $("#optBtn").addEventListener("click", () => {
      const o = $("#opts"); o.hidden = !o.hidden; $("#optBtn").setAttribute("aria-expanded", String(!o.hidden));
    });
    $("#lang").addEventListener("change", e => { state.lang = e.target.value; store.set("s5.lang", state.lang); });
    $("#tone").addEventListener("change", e => { state.tone = e.target.value; store.set("s5.tone", state.tone); });
    const sites = $("#sites"), out = $("#sitesOut");
    const syncSites = () => { out.textContent = sites.value; };
    sites.addEventListener("input", () => { state.sites = Number(sites.value); store.set("s5.sites", sites.value); syncSites(); });
    $("#uiLang").addEventListener("change", e => { state.ui = e.target.value; store.set("s5.ui", state.ui); applyI18n(); });
    $("#libFilter").addEventListener("input", renderLibrary);
    $("#recentAll").addEventListener("click", openLibrary);
    $("#menuBtn").addEventListener("click", () => { document.body.classList.add("menu"); $("#scrim").hidden = false; });
    const closeMenu = () => { document.body.classList.remove("menu"); $("#scrim").hidden = true; };
    $("#scrim").addEventListener("click", closeMenu); $("#sideClose").addEventListener("click", closeMenu);
    $("#newBtn").addEventListener("click", () => { if (location.hash === "#/" || !location.hash) route(); });
    window.addEventListener("hashchange", route);
    applyI18n();
    api("/api/s5/config").then(cfg => {
      if (!store.get("s5.sites", "")) state.sites = cfg.max_results;
      sites.value = String(state.sites); syncSites();
    }).catch(() => { sites.value = String(state.sites); syncSites(); });
    loadLibrary();
    route();
    setInterval(() => { if (state.threads.some(t => t.status === "running" || t.status === "queued")) loadLibrary(); }, 20000);
  }
  init();
})();
