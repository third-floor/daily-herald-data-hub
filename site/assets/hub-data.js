/*
 * hub-data.js — loads the Daily Herald Data Hub's published data files.
 *
 * Every widget in /tools includes this script and calls, for example:
 *
 *     HubData.autoload("visual_table", rows => myIngestFunction(rows));
 *
 * which fetches every chunk of data/widgets/visual_table/ listed in
 * data/manifest.json (in parallel), shows a small progress banner, and hands
 * the combined array to the widget. If the hub data can't be reached (e.g.
 * the page was opened straight from disk), the banner says so and the
 * widget's own "upload a file" option keeps working as before.
 */
(function () {
  "use strict";

  const script = document.currentScript;
  const DATA_BASE = new URL("../data/", script ? script.src : location.href);
  const HUB_HOME = new URL("../index.html", script ? script.src : location.href);
  let manifestPromise = null;

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

  // ── small UI helpers ──────────────────────────────────────────────────
  const css = `
  .hub-banner{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:99999;
    max-width:min(92vw,560px);padding:10px 14px;border-radius:10px;font:13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
    background:#0d1a2b;color:#d9ebff;border:1px solid #294661;box-shadow:0 10px 40px rgba(0,0,0,.35);transition:opacity .4s}
  .hub-banner.error{border-color:#a8505b}
  .hub-banner .bar{height:3px;margin-top:7px;background:#1c3550;border-radius:2px;overflow:hidden}
  .hub-banner .bar i{display:block;height:100%;width:0;background:#79c9ff;transition:width .2s}
  .hub-home{position:fixed;left:12px;bottom:12px;z-index:99998;padding:6px 10px;border-radius:999px;
    font:12px system-ui,-apple-system,"Segoe UI",sans-serif;text-decoration:none;
    background:rgba(13,26,43,.92);color:#79c9ff;border:1px solid #294661}
  .hub-home:hover{border-color:#79c9ff}
  @media print{.hub-banner,.hub-home{display:none}}`;

  function injectCss() {
    if (document.getElementById("hub-data-css")) return;
    const s = document.createElement("style");
    s.id = "hub-data-css";
    s.textContent = css;
    document.head.appendChild(s);
  }

  function banner() {
    injectCss();
    const el = document.createElement("div");
    el.className = "hub-banner";
    el.setAttribute("role", "status");
    el.innerHTML = `<div class="msg"></div><div class="bar"><i></i></div>`;
    document.body.appendChild(el);
    return {
      set(msg, frac) {
        el.querySelector(".msg").textContent = msg;
        if (frac != null) el.querySelector(".bar i").style.width = `${Math.round(frac * 100)}%`;
      },
      error(msg) {
        el.classList.add("error");
        el.querySelector(".bar").remove();
        el.querySelector(".msg").textContent = msg;
        setTimeout(() => this.close(), 9000);
      },
      close(delay = 0) {
        setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 450); }, delay);
      },
    };
  }

  function homeLink() {
    if (document.querySelector(".hub-home")) return;
    injectCss();
    const a = document.createElement("a");
    a.className = "hub-home";
    a.href = HUB_HOME.href;
    a.textContent = "← Data Hub";
    document.body.appendChild(a);
  }

  const mb = b => (b / 1e6).toFixed(1);

  /**
   * Load a view with a progress banner, then call ingest(rows, info).
   * `extra` lists further views to load alongside (passed in info.extra).
   */
  async function autoload(view, ingest, { label, extra = [], optionalExtra = [] } = {}) {
    const start = () => run().catch(err => console.error("[hub-data]", err));
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();

    async function run() {
      homeLink();
      if (location.protocol === "file:") return;   // opened from disk: keep the upload flow
      const b = banner();
      try {
        const m = await manifest();
        const views = [view, ...extra];
        const sizes = views.map(v => (filesFor(m, v) || []).reduce((n, f) => n + f.bytes, 0));
        const grand = sizes.reduce((a, c) => a + c, 0) || 1;
        const got = new Array(views.length).fill(0);
        const progress = i => p => {
          got[i] = p.bytes;
          const sum = got.reduce((a, c) => a + c, 0);
          b.set(`Loading ${label || "data"} from the Data Hub… ${mb(sum)} / ${mb(grand)} MB`, sum / grand);
        };
        b.set(`Loading ${label || "data"} from the Data Hub…`, 0);
        const [rows, ...others] = await Promise.all(views.map((v, i) => loadView(v, { onProgress: progress(i) })));
        const opt = await Promise.all(optionalExtra.map(v => loadView(v, { optional: true })));
        const info = { manifest: m, generatedAt: m.generated_at, extra: {} };
        extra.forEach((v, i) => { info.extra[v] = others[i]; });
        optionalExtra.forEach((v, i) => { info.extra[v] = opt[i]; });
        b.set(`Preparing ${rows.length.toLocaleString("en-GB")} records…`, 1);
        await new Promise(r => setTimeout(r, 30));   // let the banner paint
        await ingest(rows, info);
        b.set(`Loaded ${rows.length.toLocaleString("en-GB")} records from the Data Hub (built ${new Date(m.generated_at).toLocaleDateString("en-GB")}).`, 1);
        b.close(2500);
      } catch (err) {
        console.error("[hub-data]", err);
        b.error(`Couldn't load the hub data (${err.message}). You can still load a file manually.`);
      }
    }
  }

  window.HubData = { manifest, loadView, autoload, homeLink, dataBase: DATA_BASE.href };
})();
