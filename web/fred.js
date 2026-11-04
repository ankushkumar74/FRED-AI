// Boots FRED inside the browser via Pyodide and wires it to the page.
//
// Pyodide ships duckdb, pydantic, pyarrow and pyyaml as WASM builds, and sqlglot
// is pure Python — so the entire Phase 1 stack runs here unmodified. The page
// calls the same services.ask.Ask the CLI and the test suite call. That is the
// point: a demo running a reimplementation would prove nothing about the code.

// 314.x is Pyodide's CPython-aligned versioning. The older 0.28 line does not
// ship duckdb or pyarrow, which would sink this entire approach — so the version
// here is load-bearing, not incidental.
const PYODIDE_VERSION = "314.0.7";
const PYODIDE_BASE = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

// Resolve asset URLs against this module, so the page works at any path depth.
const asset = (name) => new URL(name, import.meta.url).href;

const el = (id) => document.getElementById(id);
const dot = el("dot");
const statusText = el("statusText");
const results = el("results");

// Edge cases are listed deliberately. The interesting behaviour is not the happy
// path — it is refusing a query that would double-count, and asking instead of
// guessing when a question is ambiguous.
const SUGGESTIONS = [
  { q: "revenue by channel last month" },
  { q: "weekly revenue over the last 8 weeks" },
  { q: "CAC by channel last month" },
  { q: "average order value by region" },
  { q: "new customers last month" },
  { q: "return on ad spend by campaign" },
  { q: "revenue by product category", edge: "refuses: fan-out join" },
  { q: "CAC by device", edge: "refuses: dimension not shared" },
  { q: "what is our conversion rate", edge: "undefined metric" },
  { q: "how much did we make in July", edge: "ambiguous" },
  { q: "why did revenue drop last week", edge: "states its limit" },
];

function setStatus(text, state) {
  statusText.textContent = text;
  dot.className = "dot" + (state ? ` ${state}` : "");
}

const fmt = (value, kind) => {
  if (value === null || value === undefined) return "n/a";
  const n = Number(value);
  if (Number.isNaN(n)) return String(value);
  if (kind === "currency")
    return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (kind === "percent")
    return (n * 100).toLocaleString(undefined, { maximumFractionDigits: 2 }) + "%";
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
};

const label = (v) => {
  const s = String(v ?? "");
  // DuckDB hands back ISO timestamps for truncated periods; the time is noise.
  return /^\d{4}-\d{2}-\d{2}T/.test(s) ? s.slice(0, 10) : s;
};

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function renderAnswer(a) {
  const card = document.createElement("div");
  card.className = `answer ${a.status}`;
  let html = `<div class="q">You asked <b>${esc(a.question)}</b></div>`;

  if (a.status === "clarify") {
    html += `<div class="msg">${esc(a.message)}</div>`;
    if (a.candidates?.length) {
      html += `<div class="cands">` + a.candidates
        .map((c) => `${esc(c.key)} · ${c.score.toFixed(2)}`).join("<br>") + `</div>`;
    }
    card.innerHTML = html;
    return card;
  }

  if (a.status === "refused" || a.status === "error") {
    const title = a.status === "refused"
      ? "I can't answer that correctly"
      : "Something went wrong";
    html += `<div class="scope">${title}</div><div class="msg">${esc(a.message)}</div>`;
    if (a.status === "refused") {
      html += `<div class="note">Refusing is the feature. Producing a number here would
        mean producing a wrong one, and a wrong number that looks confident is worse
        than no answer.</div>`;
    }
    card.innerHTML = html;
    return card;
  }

  const badge = a.certified
    ? `<span class="badge cert">certified</span>`
    : `<span class="badge">uncertified</span>`;
  html += `<div class="scope">${esc(a.scope)}${badge}</div>`;

  const labelCols = a.columns.filter((c) => c !== "value" && c !== "period_type");
  if (a.rows.length === 1 && labelCols.length === 0) {
    html += `<div class="big">${fmt(a.rows[0].value, a.metric_format)}</div>`;
  } else if (a.rows.length) {
    const vals = a.rows.map((r) => Number(r.value) || 0);
    const peak = Math.max(...vals.map(Math.abs), 1);
    html += `<table><thead><tr>` +
      labelCols.map((c) => `<th>${esc(c)}</th>`).join("") +
      `<th style="text-align:right">${esc(a.metric_label ?? "value")}</th></tr></thead><tbody>`;
    for (const row of a.rows.slice(0, 40)) {
      const w = (Math.abs(Number(row.value) || 0) / peak) * 100;
      html += `<tr>` + labelCols.map((c) => `<td>${esc(label(row[c]))}</td>`).join("") +
        `<td class="num">${fmt(row.value, a.metric_format)}` +
        `<span class="bar" style="width:${w.toFixed(1)}%"></span></td></tr>`;
    }
    html += `</tbody></table>`;
    if (a.rows.length > 40) html += `<div class="meta">… ${a.rows.length - 40} more rows</div>`;
  } else {
    html += `<div class="msg">No data in that window.</div>`;
  }

  if (a.limitation) html += `<div class="note"><b>Note.</b> ${esc(a.limitation)}</div>`;
  for (const n of a.notes ?? []) html += `<div class="note">${esc(n)}</div>`;

  html += `<div class="meta">${a.duration_ms}ms${a.from_cache ? " · cached" : ""} · ` +
    `${a.rows.length} rows · ${esc((a.tables ?? []).join(", "))}</div>`;

  if (a.sql) {
    html += `<details><summary>Show the SQL FRED wrote</summary><pre>${esc(a.sql)}</pre></details>`;
  }
  card.innerHTML = html;
  return card;
}

async function main() {
  const t0 = performance.now();
  setStatus("Loading Python runtime (~10s, first visit only)…", "busy");

  const { loadPyodide } = await import(PYODIDE_BASE + "pyodide.mjs");
  const pyodide = await loadPyodide({ indexURL: PYODIDE_BASE });

  setStatus("Loading DuckDB, pydantic and Arrow…", "busy");
  await pyodide.loadPackage(["duckdb", "pydantic", "pyarrow", "pyyaml", "micropip"]);

  setStatus("Installing the semantic layer…", "busy");
  const micropip = pyodide.pyimport("micropip");
  await micropip.install("sqlglot");
  // The wheel filename carries the version and cannot be renamed freely, so the
  // build writes it into manifest.json rather than this file guessing.
  const manifest = await (await fetch(asset("manifest.json"))).json();
  await micropip.install(asset(manifest.wheel));

  setStatus("Loading the dataset (2.6 MB)…", "busy");
  pyodide.FS.mkdirTree("/fred");
  for (const name of ["demo.duckdb", "ecommerce.yaml", "bridge.py"]) {
    const res = await fetch(asset(name));
    if (!res.ok) throw new Error(`could not fetch ${name}: HTTP ${res.status}`);
    const buf = new Uint8Array(await res.arrayBuffer());
    pyodide.FS.writeFile(`/fred/${name}`, buf);
  }

  setStatus("Starting FRED…", "busy");
  pyodide.runPython(`import sys; sys.path.insert(0, "/fred")`);
  const bridge = pyodide.pyimport("bridge");
  const boot = JSON.parse(bridge.boot());
  if (!boot.ok) throw new Error(boot.error);

  const total = ((performance.now() - t0) / 1000).toFixed(1);
  setStatus(
    `Ready — ${boot.metrics} metrics over ${boot.tables.length} tables, running locally. ` +
    `Booted in ${total}s.`,
    "ready"
  );

  el("q").disabled = false;
  el("go").disabled = false;
  el("q").focus();

  const chips = el("chips");
  for (const s of SUGGESTIONS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip" + (s.edge ? " edge" : "");
    b.textContent = s.edge ? `${s.q} — ${s.edge}` : s.q;
    b.onclick = () => { el("q").value = s.q; el("form").requestSubmit(); };
    chips.appendChild(b);
  }

  const run = (question) => {
    const answer = JSON.parse(bridge.ask(question));
    results.prepend(renderAnswer(answer));
  };

  el("form").addEventListener("submit", (e) => {
    e.preventDefault();
    const question = el("q").value.trim();
    if (!question) return;
    el("go").disabled = true;
    setStatus("Resolving metric, compiling SQL, querying…", "busy");
    // Yield a frame so the busy state paints before Python blocks the thread.
    setTimeout(() => {
      try {
        run(question);
      } catch (err) {
        results.prepend(renderAnswer({
          question, status: "error", message: String(err), columns: [], rows: [],
        }));
      } finally {
        el("go").disabled = false;
        setStatus(`Ready — ${boot.metrics} metrics over ${boot.tables.length} tables, running locally.`, "ready");
      }
    }, 20);
  });

  run("revenue by channel last month");
}

main().catch((err) => {
  console.error(err);
  setStatus(`Failed to start: ${err.message}`, "failed");
});
