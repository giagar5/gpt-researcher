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
    clip: '<path d="M21 11l-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="2"/>',
    pin: '<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-8 8"/>',
    video: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3z"/>',
    audio: '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
    document: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 13h6M9 17h6"/>',
    x: '<path d="M7 7l10 10M17 7L7 17"/>',
  };
  const icon = name => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.innerHTML = ICON[name]; return s; };

  const MODES = ["fast", "quick", "deep"];
  const MODE_ICON = {fast: "bolt", quick: "layers", deep: "dive"};
  const TONES = ["Objective", "Explanatory", "Analytical", "Critical", "Comparative", "Formal", "Simple", "Casual"];
  const LANGS = ["it", "fr", "en", "de", "es", "nl"];
  const EFFORTS = ["", "low", "medium", "high", "xhigh", "max"];
  const browser = (navigator.language || "it").slice(0, 2);
  const state = {
    ui: store.get("s5.ui", ["it", "fr", "en"].includes(browser) ? browser : "it"),
    mode: store.get("s5.mode", "fast"), kind: store.get("s5.kind", "chat"),
    lang: store.get("s5.lang", "auto"), tone: store.get("s5.tone", "Objective"),
    effort: store.get("s5.effort", ""),
    sites: Number(store.get("s5.sites", "0")) || 8,
    threads: [], thread: null, streams: new Map(), live: new Map(), timers: new Set(), expired: false, libLimit: 8,
  };
  if (state.mode === "detailed") state.mode = "deep";
  if (!MODES.includes(state.mode)) state.mode = "fast";
  if (!EFFORTS.includes(state.effort)) state.effort = "";
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
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch { /* not JSON */ }
      throw Object.assign(new Error(`HTTP ${res.status}`), {status: res.status, detail: String(detail)});
    }
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
    renderModes();
    fillSelect($("#lang"), ["auto", ...LANGS], state.lang, v => v === "auto" ? T.langAuto : T.langNames[v]);
    fillSelect($("#tone"), TONES, state.tone, v => T.tones[v]);
    fillSelect($("#effort"), EFFORTS, state.effort, v => v ? T.efforts[v] : T.effortAuto);
    fillSelect($("#flang"), ["auto", ...LANGS], state.lang, v => v === "auto" ? T.langAuto : T.langNames[v]);
    fillSelect($("#ftone"), TONES, state.tone, v => T.tones[v]);
    fillSelect($("#feffort"), EFFORTS, state.effort, v => v ? T.efforts[v] : T.effortAuto);
    hint();
    renderLibrary();
    renderRecent();
    state.homeComposer?.render(); state.followComposer?.render();
    if (state.thread) renderThread();
  }
  function hint() { $("#modeHint").textContent = T.modeHints[state.mode]; }
  /* The home and follow-up composers share mode and options; picking in one updates both. */
  function renderModes() {
    const pick = m => { state.mode = m; store.set("s5.mode", m); renderModes(); };
    buildSeg($("#modes"), MODES, state.mode, modeLabel, pick, MODE_ICON);
    buildSeg($("#fmodes"), MODES, state.mode, modeLabel, pick, MODE_ICON);
    buildSeg($("#kinds"), ["chat", "research"], state.kind, k => T.kinds[k],
      k => { state.kind = k; store.set("s5.kind", k); renderModes(); }, {chat: "book", research: "globe"});
    $("#fmodes").hidden = state.kind !== "research";
    $("#fmodes").title = T.modeHints[state.mode];
    hint();
  }
  function modeLabel(m) {
    const f = document.createDocumentFragment();
    f.append(T.modes[m], el("span", "seg-time", T.modeTimes[m]));
    return f;
  }
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
    if (view !== "thread") { $("#rail").hidden = true; $("#toBottom").hidden = true; }
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
    scheduleRail();
  }

  /* ---------- conversation timeline (Google Photos-style scrubber) and jump to the end ----------
     Dots sit at the scroll position of their question (big) or answer section (small). Desktop: hovering
     the timeline shows question titles and an instant preview of the point under the pointer. Touch: the
     first tap on the right edge shows the titles, a tap on a title jumps there, dragging scrubs. */
  const RAIL_MAX = 160, LABEL_GAP = 24;
  let railTimer = 0, railFrame = 0, railItems = [], railGeo = {height: 1, max: 1}, railCurrent = 0, railThumb = 0;
  let railDrag = null, railMode = null, railHideTimer = 0, railActiveTimer = 0;
  const railText = (s, n) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);
  function scheduleRail() { clearTimeout(railTimer); railTimer = setTimeout(renderRail, 250); }
  function headerOffset() {
    const m = $(".mtop");
    return m && getComputedStyle(m).display !== "none" ? m.getBoundingClientRect().height : 0;
  }
  function railGeometry() {
    const rail = $("#rail"), doc = document.documentElement;
    const top = headerOffset() + (headerOffset() ? 14 : 24);
    const followTop = Math.min($("#follow").getBoundingClientRect().top, window.innerHeight);
    const bottom = Math.max(24, window.innerHeight - followTop + 16);
    rail.style.top = `${top}px`; rail.style.bottom = `${bottom}px`;
    railGeo = {height: Math.max(40, window.innerHeight - top - bottom), max: Math.max(1, doc.scrollHeight - window.innerHeight),
      full: Math.max(1, doc.scrollHeight)};
    return railGeo;
  }
  const targetOf = node => Math.max(0, node.getBoundingClientRect().top + window.scrollY - headerOffset() - 8);
  function renderRail() {
    const rail = $("#rail");
    if (!state.thread || $("#thread").hidden) { rail.hidden = true; railItems = []; return; }
    let items = [];
    document.querySelectorAll("#turns .turn").forEach((turn, qi) => {
      items.push({node: turn, q: true, qi, label: $(".q", turn).textContent.trim()});
      turn.querySelectorAll(".answer h2").forEach(h => items.push({node: h, q: false, qi, label: h.textContent.trim()}));
    });
    if (items.length > RAIL_MAX) items = items.filter(i => i.q);
    const g = railGeometry();
    rail.hidden = items.length < 2 || g.max < 80;
    if (rail.hidden) { railItems = []; return; }
    let lastLabel = -Infinity;
    railItems = items.map((item, i) => {
      item.target = targetOf(item.node);
      // Positions over the whole conversation (like a minimap), so the last screen does not pile up at the bottom.
      item.y = Math.min(1, (item.node.getBoundingClientRect().top + window.scrollY) / g.full) * g.height;
      // "dq"/"ds", not "turn": a .turn class would pick up the turn styles and break turn counts.
      const b = el("button", `dot ${item.q ? "dq" : "ds"}`); b.type = "button";
      b.dataset.i = String(i); b.style.top = `${item.y}px`;
      b.setAttribute("aria-label", railText(item.label, 140));
      b.addEventListener("click", e => { if (e.detail === 0) jumpTo(item); });   // keyboard only; pointers are handled by the rail
      if (item.q && item.y - lastLabel >= LABEL_GAP) { b.append(el("span", "lbl", railText(item.label, 70))); lastLabel = item.y; }
      item.el = b;
      return item;
    });
    $(".rail-items", rail).replaceChildren(...railItems.map(item => item.el));
    onScroll();
  }
  function itemAt(y) {
    let found = railItems[0];
    for (const item of railItems) { if (item.y <= y + 1) found = item; else break; }
    return found;
  }
  function nearestItem(y, within) {
    let best = null, distance = within;
    railItems.forEach(item => { const d = Math.abs(item.y - y); if (d <= distance) { distance = d; best = item; } });
    return best;
  }
  function setHot(item) {
    $("#rail").querySelectorAll(".dot.hot").forEach(d => d.classList.remove("hot"));
    if (item) item.el.classList.add("hot");
  }
  function showFocus(item, y) {
    const focus = $("#rail .rail-focus");
    if (!item) { focus.hidden = true; railItems.forEach(it => it.el.classList.remove("covered")); return; }
    const question = item.q ? item : railItems.find(it => it.q && it.qi === item.qi);
    focus.replaceChildren(el("b", "", railText(question ? question.label : item.label, 400)));
    if (!item.q) focus.append(el("span", "", `› ${railText(item.label, 200)}`));
    focus.hidden = false;
    const half = focus.offsetHeight / 2;
    focus.style.top = `${Math.max(half - 6, Math.min(railGeo.height - half + 6, y))}px`;
    railItems.forEach(it => it.el.classList.toggle("covered", Math.abs(it.y - y) < Math.max(20, half)));
  }
  // While scrubbing the preview follows the band; otherwise it sits next to the current dot.
  const showCurrent = () => {
    const item = railItems[railCurrent];
    if (item) showFocus(item, railDrag && railDrag.moved ? railThumb : item.y);
  };
  function jumpTo(item) { window.scrollTo({top: targetOf(item.node), behavior: "smooth"}); }
  function scrubTo(y, smooth) {
    // The pointer marks the middle of the visible part, as on the minimap band.
    const top = Math.max(0, Math.min(1, y / railGeo.height)) * railGeo.full - window.innerHeight / 2;
    window.scrollTo({top: Math.max(0, Math.min(railGeo.max, top)), behavior: smooth ? "smooth" : "auto"});
  }
  function openRail(mode) { clearTimeout(railHideTimer); railMode = mode; $("#rail").classList.add("open"); }
  function closeRail(delay = 0) {
    clearTimeout(railHideTimer);
    railHideTimer = setTimeout(() => {
      if (railDrag) return;
      railMode = null; $("#rail").classList.remove("open"); setHot(null); showFocus(null);
    }, delay);
  }
  function setupRail() {
    const rail = $("#rail");
    rail.append(el("div", "rail-track"), el("div", "rail-thumb"), el("div", "rail-items"), el("div", "rail-focus"));
    $(".rail-focus", rail).hidden = true;
    const railY = e => e.clientY - rail.getBoundingClientRect().top;
    rail.addEventListener("pointerenter", e => { if (e.pointerType === "mouse") openRail("hover"); });
    rail.addEventListener("pointerleave", e => { if (e.pointerType === "mouse" && !railDrag) closeRail(); });
    rail.addEventListener("pointerdown", e => {
      if (!railItems.length || (e.pointerType === "mouse" && e.button !== 0)) return;
      const wasOpen = rail.classList.contains("open") && railMode === "touch";
      const touch = e.pointerType !== "mouse";
      openRail(touch ? "touch" : "hover");
      railDrag = {id: e.pointerId, y0: e.clientY, moved: false, wasOpen, touch, dot: e.target.closest(".dot")};
      rail.setPointerCapture(e.pointerId);
      e.preventDefault();
      if (touch) showCurrent();
    });
    rail.addEventListener("pointermove", e => {
      const y = railY(e);
      if (railDrag && railDrag.id === e.pointerId) {
        if (!railDrag.moved && Math.abs(e.clientY - railDrag.y0) > 5) { railDrag.moved = true; setHot(null); }
        if (railDrag.moved) scrubTo(y, false);
        return;
      }
      if (e.pointerType === "mouse") {
        const dot = e.target.closest(".dot");
        const near = dot ? railItems[Number(dot.dataset.i)] : nearestItem(y, 10);
        setHot(near);
        showFocus(near || itemAt(y), near ? near.y : y);
      }
    });
    const end = e => {
      if (!railDrag || railDrag.id !== e.pointerId) return;
      const drag = railDrag; railDrag = null;
      if (!drag.moved && e.type === "pointerup") {
        if (drag.touch && !drag.wasOpen) { showCurrent(); closeRail(3500); return; }   // first tap: just show the titles
        const item = drag.dot ? railItems[Number(drag.dot.dataset.i)] : nearestItem(railY(e), drag.touch ? 22 : 12);
        if (item) jumpTo(item); else scrubTo(railY(e), true);
      }
      if (drag.touch) closeRail(1800);
      else if (!rail.matches(":hover")) closeRail();
    };
    rail.addEventListener("pointerup", end);
    rail.addEventListener("pointercancel", end);
    document.addEventListener("pointerdown", e => { if (railMode && !rail.contains(e.target)) closeRail(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && railMode) closeRail(); });
  }
  function onScroll() {
    cancelAnimationFrame(railFrame);
    railFrame = requestAnimationFrame(() => {
      if ($("#thread").hidden) { $("#toBottom").hidden = true; return; }
      const doc = document.documentElement, rail = $("#rail");
      const fromBottom = doc.scrollHeight - (window.scrollY + window.innerHeight);
      $("#toBottom").hidden = fromBottom < 400;
      if (rail.hidden || !railItems.length) return;
      railGeo.max = Math.max(1, doc.scrollHeight - window.innerHeight);
      railGeo.full = Math.max(1, doc.scrollHeight);
      // The band shows which part of the conversation is on screen.
      const bandTop = (window.scrollY / railGeo.full) * railGeo.height;
      const bandHeight = Math.max(10, (window.innerHeight / railGeo.full) * railGeo.height);
      railThumb = bandTop + bandHeight / 2;
      const thumb = $(".rail-thumb", rail);
      thumb.style.top = `${bandTop}px`; thumb.style.height = `${bandHeight}px`;
      let current = 0;
      railItems.forEach((item, i) => { if (item.target <= window.scrollY + 40) current = i; });
      if (fromBottom < 4) current = railItems.length - 1;
      railCurrent = current;
      railItems.forEach((item, i) => {
        item.el.classList.toggle("on", i === current);
        if (i === current) item.el.setAttribute("aria-current", "location"); else item.el.removeAttribute("aria-current");
      });
      if ((railDrag && railDrag.moved) || railMode === "touch") showCurrent();
      rail.classList.add("active");
      clearTimeout(railActiveTimer);
      railActiveTimer = setTimeout(() => rail.classList.remove("active"), 1400);
    });
  }
  function toEnd() { window.scrollTo({top: document.documentElement.scrollHeight, behavior: "smooth"}); }

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
    if (turn.effort) meta.append(el("span", "", `${T.effort}: ${T.efforts[turn.effort] || turn.effort}`));
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
    renderAttachments($(".att-row", node), turn);
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
    const order = [...(turn.attachments || []).length ? ["files"] : [],
      ...(turn.kind === "chat" ? ["chat"] : ["plan", "search", "read", "write"])];
    let at = turn.status === "queued" ? -1 : Math.max(0, order.indexOf(live.phase || order[0]));
    if (live.phase === "files" && live.files && live.files.done === live.files.total) at += 1;
    if (turn.status === "queued") steps.append(Object.assign(el("li", "on", T.status.queued)));
    order.forEach((p, i) => {
      const li = el("li", i < at ? "ok" : i === at ? "on" : "", p === "chat" ? T.chatPhase : T.phases[p]);
      if (p === "files" && live.files) li.append(el("span", "n", ` · ${live.files.done}/${live.files.total}`));
      if (p === "search" && live.queries.length) li.append(el("span", "n", ` · ${live.queries.length} ${T.queries}`));
      if (p === "read" && live.sources.length) li.append(el("span", "n", ` · ${T.sourcesN(live.sources.length)}`));
      steps.append(li);
    });
    const q = $(".queries", box); q.hidden = !live.queries.length || order[at] === "write";
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
  const ENGINE_NAMES = {brave: "Brave", bing: "Bing", duckduckgo: "DuckDuckGo"};
  function renderEngines(root, stats) {
    const names = Object.keys(ENGINE_NAMES).filter(n => stats[n]);
    root.hidden = !names.length;
    root.replaceChildren(...names.map(n => {
      const s = stats[n], chip = el("span", "eng");
      chip.append(el("i", `edot e-${n}`), el("b", "", ENGINE_NAMES[n]), el("span", "", T.engineUsed(s.used)));
      const problems = (s.timeouts ? ` · ${T.engineTimeouts(s.timeouts)}` : "") + (s.errors ? ` · ${T.engineErrors(s.errors)}` : "");
      if (problems || !s.used) chip.classList.add("warn");
      chip.title = `${ENGINE_NAMES[n]}: ${T.engineTip(s.used, s.read, s.cited, s.queries)}${problems}`;
      return chip;
    }));
  }
  function renderSources(node, sources, turn) {
    const box = $(".sources", node); box.hidden = !sources.length;
    if (!sources.length) return;
    const isOfficial = officialChecker(turn.question);
    $(".src-title", box).textContent = `${T.sources} · ${T.sourcesN(sources.length)}`;
    const cited = sources.filter(s => s.cited);
    const number = new Map(cited.map((s, i) => [s.url, i + 1]));
    const cards = $(".cards", box), grouped = $(".grouped", box), toggle = $(".all", box);
    renderEngines($(".engines", box), turn.engines || {});
    cards.replaceChildren(...sources.slice(0, 40).map(s => {
      const a = el("a", "card"); a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.title = s.engines && s.engines.length ? `${s.url}\n${T.foundBy}: ${s.engines.map(e => ENGINE_NAMES[e] || e).join(", ")}` : s.url;
      a.append(el("span", "ct", s.title || s.url.replace(/^https?:\/\//, "")));
      const d = el("span", "cd"); d.append(avatar(s.domain || ""), el("span", "dn", s.domain || ""));
      if (isOfficial(s.domain || "")) { const b = el("span", "badge", T.official); b.title = T.officialTip; d.append(b); }
      if (s.engines && s.engines.length) {
        const dots = el("span", "edots");
        s.engines.forEach(e => dots.append(el("i", `edot e-${e}`)));
        d.append(dots);
      }
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
        case "files": live.files = {done: event.done, total: event.total}; break;
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
    scheduleRail();
  }

  /* ---------- attachments and location ---------- */
  const ACCEPT = ["image/*", "video/*", "audio/*", ".heic", ".heif", ".pdf", ".docx", ".pptx", ".xlsx", ".csv",
    ".txt", ".md", ".html", ".htm", ".epub", ".m4a", ".opus", ".amr", ".mkv"].join(",");
  const MAX_ATTACH = 10, MAX_RECORD = 30 * 60;
  const size = n => n >= 1048576 ? `${(n / 1048576).toFixed(n >= 10485760 ? 0 : 1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
  function kindOf(file) {
    const t = file.type || "";
    return t.startsWith("image/") || /\.(heic|heif)$/i.test(file.name) ? "image" : t.startsWith("video/") ? "video"
      : t.startsWith("audio/") ? "audio" : "document";
  }
  function uploadError(error) {
    if (error.status === 413 && error.detail.startsWith("too_large:")) return T.tooLarge(error.detail.split(":")[1]);
    if (error.status === 415) return T.unsupported;
    if (error.status === 507) return T.storage;
    return T.uploadFailed;
  }
  function locLabel(loc) {
    return `${loc.label || T.here} (±${loc.accuracy < 1000 ? `${loc.accuracy} m` : `${(loc.accuracy / 1000).toFixed(1)} km`})`;
  }

  function makeComposer(form) {
    const c = {form, items: [], location: null, locating: false, recorder: null, list: $(".attach-list", form)};
    const picker = $(".picker", form), button = act => $(`.tool[data-act="${act}"]`, form);
    picker.accept = ACCEPT;

    c.render = () => {
      c.list.replaceChildren();
      c.items.forEach(item => {
        const chip = el("div", `att-chip ${item.state}`);
        if (item.preview) { const img = el("img", "thumb"); img.src = item.preview; img.alt = ""; chip.append(img); }
        else chip.append(icon(item.kind));
        const text = el("span", "att-text");
        text.append(el("span", "att-name", item.name));
        const status = item.state === "recording" ? `${T.recording} ${duration(item.seconds || 0)}`
          : item.state === "uploading" ? `${T.uploading} ${Math.floor(item.progress * 100)}%`
          : item.state === "processing" ? T.processingAtt : item.state === "error" ? item.error : size(item.size);
        text.append(el("span", "att-status", status));
        chip.append(text);
        if (item.state === "uploading") { const bar = el("span", "att-bar"); bar.style.width = `${Math.round(item.progress * 100)}%`; chip.append(bar); }
        chip.title = item.state === "error" ? `${item.name}: ${item.error}` : item.name;
        if (item.state !== "recording") {
          const x = el("button", "att-x"); x.type = "button"; x.title = T.removeAtt; x.setAttribute("aria-label", T.removeAtt);
          x.append(icon("x")); x.addEventListener("click", () => c.remove(item)); chip.append(x);
        }
        c.list.append(chip);
      });
      if (c.locating || c.location || c.locError) {
        const chip = el("div", `att-chip loc ${c.locating ? "processing" : c.locError ? "error" : "ready"}`);
        chip.append(icon("pin"));
        const text = el("span", "att-text");
        text.append(el("span", "att-name", c.locating ? T.locating : c.locError || locLabel(c.location)));
        chip.append(text);
        if (!c.locating) {
          const x = el("button", "att-x"); x.type = "button"; x.title = T.removeLocation; x.setAttribute("aria-label", T.removeLocation);
          x.append(icon("x")); x.addEventListener("click", () => { c.location = null; c.locError = ""; c.render(); }); chip.append(x);
        }
        c.list.append(chip);
      }
      c.list.hidden = !c.list.childElementCount;
      button("record").classList.toggle("rec", !!c.recorder);
      button("record").replaceChildren(icon(c.recorder ? "stop" : "mic"));
      button("record").title = c.recorder ? T.stopRecord : T.record;
      button("locate").classList.toggle("on", !!c.location);
    };

    c.add = files => {
      for (const file of files) {
        if (c.items.length >= MAX_ATTACH) { banner(T.tooMany(MAX_ATTACH)); break; }
        const item = {key: Math.random().toString(36).slice(2), file, name: file.name || "file", size: file.size,
          kind: kindOf(file), state: "uploading", progress: 0, abort: new AbortController()};
        if (item.kind === "image" && /^image\/(jpeg|png|webp|gif)$/.test(file.type)) item.preview = URL.createObjectURL(file);
        c.items.push(item);
        upload(item);
      }
      c.render();
    };

    async function upload(item) {
      try {
        const init = await api("/api/s5/uploads", {method: "POST",
          body: {name: item.name, size: item.size, mime: item.file.type || "", ui: state.ui}});
        item.id = init.id; item.kind = init.kind;
        for (let offset = 0; offset < item.size; offset += init.chunk) {
          if (!c.items.includes(item)) return;
          const res = await fetch(`/api/s5/uploads/${item.id}?offset=${offset}`, {method: "PUT",
            body: item.file.slice(offset, offset + init.chunk), signal: item.abort.signal, credentials: "same-origin",
            redirect: "manual", headers: {"X-Star5": "1", "Content-Type": "application/octet-stream"}});
          if (res.type === "opaqueredirect" || res.status === 401) { expired(); throw new Error("expired"); }
          if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), {status: res.status, detail: ""});
          item.progress = Math.min(1, (offset + init.chunk) / item.size); c.render();
        }
        const done = await api(`/api/s5/uploads/${item.id}/complete`, {method: "POST"});
        item.state = done.state === "ready" ? "ready" : done.state === "error" ? "error" : "processing";
        item.error = done.error; c.render();
        poll(item);
      } catch (error) {
        if (error.name === "AbortError" || !c.items.includes(item)) return;
        item.state = "error"; item.error = uploadError(error); c.render();
      }
    }

    async function poll(item) {
      while (item.state === "processing" && c.items.includes(item)) {
        await new Promise(r => setTimeout(r, 3000));
        try {
          const s = await api(`/api/s5/uploads/${item.id}`);
          if (s.state === "ready" || s.state === "error") { item.state = s.state; item.error = s.error ? T.processFailed : ""; c.render(); }
        } catch { return; }
      }
    }

    c.remove = item => {
      c.items = c.items.filter(i => i !== item);
      item.abort.abort();
      if (item.preview) URL.revokeObjectURL(item.preview);
      if (item.id) api(`/api/s5/uploads/${item.id}`, {method: "DELETE"}).catch(() => {});
      c.render();
    };

    c.toggleRecord = async () => {
      if (c.recorder) { c.recorder.stop(); return; }
      if (!window.MediaRecorder || !navigator.mediaDevices) { banner(T.micDenied); return; }
      let stream;
      try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
      catch { banner(T.micDenied); return; }
      const mime = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"].find(m => MediaRecorder.isTypeSupported(m)) || "";
      const recorder = new MediaRecorder(stream, mime ? {mimeType: mime} : undefined);
      const chunks = [], started = Date.now();
      const item = {key: "rec", name: T.recording, kind: "audio", state: "recording", seconds: 0, abort: new AbortController()};
      c.items.push(item); c.recorder = recorder;
      const timer = setInterval(() => {
        item.seconds = (Date.now() - started) / 1000; c.render();
        if (item.seconds >= MAX_RECORD) recorder.stop();
      }, 1000);
      recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = () => {
        clearInterval(timer); stream.getTracks().forEach(t => t.stop());
        c.recorder = null; c.items = c.items.filter(i => i !== item);
        const type = (recorder.mimeType || mime || "audio/webm").split(";")[0];
        const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
        const stamp = new Date().toTimeString().slice(0, 5).replace(":", "");
        if (chunks.length) c.add([new File(chunks, `${T.recordingFile}-${stamp}.${ext}`, {type})]);
        else c.render();
      };
      recorder.start(1000);
      c.render();
    };

    c.toggleLocation = () => {
      if (c.location || c.locError) { c.location = null; c.locError = ""; c.render(); return; }
      if (!navigator.geolocation) { c.locError = T.locUnavailable; c.render(); return; }
      c.locating = true; c.render();
      navigator.geolocation.getCurrentPosition(async pos => {
        const loc = {lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: Math.round(pos.coords.accuracy || 0)};
        try { Object.assign(loc, await api("/api/s5/geocode", {method: "POST", body: {...loc, ui: state.ui}})); }
        catch { loc.label = ""; }
        c.locating = false; c.location = loc; c.render();
      }, error => {
        c.locating = false;
        c.locError = error.code === 1 ? T.locDenied : error.code === 3 ? T.locTimeout : T.locUnavailable;
        c.render();
      }, {enableHighAccuracy: true, timeout: 20000, maximumAge: 60000});
    };

    c.busy = () => c.items.some(i => i.state === "uploading" || i.state === "recording") || c.locating;
    c.extras = () => ({
      attachments: c.items.filter(i => i.id && (i.state === "processing" || i.state === "ready")).map(i => i.id),
      location: c.location ? {lat: c.location.lat, lon: c.location.lon, accuracy: c.location.accuracy, label: c.location.label || ""} : null,
    });
    c.reset = () => {
      c.items.forEach(i => { if (i.preview) URL.revokeObjectURL(i.preview); });
      c.items = []; c.location = null; c.locError = ""; c.render();
    };

    button("attach").addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => { c.add([...picker.files]); picker.value = ""; });
    button("record").addEventListener("click", c.toggleRecord);
    button("locate").addEventListener("click", c.toggleLocation);
    form.addEventListener("dragover", e => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); form.classList.add("drop"); } });
    form.addEventListener("dragleave", () => form.classList.remove("drop"));
    form.addEventListener("drop", e => { if (e.dataTransfer?.files.length) { e.preventDefault(); form.classList.remove("drop"); c.add([...e.dataTransfer.files]); } });
    form.addEventListener("paste", e => { if (e.clipboardData?.files.length) { e.preventDefault(); c.add([...e.clipboardData.files]); } });
    button("attach").append(icon("clip")); button("locate").append(icon("pin"));
    c.render();
    return c;
  }

  function renderAttachments(row, turn) {
    row.replaceChildren();
    (turn.attachments || []).forEach(a => {
      const link = el("a", "att"); link.href = `/api/s5/uploads/${a.id}/file`; link.title = `${a.name} · ${size(a.size)}`;
      if (a.kind === "image" || a.kind === "video") {
        const img = el("img", "thumb"); img.alt = ""; img.loading = "lazy"; img.src = `/api/s5/uploads/${a.id}/thumb`;
        img.addEventListener("error", () => img.replaceWith(icon(a.kind)), {once: true});
        link.append(img);
      } else link.append(icon(a.kind));
      link.append(el("span", "att-name", a.name));
      row.append(link);
    });
    if (turn.location) {
      const loc = turn.location, link = el("a", "att loc");
      link.href = `https://www.openstreetmap.org/?mlat=${loc.lat}&mlon=${loc.lon}#map=15/${loc.lat}/${loc.lon}`;
      link.target = "_blank"; link.rel = "noopener noreferrer"; link.title = T.openMap;
      link.append(icon("pin"), el("span", "att-name", locLabel(loc)));
      row.append(link);
    }
    row.hidden = !row.childElementCount;
  }

  /* ---------- asking ---------- */
  function payload(question, kind, mode, composer) {
    return {question, kind, mode: mode || state.mode, ui: state.ui, lang: state.lang, tone: state.tone,
      effort: state.effort, max_results: state.sites, ...(composer ? composer.extras() : {})};
  }
  async function ask(question, kind, mode, composer) {
    if (!state.thread) return false;
    try {
      const started = await api(`/api/s5/threads/${state.thread.id}/turns`, {method: "POST", body: payload(question, kind, mode, composer)});
      if (composer) composer.reset();
      state.thread = await api(`/api/s5/threads/${state.thread.id}`);
      renderThread(); follow(started.turn);
      document.querySelector(`.turn[data-id="${started.turn}"]`)?.scrollIntoView({behavior: "smooth", block: "start"});
      loadLibrary();
      return true;
    } catch (error) { if (error.message !== "expired") banner(T.genericError); return false; }
  }
  async function submitHome(event) {
    event.preventDefault();
    const q = $("#q"), question = q.value.trim(); if (!question) return;
    if (state.homeComposer.busy()) { banner(T.waitUploads); return; }
    const button = $("#ask .send"); button.disabled = true;
    try {
      const started = await api("/api/s5/threads", {method: "POST", body: payload(question, "research", null, state.homeComposer)});
      q.value = ""; autosize(q); state.homeComposer.reset(); $("#banner").hidden = true;
      location.hash = `#/t/${started.thread}`;
      loadLibrary();
    } catch (error) { if (error.message !== "expired") banner(T.genericError); }
    finally { button.disabled = false; }
  }
  async function submitFollow(event) {
    event.preventDefault();
    const f = $("#fq"), question = f.value.trim(); if (!question) return;
    if (state.followComposer.busy()) { banner(T.waitUploads); return; }
    if (await ask(question, state.kind, state.kind === "research" ? state.mode : "quick", state.followComposer)) {
      f.value = ""; autosize(f); $("#banner").hidden = true;
    }
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
    state.homeComposer = makeComposer($("#ask"));
    state.followComposer = makeComposer($("#follow"));
    $("#ask").addEventListener("submit", submitHome);
    $("#follow").addEventListener("submit", submitFollow);
    enterSubmits($("#q"), $("#ask")); enterSubmits($("#fq"), $("#follow"));
    const toggleOpts = (button, panel) => button.addEventListener("click", () => {
      panel.hidden = !panel.hidden; button.setAttribute("aria-expanded", String(!panel.hidden));
    });
    toggleOpts($("#optBtn"), $("#opts")); toggleOpts($("#fOptBtn"), $("#fopts"));
    // Each option has a control in both panels; a change updates state, storage and the twin control.
    const pair = (a, b, key, field) => [a, b].forEach((select, i) => select.addEventListener("change", () => {
      state[field] = select.value; store.set(key, select.value); [a, b][1 - i].value = select.value;
    }));
    pair($("#lang"), $("#flang"), "s5.lang", "lang");
    pair($("#tone"), $("#ftone"), "s5.tone", "tone");
    pair($("#effort"), $("#feffort"), "s5.effort", "effort");
    const sites = $("#sites"), fsites = $("#fsites");
    const syncSites = () => {
      sites.value = fsites.value = String(state.sites);
      $("#sitesOut").textContent = $("#fsitesOut").textContent = String(state.sites);
    };
    [sites, fsites].forEach(input => input.addEventListener("input", () => {
      state.sites = Number(input.value); store.set("s5.sites", input.value); syncSites();
    }));
    $("#toBottom").addEventListener("click", toEnd);
    setupRail();
    window.addEventListener("scroll", onScroll, {passive: true});
    window.addEventListener("resize", () => { onScroll(); scheduleRail(); });
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
      syncSites();
    }).catch(syncSites);
    loadLibrary();
    route();
    setInterval(() => { if (state.threads.some(t => t.status === "running" || t.status === "queued")) loadLibrary(); }, 20000);
  }
  init();
})();
