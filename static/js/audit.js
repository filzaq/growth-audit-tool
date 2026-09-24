/* Growth Audit - live progress (SSE) and results view. */
(function () {
  "use strict";

  const app = document.getElementById("app");
  const cfg = app.dataset;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ---------------------------------------------------------------- helpers
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /** Render untrusted markdown (it comes from web research) to sanitized HTML. */
  function md(text) {
    if (!text) return "";
    if (window.marked && window.DOMPurify) {
      return DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: false }));
    }
    return "<p>" + esc(text).replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>") + "</p>";
  }
  /** Inline markdown (no wrapping <p>) for short fields. */
  function mdInline(text) {
    if (window.marked && window.DOMPurify) return DOMPurify.sanitize(marked.parseInline(text || ""));
    return esc(text);
  }
  const fmtClock = (secs) => {
    secs = Math.max(0, Math.floor(secs));
    const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
    const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  };
  function toast(msg) {
    let el = $(".toast");
    if (!el) { el = document.createElement("div"); el.className = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 2200);
  }
  const ICON = {
    copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>',
    check: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    download: '<svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
    plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
    spark: '<svg viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/></svg>',
    pen: '<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4z"/></svg>',
  };

  // ======================================================= LIVE PROGRESS VIEW
  const progress = {
    startedAt: null,       // client clock time matching the audit's t=0
    finishedT: null,
    prepDone: false,
    layers: {},            // index -> state
    reportState: "pending",
    runningLayer: null,
    logCount: 0,
  };

  function setStep(key, state) {
    const el = $(`.step[data-step="${key}"]`);
    if (el && el.dataset.state !== state) el.dataset.state = state;
  }

  function updateBar(label) {
    let pct = 2;
    if (progress.prepDone) pct = 6;
    for (let i = 1; i <= 5; i++) {
      const s = progress.layers[i];
      if (s === "done" || s === "failed") pct += 17;
      else if (s === "running") pct += 5;
    }
    if (progress.reportState === "running") pct = Math.max(pct, 93);
    if (progress.reportState === "done") pct = 100;
    $("#progress-fill").style.width = pct + "%";
    $("#progress-pct").textContent = pct + "%";
    if (label) $("#progress-label").textContent = label;
  }

  function tickClock() {
    if (progress.startedAt === null) return;
    const secs = progress.finishedT !== null ? progress.finishedT : (Date.now() - progress.startedAt) / 1000;
    $("#clock").textContent = fmtClock(secs);
  }

  const GLYPH = { step: "▸", info: "·", activity: "→", start: "◌", done: "✓", warn: "!", error: "✗" };

  function appendLog(ev) {
    const log = $("#log");
    const prev = $(".cursor-line", log);
    if (prev) prev.classList.remove("cursor-line");
    const li = document.createElement("li");
    li.className = `lvl-${ev.level} cursor-line`;
    li.innerHTML = `<span class="ts">${fmtClock(ev.t)}</span><span class="glyph">${GLYPH[ev.level] || "·"}</span><span class="msg">${esc(ev.text)}</span>`;
    log.appendChild(li);
    if (++progress.logCount > 1500) log.firstElementChild.remove();
    if ($("#autoscroll").checked) log.scrollTop = log.scrollHeight;

    // Mirror what the agent is doing onto the running layer's card.
    if (ev.level === "activity" && progress.runningLayer) {
      const act = $(`.layer-card[data-layer="${progress.runningLayer}"] .layer-activity`);
      act.hidden = false;
      act.textContent = ev.text.trim();
    }
  }

  function onStep(ev) {
    if (ev.phase === "prep") {
      setStep("prep", "active");
      updateBar(ev.title);
    } else if (ev.phase === "report") {
      progress.reportState = "running";
      setStep("report", "active");
      updateBar("Synthesizing the final report");
    }
  }

  function onLayer(ev) {
    const i = ev.index;
    const card = $(`.layer-card[data-layer="${i}"]`);
    if (!progress.prepDone) { progress.prepDone = true; setStep("prep", "done"); }
    progress.layers[i] = ev.status;

    if (ev.status === "running") {
      progress.runningLayer = i;
      card.dataset.state = "running";
      $(".layer-state", card).textContent = "Researching";
      setStep("L" + i, "active");
      updateBar(`Layer ${i} of 5 · ${$("h3", card).textContent}`);
      return;
    }
    progress.runningLayer = null;
    card.dataset.state = ev.status;
    $(".layer-activity", card).hidden = true;
    $(".layer-state", card).textContent = ev.status === "done" ? "Complete" : "Failed";
    setStep("L" + i, ev.status === "done" ? "done" : "failed");
    const summary = $(".layer-summary", card);
    summary.innerHTML = ev.status === "done" ? md(ev.summary) : `<p>${esc(ev.summary || "This layer could not be completed.")}</p>`;
    summary.hidden = false;
    // Long summaries are clamped with a fade; click (or the toggle) to read the rest.
    requestAnimationFrame(() => {
      if (summary.scrollHeight <= summary.clientHeight + 4) { summary.classList.add("expanded"); return; }
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "link-btn layer-summary-toggle";
      toggle.textContent = "Show full summary";
      const flip = () => {
        const open = summary.classList.toggle("expanded");
        toggle.textContent = open ? "Show less" : "Show full summary";
      };
      toggle.addEventListener("click", flip);
      summary.addEventListener("click", () => { if (!summary.classList.contains("expanded")) flip(); });
      summary.after(toggle);
    });
    updateBar(`Layer ${i} ${ev.status === "done" ? "complete" : "failed"}`);
  }

  function onComplete(ev) {
    progress.finishedT = ev.t;
    tickClock();
    document.body.classList.add("is-finished");
    if (ev.status === "error") {
      setStep("report", "failed");
      $("#run-state").textContent = "Audit failed";
      const alert = document.createElement("div");
      alert.className = "alert alert-danger";
      alert.setAttribute("role", "alert");
      alert.innerHTML = `<strong>The audit stopped.</strong> ${esc(ev.message)} <a href="/">Start a new audit</a>`;
      $(".run-head").after(alert);
      return;
    }
    progress.reportState = "done";
    setStep("report", "done");
    $("#run-state").textContent = "Audit complete";
    updateBar("Complete · building results");
    // Let the final check-mark animation land before switching views.
    setTimeout(loadResults, 1100);
  }

  function startStream() {
    const es = new EventSource(cfg.eventsUrl);
    const handle = (fn) => (e) => {
      const ev = JSON.parse(e.data);
      if (progress.startedAt === null) progress.startedAt = Date.now() - ev.t * 1000;
      fn(ev);
    };
    es.addEventListener("log", handle(appendLog));
    es.addEventListener("step", handle(onStep));
    es.addEventListener("layer", handle(onLayer));
    es.addEventListener("keywords", handle((ev) => {
      $("#keywords").hidden = false;
      $("#keyword-chips").innerHTML = ev.keywords.map((k, n) => `<span class="chip" style="animation-delay:${n * 60}ms">${esc(k)}</span>`).join("");
    }));
    es.addEventListener("complete", handle((ev) => { es.close(); onComplete(ev); }));
    // The browser retries dropped streams by itself; it gives up (CLOSED) only on errors such as
    // an expired sign-in, so point the user at a reload instead of freezing silently.
    es.addEventListener("error", () => {
      if (es.readyState !== EventSource.CLOSED) return;
      appendLog({ level: "error", t: 0, text: "Lost connection to the server. Reload the page to reconnect (you may need to sign in again)." });
    });
    setInterval(tickClock, 1000);
  }

  // ============================================================ RESULTS VIEW
  let result = null;
  const pendingSaves = new Map();   // layer -> promise/timer bookkeeping

  async function loadResults() {
    const res = await fetch(cfg.resultUrl);
    if (res.status === 202) { setTimeout(loadResults, 800); return; }
    result = await res.json();
    renderResults(result);
    $("#progress-view").hidden = true;
    $("#results-view").hidden = false;
    document.title = `${titleCase(result.company)} audit · Growth Audit`;
    window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  }

  const titleCase = (s) => (s || "").replace(/\b\w/g, (c) => c.toUpperCase());
  const LEVEL = { High: 3, Med: 2, Low: 1 };

  function renderResults(r) {
    const view = $("#results-view");
    const mins = Math.round(r.duration / 60);
    const failed = r.layers.filter((l) => l.status !== "done").map((l) => `L${l.index}`);
    let banner = "";
    if (r.status === "out_of_credits" || r.status === "partial") {
      banner = `<div class="alert alert-warn banner" role="status"><strong>Partial audit.</strong> ${esc(r.message)}${failed.length ? ` Incomplete: ${failed.join(", ")}.` : ""}</div>`;
    }

    view.innerHTML = `
      <header class="results-head">
        <div class="results-meta">
          <p class="eyebrow"><span class="source-tag source-ai">${ICON.spark}Growth audit</span>&nbsp; ${esc(r.date)}${mins ? ` · ${mins} min run` : ""} · ${r.sources} sources</p>
          <h1 class="results-title">${esc(titleCase(r.company))}</h1>
          <p class="run-meta">
            <span class="meta-item"><span class="meta-key">URL</span>${esc(r.url)}</span>
            <span class="meta-item"><span class="meta-key">ICP</span>${esc(r.icp)}</span>
            ${r.competitors.length ? `<span class="meta-item"><span class="meta-key">VS</span>${esc(r.competitors.join(", "))}</span>` : ""}
          </p>
          ${r.keywords.length ? `<div class="chips">${r.keywords.map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div>` : ""}
        </div>
        <div class="actions">
          <button class="btn" id="copy-btn" type="button">${ICON.copy}<span>Copy markdown</span></button>
          <button class="btn btn-primary" id="download-btn" type="button">${ICON.download}<span>Download .md</span></button>
          <a class="btn" href="/">${ICON.plus}<span>New audit</span></a>
        </div>
      </header>
      ${banner}
      ${renderPriorities(r)}
      ${renderTabs(r)}
      ${renderData(r)}
      ${renderAppendix(r)}
    `;
    wireResults(r);
  }

  function renderPriorities(r) {
    if (!r.priorities.length) {
      return r.unparsed ? `<section class="priority"><div class="md">${md(r.unparsed)}</div></section>` : "";
    }
    const cards = r.priorities.slice(0, 3).map((p, n) => `
      <article class="priority-card">
        <span class="priority-rank">${String(n + 1).padStart(2, "0")}</span>
        <h3 class="priority-title md">${mdInline(p.title)}</h3>
        <div class="scores">
          ${["impact", "confidence", "speed"].map((k) => `
            <div>
              <span class="score-label">${k}</span>
              <div class="meter" data-level="${LEVEL[p[k]] || 0}" aria-label="${k}: ${esc(p[k] || "n/a")}"><i></i><i></i><i></i></div>
              <span class="score-value">${esc(p[k] || "—")}</span>
            </div>`).join("")}
        </div>
        <div class="priority-body md clamped">${md(p.rationale)}</div>
        <button class="link-btn" type="button" data-expand>Read full rationale</button>
      </article>`).join("");
    return `
      <section class="priority" aria-labelledby="prio-title">
        <div class="section-head">
          <h2 class="section-title" id="prio-title">Priority stack</h2>
          <span class="section-sub">ranked by impact × confidence × speed</span>
        </div>
        <div class="priority-grid">${cards}</div>
      </section>`;
  }

  function renderObservation(o) {
    if (o.raw) return `<li class="obs"><div class="md">${md(o.raw)}</div></li>`;
    return `
      <li class="obs">
        <div class="obs-row observed"><span class="obs-key">Observed</span><div class="obs-val md">${mdInline(o.observed)}</div></div>
        <div class="obs-row why"><span class="obs-key">Why it matters</span><div class="obs-val md">${mdInline(o.why)}</div></div>
        <div class="obs-row hypothesis"><span class="obs-key">Hypothesis</span><div class="obs-val md">${mdInline(o.hypothesis)}</div></div>
      </li>`;
  }

  function renderTabs(r) {
    const tabs = r.layers.map((l, n) => `
      <button class="tab" role="tab" id="tab-${l.index}" aria-controls="panel-${l.index}" aria-selected="${n === 0}"
              tabindex="${n === 0 ? 0 : -1}" data-failed="${l.status !== "done"}">
        <span class="layer-code">L${l.index}</span>${esc(l.short)}
        <span class="tab-count">${l.observations.length || ""}</span>
        <span class="note-dot" ${l.analyst_notes.trim() ? "" : "hidden"}></span>
      </button>`).join("");

    const panels = r.layers.map((l, n) => `
      <div class="tabpanel" role="tabpanel" id="panel-${l.index}" aria-labelledby="tab-${l.index}" ${n === 0 ? "" : "hidden"}>
        <div class="ai-col">
          <span class="source-tag source-ai">${ICON.spark}AI research</span>
          <h2 class="tabpanel-title">${esc(l.title.replace(/^Layer \d+ - /, ""))}</h2>
          ${l.status !== "done" ? `<div class="alert alert-warn">This layer did not complete. ${esc(l.notes.slice(0, 300))}</div>` : ""}
          ${l.working ? `<div class="working"><span class="working-label">What's working</span><div class="md">${mdInline(l.working)}</div></div>` : ""}
          ${l.observations.length ? `<ol class="obs-list">${l.observations.map(renderObservation).join("")}</ol>` : ""}
          ${l.notes ? `
          <details class="raw">
            <summary>Full research notes <span class="mono-muted">${Math.round(l.notes.length / 1000)}k chars · sources cited inline</span></summary>
            <div class="md">${md(l.notes)}</div>
          </details>` : ""}
        </div>
        <aside class="analyst">
          <div class="analyst-head">
            <span class="source-tag source-analyst">${ICON.pen}Field notes · you</span>
            <span class="save-state" data-save-state="${l.index}">${l.analyst_notes.trim() ? "Saved" : ""}</span>
          </div>
          <p class="analyst-hint">Your own live observations for this layer. Kept separate from the AI research and exported as "Analyst Field Notes".</p>
          <label class="visually-hidden" for="notes-${l.index}">Field notes for layer ${l.index}</label>
          <textarea id="notes-${l.index}" data-layer="${l.index}" placeholder="e.g. Tried the savings calculator on mobile — it took 3 taps to find and didn't pre-fill my volume…">${esc(l.analyst_notes)}</textarea>
        </aside>
      </div>`).join("");

    return `
      <section class="tabs-wrap" aria-labelledby="layers-title">
        <div class="section-head">
          <h2 class="section-title" id="layers-title">Channel by channel</h2>
          <span class="section-sub">observed → why it matters → hypothesis to test</span>
        </div>
        <div class="tablist" role="tablist" aria-label="Audit layers">${tabs}</div>
        ${panels}
      </section>`;
  }

  function renderData(r) {
    if (!r.data.length) return "";
    return `
      <section class="data" aria-labelledby="data-title">
        <div class="section-head">
          <h2 class="section-title" id="data-title">Data I'd want before committing</h2>
          <span class="section-sub">analytics to confirm or kill each bet</span>
        </div>
        <div class="data-grid">
          ${r.data.map((d, n) => `<div class="data-card md"><span class="data-num">Q${n + 1}</span>${md(d)}</div>`).join("")}
        </div>
      </section>`;
  }

  function renderAppendix(r) {
    if (!r.fetch_log.length) return "";
    return `
      <section class="appendix">
        <details class="raw">
          <summary>Page fetch log <span class="mono-muted">${r.fetch_log.length} pages${r.report_path ? " · saved to " + esc(r.report_path) : ""}</span></summary>
          <ul class="fetch-log">${r.fetch_log.map((line) => `<li class="${/FAILED/.test(line) ? "failed" : ""}">${esc(line)}</li>`).join("")}</ul>
        </details>
      </section>`;
  }

  function wireResults(r) {
    // Tabs: click + arrow-key navigation.
    const tabs = $$(".tab");
    const select = (tab) => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", on);
        t.tabIndex = on ? 0 : -1;
        $("#" + t.getAttribute("aria-controls")).hidden = !on;
      });
    };
    tabs.forEach((tab, n) => {
      tab.addEventListener("click", () => select(tab));
      tab.addEventListener("keydown", (e) => {
        const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (!d) return;
        const next = tabs[(n + d + tabs.length) % tabs.length];
        select(next);
        next.focus();
      });
    });

    // Expand priority rationale.
    $$("[data-expand]").forEach((btn) => btn.addEventListener("click", () => {
      const body = btn.previousElementSibling;
      const clamped = body.classList.toggle("clamped");
      btn.textContent = clamped ? "Read full rationale" : "Show less";
    }));

    // Analyst notes: autosave while typing, and immediately on blur.
    $$(".analyst textarea").forEach((ta) => {
      const layer = ta.dataset.layer;
      ta.addEventListener("input", () => {
        setSaveState(layer, "saving", "Editing…");
        clearTimeout(pendingSaves.get(layer));
        pendingSaves.set(layer, setTimeout(() => saveNotes(layer), 700));
        $(`#tab-${layer} .note-dot`).hidden = !ta.value.trim();
      });
      ta.addEventListener("blur", () => { if (pendingSaves.has(layer)) saveNotes(layer); });
    });

    $("#copy-btn").addEventListener("click", copyMarkdown);
    $("#download-btn").addEventListener("click", async () => {
      await flushSaves();
      window.location.href = cfg.reportUrl;
    });
  }

  function setSaveState(layer, cls, text) {
    const el = $(`[data-save-state="${layer}"]`);
    el.className = "save-state " + cls;
    el.textContent = text;
  }

  async function saveNotes(layer) {
    clearTimeout(pendingSaves.get(layer));
    pendingSaves.delete(layer);
    const text = $(`#notes-${layer}`).value;
    setSaveState(layer, "saving", "Saving…");
    try {
      const res = await fetch(cfg.notesUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ layer: Number(layer), text }),
      });
      if (res.status === 401) { setSaveState(layer, "error", "Signed out — reload to sign in, then re-save"); return; }
      if (!res.ok) throw new Error(res.status);
      const data = await res.json();
      setSaveState(layer, "saved", `Saved · ${data.at}`);
    } catch (err) {
      setSaveState(layer, "error", "Not saved — retrying on next edit");
    }
  }

  function flushSaves() {
    return Promise.all(Array.from(pendingSaves.keys()).map(saveNotes));
  }

  async function copyMarkdown() {
    const btn = $("#copy-btn");
    await flushSaves();
    const text = await (await fetch(cfg.reportUrl + "?inline=1")).text();
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      // Clipboard API needs a secure context; fall back for plain-http LAN use.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    btn.classList.add("is-done");
    btn.innerHTML = `${ICON.check}<span>Copied</span>`;
    toast(`Full report copied · ${Math.round(text.length / 1000)}k characters of markdown`);
    setTimeout(() => { btn.classList.remove("is-done"); btn.innerHTML = `${ICON.copy}<span>Copy markdown</span>`; }, 2000);
  }

  // ------------------------------------------------------------------ boot
  function boot() {
    if (cfg.status === "running") startStream();
    else loadResults();
  }
  // marked / DOMPurify are deferred; wait for them so the first render is formatted.
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
