/*
 * hub-data.js — loads the Daily Herald Data Hub's published data files.
 *
 * Every widget in /tools includes this script in its <head> and calls, e.g.
 *
 *     HubData.autoload("visual_table", rows => myIngestFunction(rows), { label: "visual elements" });
 *
 * While the data loads, a full-screen loading screen covers the page (so the
 * widget's own "drop a file here" screen is never shown). When the data has
 * arrived it is handed to the widget's existing loading code and the screen
 * fades away. If the hub data can't be reached (or the page was opened
 * straight from disk), the loading screen is removed and the widget's own
 * upload option is shown instead, with a short explanation.
 */
(function () {
  "use strict";

  const script = document.currentScript;
  const DATA_BASE = new URL("../data/", script ? script.src : location.href);
  const HUB_HOME = new URL("../index.html", script ? script.src : location.href);
  const ONLINE = location.protocol !== "file:";
  let manifestPromise = null;

  // ── data ──────────────────────────────────────────────────────────────
  function manifest() {
    if (!manifestPromise) {
      manifestPromise = fetch(new URL("manifest.json", DATA_BASE), { cache: "no-cache" })
        .then(r => { if (!r.ok) throw new Error(`manifest.json: HTTP ${r.status}`); return r.json(); })
        .catch(e => { manifestPromise = null; throw e; });
    }
    return manifestPromise;
  }

  function filesFor(m, view) {
    return m.files[view] || m.files[`widgets/${view}`] || m.files[`lookups/${view}`] || null;
  }

  async function fetchJson(path) {
    const r = await fetch(new URL(path.replace(/^data\//, ""), DATA_BASE));
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  }

  /** Load every chunk of a view and return one array (in chunk order). */
  async function loadView(view, { onProgress, concurrency = 4, optional = false } = {}) {
    const m = await manifest();
    const files = filesFor(m, view);
    if (!files) {
      if (optional) return [];
      throw new Error(`No "${view}" data in this hub build.`);
    }
    const total = files.reduce((n, f) => n + (f.bytes || 0), 0);
    const results = new Array(files.length);
    let done = 0, doneBytes = 0, next = 0;
    async function worker() {
      while (next < files.length) {
        const i = next++;
        results[i] = await fetchJson(files[i].path);
        done++; doneBytes += files[i].bytes || 0;
        if (onProgress) onProgress({ done, count: files.length, bytes: doneBytes, total });
      }
    }
    await Promise.all(Array.from({ length: Math.min(concurrency, files.length) }, worker));
    return results.flat();
  }

  // ── styles ────────────────────────────────────────────────────────────
  const FONT = `system-ui,-apple-system,"Segoe UI",Roboto,sans-serif`;
  const css = `
  html.hub-loading body{visibility:hidden}
  html.hub-loading:not(.hub-overlay-on)::after{content:"Loading the Daily Herald Data Hub…";position:fixed;inset:0;z-index:2147483646;
    display:grid;place-items:center;background:#08111f;color:#83a3c2;font:14px ${FONT}}
  .hub-overlay{visibility:visible;position:fixed;inset:0;z-index:2147483647;display:grid;place-items:center;padding:24px;
    background:radial-gradient(circle at 20% 0%,rgba(41,121,255,.14),transparent 34rem),#08111f;color:#d9ebff;
    font:15px/1.5 ${FONT};transition:opacity .35s ease}
  .hub-overlay.out{opacity:0;pointer-events:none}
  .hub-overlay .card{width:min(440px,100%);text-align:left}
  .hub-overlay .eyebrow{font:11px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.14em;text-transform:uppercase;color:#83a3c2}
  .hub-overlay h1{margin:8px 0 18px;font-size:26px;line-height:1.15;letter-spacing:-.01em;font-weight:700;color:#d9ebff}
  .hub-overlay .msg{color:#83a3c2;font-size:14px;min-height:1.5em}
  .hub-overlay .bar{height:4px;margin:12px 0 8px;background:#1c3550;border-radius:3px;overflow:hidden}
  .hub-overlay .bar i{display:block;height:100%;width:3%;background:#79c9ff;border-radius:3px;transition:width .25s ease}
  .hub-overlay .bar.indeterminate i{width:30%;animation:hub-slide 1.1s ease-in-out infinite}
  .hub-overlay .sub{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#587895;min-height:1.4em}
  @keyframes hub-slide{0%{transform:translateX(-110%)}100%{transform:translateX(360%)}}
  @media (prefers-reduced-motion:reduce){.hub-overlay .bar.indeterminate i{animation:none;width:100%;opacity:.5}}
  .hub-banner{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:99999;
    max-width:min(92vw,560px);padding:10px 14px;border-radius:10px;font:13px/1.45 ${FONT};
    background:#0d1a2b;color:#d9ebff;border:1px solid #a8505b;box-shadow:0 10px 40px rgba(0,0,0,.35);transition:opacity .4s}
  .hub-home{position:fixed;left:12px;bottom:12px;z-index:99998;padding:6px 10px;border-radius:999px;
    font:12px ${FONT};text-decoration:none;background:rgba(13,26,43,.92);color:#79c9ff;border:1px solid #294661}
  .hub-home:hover{border-color:#79c9ff}
  @media print{.hub-banner,.hub-home,.hub-overlay{display:none}}`;

  function injectCss() {
    if (document.getElementById("hub-data-css")) return;
    const s = document.createElement("style");
    s.id = "hub-data-css";
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  }

  // Hide the widget's own landing page from the very first paint.
  injectCss();
  if (ONLINE) document.documentElement.classList.add("hub-loading");
  let loadStarted = false;
  const reveal = () => document.documentElement.classList.remove("hub-loading", "hub-overlay-on");
  // Safety net: never leave a page hidden if no loader was started.
  document.addEventListener("DOMContentLoaded", () => setTimeout(() => { if (!loadStarted) reveal(); }, 1500));

  function onReady(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  // ── UI pieces ─────────────────────────────────────────────────────────
  function overlay(title) {
    const el = document.createElement("div");
    el.className = "hub-overlay";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.innerHTML = `<div class="card"><div class="eyebrow">Daily Herald Data Hub</div>
      <h1></h1><div class="msg"></div><div class="bar indeterminate"><i></i></div><div class="sub"></div></div>`;
    el.querySelector("h1").textContent = title;
    document.body.appendChild(el);
    document.documentElement.classList.add("hub-overlay-on");
    return {
      set(msg, frac, sub) {
        el.querySelector(".msg").textContent = msg;
        const bar = el.querySelector(".bar");
        if (frac != null) { bar.classList.remove("indeterminate"); bar.querySelector("i").style.width = `${Math.max(3, Math.round(frac * 100))}%`; }
        else bar.classList.add("indeterminate");
        el.querySelector(".sub").textContent = sub || "";
      },
      close() {
        reveal();
        el.classList.add("out");
        setTimeout(() => el.remove(), 400);
      },
    };
  }

  function errorBanner(msg) {
    const el = document.createElement("div");
    el.className = "hub-banner";
    el.setAttribute("role", "alert");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 450); }, 10000);
  }

  function homeLink() {
    onReady(() => {
      if (document.querySelector(".hub-home")) return;
      const a = document.createElement("a");
      a.className = "hub-home";
      a.href = HUB_HOME.href;
      a.textContent = "← Data Hub";
      document.body.appendChild(a);
    });
  }

  const mb = b => (b / 1e6).toFixed(1);
  const pageTitle = () => (document.title || "Loading").replace(/\s*[—|·-]\s*v?\d.*$/, "").replace(/\s+v\d+(\.\d+)*$/i, "");

  /**
   * Show the loading screen, load `view` (+ `extra` / `optionalExtra` views),
   * then call ingest(rows, info). Resolves to true on success, false if the
   * data could not be loaded (the page is then revealed with its own upload
   * option; pass `onFail` to try something else first, `quietFail` to skip
   * the error message).
   */
  async function load(view, ingest, { label, extra = [], optionalExtra = [], onFail, quietFail = false } = {}) {
    loadStarted = true;
    homeLink();
    if (!ONLINE) { reveal(); if (onFail) await onFail(); return false; }
    await new Promise(onReady);
    const ov = overlay(pageTitle());
    try {
      ov.set(`Loading ${label || "data"}…`, null);
      const m = await manifest();
      const views = [view, ...extra];
      const grand = views.reduce((n, v) => n + (filesFor(m, v) || []).reduce((a, f) => a + f.bytes, 0), 0) || 1;
      const got = new Array(views.length).fill(0);
      const progress = i => p => {
        got[i] = p.bytes;
        const sum = got.reduce((a, c) => a + c, 0);
        ov.set(`Loading ${label || "data"}…`, sum / grand * 0.9, `${mb(sum)} of ${mb(grand)} MB`);
      };
      const [rows, ...others] = await Promise.all(views.map((v, i) => loadView(v, { onProgress: progress(i) })));
      const opt = await Promise.all(optionalExtra.map(v => loadView(v, { optional: true })));
      const info = { manifest: m, generatedAt: m.generated_at, extra: {} };
      extra.forEach((v, i) => { info.extra[v] = others[i]; });
      optionalExtra.forEach((v, i) => { info.extra[v] = opt[i]; });
      ov.set(`Preparing ${rows.length.toLocaleString("en-GB")} records…`, 0.95, "This can take a few seconds for the larger tools.");
      await new Promise(r => requestAnimationFrame(() => setTimeout(r, 30)));   // let it paint
      await ingest(rows, info);
      ov.set(`Ready`, 1, "");
      ov.close();
      return true;
    } catch (err) {
      console.error("[hub-data]", err);
      ov.close();
      if (onFail) { try { await onFail(err); } catch (e) { console.error(e); } }
      if (!quietFail) errorBanner(`Couldn't load the Data Hub data (${err.message}). You can load a file yourself instead.`);
      return false;
    }
  }

  /** Same as load(), started automatically once the page is ready. */
  function autoload(view, ingest, options) {
    loadStarted = true;
    onReady(() => { load(view, ingest, options); });
  }

  window.HubData = { manifest, loadView, load, autoload, homeLink, dataBase: DATA_BASE.href };
})();
