
const $ = (id) => document.getElementById(id);
let state = { tournaments: [], current: null, modules: [], dirty: false };

function flash(msg, ok = true) {
  const f = $("flash");
  f.textContent = msg;
  f.className = "flash " + (ok ? "ok" : "err") + " show";
  clearTimeout(f._t);
  f._t = setTimeout(() => f.className = "flash", 2600);
}

async function api(method, url, body, publishToken) {
  const opts = { method, headers: {} };
  const token = sessionStorage.getItem("admin_token");
  if (token) opts.headers["Authorization"] = "Bearer " + token;
  if (publishToken) opts.headers["X-Publish-Token"] = publishToken;
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (r.status === 401) { promptToken(); throw new Error("unauthorized — enter the admin token"); }
  if (r.status === 409) { flash(data.error || "conflict — module changed elsewhere, reload", false); throw new Error("conflict"); }
  if (!r.ok && !data.error) throw new Error("HTTP " + r.status);
  return data;
}

function promptToken() {
  const t = prompt("Enter admin token (from the server's .admin-token file):");
  if (t) { sessionStorage.setItem("admin_token", t.trim()); location.reload(); }
}

function ensureToken() {
  if (!sessionStorage.getItem("admin_token")) promptToken();
}

function clearToken() {
  sessionStorage.removeItem("admin_token");
  sessionStorage.removeItem("publish_token");
  flash("Logged out — tokens cleared");
  showList();
}

function promptPublishToken() {
  const t = prompt("Enter PUBLISH token (from the server's .publish-token file):");
  if (t) sessionStorage.setItem("publish_token", t.trim());
  return t;
}

function showList() {
  $("view-list").classList.remove("hidden");
  $("view-new").classList.add("hidden");
  $("view-edit").classList.add("hidden");
  loadList();
}
function showNew() {
  $("view-list").classList.add("hidden");
  $("view-new").classList.remove("hidden");
  $("view-edit").classList.add("hidden");
}
async function showEdit(org, slug) {
  $("view-list").classList.add("hidden");
  $("view-new").classList.add("hidden");
  $("view-edit").classList.remove("hidden");
  $("e-report-card").classList.add("hidden");
  const data = await api("GET", `/api/tournament/${org}/${slug}`);
  state.current = data;
  state.modules = data.modules || [];
  $("e-title").textContent = `${data.manifest.name || org + "/" + slug}`;
  renderStatus();
  renderTabs();
  selectModule(state.modules[0]);
}

window.addEventListener("beforeunload", (e) => {
  if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
});

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function badge(status) {
  const map = { live: "b-live", draft: "b-draft", in_review: "b-in_review",
                approved: "b-approved", published: "b-published" };
  return `<span class="badge ${map[status] || "b-unknown"}">${esc(status || "?")}</span>`;
}

function renderStatus() {
  const m = state.current.manifest;
  const rev = m.revision || {};
  const el = $("e-statusbar");
  el.className = "statusbar st-ok";
  el.innerHTML = "";
  const add = (label, value) => {
    const span = document.createElement("span");
    span.className = "kv";
    const b = document.createElement("b");
    b.textContent = label;
    const v = document.createElement("span");
    v.textContent = value;
    span.append(b, v);
    el.appendChild(span);
  };
  add("status", m.status || "?");
  add("revision", rev.workflow || "none");
  add("digest", (state.current.digest || "").slice(0, 10));
  add("modules", String(state.modules.length));
  if (rev.reviewer) add("reviewer", rev.reviewer);
  if (rev.approvedAt) add("approved", rev.approvedAt);
  if (rev.publishedAt) add("published", rev.publishedAt);
}

function renderTabs() {
  const t = $("e-tabs");
  t.innerHTML = "";
  state.modules.forEach((m) => {
    const b = document.createElement("button");
    b.className = "tab";
    b.textContent = m.replace(".json", "");
    b.onclick = () => selectModule(m);
    t.appendChild(b);
  });
}

async function selectModule(name) {
  if (state.dirty && !confirm("Unsaved changes — discard?")) return;
  state.dirty = false;
  $("e-saved").classList.add("hidden");
  $("e-module-desc").textContent = `Module: ${name}`;
  const r = await api("GET", `/api/tournament/${state.current.org}/${state.current.slug}`);
  // Edit the RAW module file (source of truth) — not the compiled bundle.
  // manifest.json is edited via the manifest object.
  let content = (r.moduleFiles && r.moduleFiles[name]) || "";
  if (name === "manifest.json" && !content) content = JSON.stringify(r.manifest, null, 2);
  $("e-editor").value = content || "{}";
  $("e-editor").oninput = () => { state.dirty = true; $("e-saved").classList.add("hidden"); };
  [...$("e-tabs").children].forEach(b => b.classList.toggle("active", b.textContent === name.replace(".json", "")));
}

async function saveModule() {
  const name = currentModuleName();
  let content = $("e-editor").value;
  try { JSON.parse(content); } catch (e) {
    flash("Invalid JSON: " + e.message, false); return;
  }
  const baseDigest = (state.current.moduleDigests || {})[name] || null;
  const d = await api("PUT", `/api/tournament/${state.current.org}/${state.current.slug}/module/${name}`,
            { content, baseDigest });
  state.dirty = false;
  state.current.moduleDigests = state.current.moduleDigests || {};
  state.current.moduleDigests[name] = d.digest;
  $("e-saved").classList.remove("hidden");
  flash("Saved " + name);
}

function currentModuleName() {
  const act = $("e-tabs").querySelector(".tab.active");
  return (act ? act.textContent : state.modules[0]).replace(/\s+/g, "-") + ".json";
}

async function runAutofill() {
  const mod = currentModuleName();
  const org = state.current.org, slug = state.current.slug;
  let body;
  if (mod === "weather.json") {
    // venue coordinates come from the venue module
    const v = await api("GET", `/api/tournament/${org}/${slug}`);
    const venue = (v.bundle && v.bundle.venue) || {};
    const coords = venue.coordinates || {};
    if (!coords.lat || !coords.lng) { flash("venue.json has no coordinates — add lat/lng first", false); return; }
    body = { lat: coords.lat, lng: coords.lng };
  } else if (mod === "rules.json") {
    const url = prompt("Rules page URL (e.g. Google Docs):");
    if (!url) return;
    body = { url };
  } else if (mod === "schedule.json") {
    const raw = prompt("Paste games JSON: {\"games\":[{date,time,opponent,...}],\"scheduleStatus\":\"partial\"}");
    if (!raw) return;
    try { body = { data: JSON.parse(raw) }; } catch (e) { flash("Invalid JSON: " + e.message, false); return; }
  } else if (mod === "hotels.json") {
    const raw = prompt("Paste research JSON: {\"stayToPlay\":true,\"official\":[{name,drive,...}]}");
    if (!raw) return;
    try { body = { data: JSON.parse(raw) }; } catch (e) { flash("Invalid JSON: " + e.message, false); return; }
  } else {
    flash("Autofill supports: weather, rules, schedule, hotels", false); return;
  }
  const d = await api("POST", `/api/tournament/${org}/${slug}/autofill/${mod}`, body);
  if (d.error) { flash(d.error, false); return; }
  flash(d.message + " — draft only, approve before publish");
  await showEdit(org, slug);
}

async function runValidate() {
  if (state.dirty) { flash("Save the module first", false); return; }
  const d = await api("POST", `/api/tournament/${state.current.org}/${state.current.slug}/validate`);
  renderReport(d);
  flash(`Validation: ${d.summary.blocking} blocking, ${d.summary.warnings} warnings`, d.summary.blocking === 0);
}

async function runPreview() {
  const d = await api("POST", `/api/tournament/${state.current.org}/${state.current.slug}/preview`);
  renderReport({ message: d.message, status: d.status, summary: { blocking: 0, warnings: 0 } });
  flash(d.message, d.exit_code === 0);
}

async function runApprove() {
  if (state.dirty) { flash("Save the module first", false); return; }
  const d = await api("POST", `/api/tournament/${state.current.org}/${state.current.slug}/approve`);
  if (d.status === "error") { flash(d.message, false); return; }
  flash(`Approved digest ${(d.digest || "").slice(0, 10)}`);
  await showEdit(state.current.org, state.current.slug);
}

async function runPublish() {
  if (state.dirty) { flash("Save the module first", false); return; }
  let pt = sessionStorage.getItem("publish_token");
  if (!pt) pt = promptPublishToken();
  if (!pt) return;
  if (!confirm("Publish to parents? This updates the live app data.\n\n" +
               "Approved digest: " + ((state.current.manifest.revision || {}).digest || "—").slice(0,10) + "\n" +
               "Current digest:  " + (state.current.digest || "—").slice(0,10))) return;
  const d = await api("POST", `/api/tournament/${state.current.org}/${state.current.slug}/publish`,
                      { no_links: true }, pt);
  if (d.status === "error") { flash(d.message, false); renderReport({ error: d.message }); return; }
  flash(d.message, d.exit_code === 0);
  renderReport({ status: d.status, message: d.message });
  if (d.exit_code === 0) await showEdit(state.current.org, state.current.slug);
}

function renderReport(data) {
  const card = $("e-report-card");
  card.classList.remove("hidden");
  const lines = [];
  if (data.results) {
    data.results.forEach(r => lines.push(`${r.severity === "ok" ? "✓" : r.severity === "warn" ? "⚠" : "✗"} [${r.check}] ${r.message}`));
    lines.push(`── ${data.summary.passed} passed · ${data.summary.warnings} warnings · ${data.summary.blocking} blocking`);
  } else {
    lines.push(JSON.stringify(data, null, 2));
  }
  $("e-report").textContent = lines.join("\n");
}

async function createTournament() {
  const org = $("n-org").value.trim(), slug = $("n-slug").value.trim();
  if (!org || !slug) { flash("org and slug required", false); return; }
  const d = await api("POST", "/api/tournaments/new", { org, slug, name: $("n-name").value.trim() });
  if (d.error) { flash(d.error, false); return; }
  flash("Created " + d.tournament + " (draft)");
  showList();
}

async function loadList() {
  const d = await api("GET", "/api/tournaments");
  const tb = $("tbody");
  tb.innerHTML = "";
  (d.tournaments || []).forEach(t => {
    const rev = t.revision || {};
    const tr = document.createElement("tr");
    const nameCell = document.createElement("td");
    const b1 = document.createElement("b"); b1.textContent = t.name || t.tournament;
    const br = document.createElement("br");
    const muted = document.createElement("span"); muted.className = "muted";
    muted.textContent = t.tournament;
    nameCell.append(b1, br, muted);
    const stCell = document.createElement("td"); stCell.innerHTML = badge(t.status);
    const rvCell = document.createElement("td"); rvCell.innerHTML = badge(rev.workflow || "none");
    const dgCell = document.createElement("td");
    const code = document.createElement("code"); code.className = "muted";
    code.textContent = (rev.digest || "").slice(0, 10) || "—";
    dgCell.appendChild(code);
    const actCell = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "btn-ghost";
    btn.textContent = "Edit";
    btn.onclick = () => showEdit(t.org, t.slug);
    actCell.appendChild(btn);
    tr.append(nameCell, stCell, rvCell, dgCell, actCell);
    tb.appendChild(tr);
  });
}


// ── Event wiring (strict CSP: no inline handlers) ────────────────────
function wire(id, fn) {
  const el = document.getElementById(id);
  if (el) el.onclick = fn;
}
wire("btn-new", showNew);
wire("btn-cancel-new", showList);
wire("btn-back", showList);
wire("btn-save", saveModule);
wire("btn-validate", runValidate);
wire("btn-preview", runPreview);
wire("btn-approve", runApprove);
wire("btn-publish", runPublish);
wire("btn-autofill", runAutofill);
wire("btn-create", createTournament);
wire("btn-logout", clearToken);
document.querySelectorAll(".tab").forEach(() => {});  // tabs wired in renderTabs
document.addEventListener("DOMContentLoaded", () => { ensureToken(); showList(); });
