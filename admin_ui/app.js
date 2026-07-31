
const $ = (id) => document.getElementById(id);
let state = { tournaments: [], current: null, modules: [], dirty: false,
              formModel: null, showForm: false, rawContent: "{}", parsed: null };

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
  if (!confirmDiscardChanges()) return;
  state.dirty = false;  // already confirmed; don't re-prompt in showList
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

function confirmDiscardChanges() {
  if (state.dirty && !confirm("Unsaved changes — discard?")) return false;
  return true;
}

function showList() {
  if (!confirmDiscardChanges()) return;
  $("view-list").classList.remove("hidden");
  $("view-new").classList.add("hidden");
  $("view-edit").classList.add("hidden");
  loadList();
}
function showNew() {
  if (!confirmDiscardChanges()) return;
  $("view-list").classList.add("hidden");
  $("view-new").classList.remove("hidden");
  $("view-edit").classList.add("hidden");
}
async function showEdit(org, slug) {
  if (!confirmDiscardChanges()) return;
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
  if (!confirmDiscardChanges()) return;
  state.dirty = false;
  $("e-saved").classList.add("hidden");
  $("e-module-desc").textContent = `Module: ${name}`;
  const r = await api("GET", `/api/tournament/${state.current.org}/${state.current.slug}`);
  // Edit the RAW module file (source of truth) — not the compiled bundle.
  // manifest.json is edited via the manifest object.
  let content = (r.moduleFiles && r.moduleFiles[name]) || "";
  if (name === "manifest.json" && !content) content = JSON.stringify(r.manifest, null, 2);
  state.current.moduleFiles = r.moduleFiles || {};
  state.current.moduleDigests = r.moduleDigests || {};
  // Form model for this module (static schema-derived UI metadata)
  let model = null;
  if (name !== "manifest.json") {
    try {
      const fm = await api("GET", `/api/forms/${name}`);
      if (fm && fm.fields) model = fm;
    } catch (e) { model = null; }
  }
  state.formModel = model;
  state.showForm = !!model;   // forms on by default when available
  state.rawContent = content || "{}";
  try { state.parsed = JSON.parse(state.rawContent); } catch (e) { state.parsed = null; }
  renderModuleEditor();
  [...$("e-tabs").children].forEach(b => b.classList.toggle("active", b.textContent === name.replace(".json", "")));
}

function renderModuleEditor() {
  const editor = $("e-editor");
  const toggleWrap = document.createElement("div");
  if (state.formModel) {
    // Form view (default) with a toggle to the raw JSON editor
    editor.classList.add("hidden");
    toggleWrap.className = "row";
    toggleWrap.style.marginBottom = "8px";
    const toggle = document.createElement("button");
    toggle.className = "btn-ghost";
    toggle.textContent = state.showForm ? "Raw JSON" : "Form view";
    toggle.id = "btn-toggle-view";
    toggle.onclick = () => {
      if (state.showForm) {
        // form → raw: push current form object into the raw editor
        try { state.rawContent = JSON.stringify(state.parsed || {}, null, 2); }
        catch (e) { flash("Form state invalid: " + e.message, false); return; }
      } else {
        // raw → form: re-parse the raw editor into the form object
        try { state.parsed = JSON.parse(state.rawContent); }
        catch (e) { flash("Invalid JSON in raw editor: " + e.message, false); return; }
      }
      state.showForm = !state.showForm;
      renderModuleEditor();
    };
    toggleWrap.appendChild(toggle);
    $("e-tabs").after ? null : null;
    // remove previous toggle + form, then rebuild
    const oldToggle = document.getElementById("toggle-wrap");
    if (oldToggle) oldToggle.remove();
    const oldForm = document.getElementById("form-panel");
    if (oldForm) oldForm.remove();
    toggleWrap.id = "toggle-wrap";
    editor.parentNode.insertBefore(toggleWrap, editor);
    if (state.showForm) {
      const form = renderForm(state.formModel, state.parsed);
      form.id = "form-panel";
      editor.parentNode.insertBefore(form, editor.nextSibling);
    } else {
      editor.classList.remove("hidden");
      editor.value = state.rawContent;
      editor.oninput = () => { state.dirty = true; $("e-saved").classList.add("hidden"); };
    }
  } else {
    editor.classList.remove("hidden");
    editor.value = state.rawContent;
    editor.oninput = () => { state.dirty = true; $("e-saved").classList.add("hidden"); };
  }
}

// ── Form engine ─────────────────────────────────────────────────────────
function getPath(obj, path) {
  const parts = path.split(".");
  let cur = obj;
  for (const p of parts) {
    if (cur == null) return undefined;
    const m = p.match(/^(.+)\[(\d+)\]$/);
    if (m) cur = cur[m[1]] && cur[m[1]][+m[2]];
    else cur = cur[p];
  }
  return cur;
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    const last = i === parts.length - 1;
    const m = p.match(/^(.+)\[(\d+)\]$/);
    if (m) {
      const key = m[1], idx = +m[2];
      if (!cur[key]) cur[key] = [];
      while (cur[key].length <= idx) cur[key].push({});
      if (last) cur[key][idx] = value;
      else cur = cur[key][idx];
    } else {
      if (last) cur[p] = value;
      else { if (cur[p] == null) cur[p] = {}; cur = cur[p]; }
    }
  }
  return obj;
}

function delPath(obj, path) {
  const parts = path.split(".");
  const last = parts.pop();
  const parent = getPath(obj, parts.join("."));
  if (parent && typeof parent === "object") {
    const m = last.match(/^(.+)\[(\d+)\]$/);
    if (m) parent[m[1]].splice(+m[2], 1);
    else delete parent[last];
  }
}

function renderForm(model, data) {
  const form = document.createElement("div");
  form.className = "form-panel";
  if (model.help) {
    const h = document.createElement("p");
    h.className = "muted";
    h.textContent = model.help;
    form.appendChild(h);
  }
  for (const f of model.fields) form.appendChild(renderField(f, data));
  return form;
}

function fieldInput(f, value) {
  const input = document.createElement(f.widget === "textarea" ? "textarea"
                : f.widget === "select" ? "select" : "input");
  if (f.widget === "textarea") input.rows = 3;
  if (input.tagName === "INPUT") {
    const typeMap = { text: "text", url: "url", email: "email", date: "date",
                      number: "number", checkbox: "checkbox" };
    input.type = typeMap[f.widget] || "text";
    if (f.widget === "number" && f.minimum != null) input.min = f.minimum;
    if (f.widget === "number" && f.maximum != null) input.max = f.maximum;
  }
  if (f.widget === "select") {
    for (const opt of f.options || []) {
      const o = document.createElement("option");
      o.value = opt; o.textContent = opt;
      input.appendChild(o);
    }
  }
  if (f.widget === "checkbox") input.checked = !!value;
  else input.value = value == null ? "" : value;
  return input;
}

function renderField(f, data, rowIndex) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const value = getPath(data, f.path);
  const has = value !== undefined;

  if (f.widget === "section") {
    const fs = document.createElement("fieldset");
    const lg = document.createElement("legend");
    lg.textContent = f.label;
    fs.appendChild(lg);
    if (f.help) { const h = document.createElement("p"); h.className = "muted"; h.textContent = f.help; fs.appendChild(h); }
    for (const c of f.children || []) fs.appendChild(renderField(c, data));
    wrap.appendChild(fs);
    return wrap;
  }

  if (f.widget === "repeater") {
    const label = document.createElement("label");
    label.textContent = f.label + (f.required ? " *" : "");
    wrap.appendChild(label);
    if (f.help) { const h = document.createElement("p"); h.className = "muted"; h.textContent = f.help; wrap.appendChild(h); }
    const list = document.createElement("div");
    list.className = "rep-list";
    const rows = Array.isArray(value) ? value : [];
    rows.forEach((_, i) => {
      const row = document.createElement("div");
      row.className = "rep-row";
      for (const c of f.children || []) {
        const cf = Object.assign({}, c, { path: f.path + "[" + i + "]." + c.name });
        row.appendChild(renderField(cf, data));
      }
      const rm = document.createElement("button");
      rm.className = "btn-ghost btn-sm";
      rm.textContent = "Remove";
      rm.onclick = () => { delPath(data, f.path + "[" + i + "]"); state.dirty = true; renderModuleEditor(); };
      row.appendChild(rm);
      list.appendChild(row);
    });
    const add = document.createElement("button");
    add.className = "btn-ghost btn-sm";
    add.textContent = "+ Add " + f.label.toLowerCase();
    add.onclick = () => {
      if (!Array.isArray(getPath(data, f.path))) setPath(data, f.path, []);
      setPath(data, f.path + "[" + rows.length + "]", {});
      state.dirty = true;
      renderModuleEditor();
    };
    wrap.appendChild(list);
    wrap.appendChild(add);
    return wrap;
  }

  if (f.widget === "coords") {
    const label = document.createElement("label");
    label.textContent = f.label + (f.required ? " *" : "");
    wrap.appendChild(label);
    const row = document.createElement("div");
    row.className = "row";
    for (const c of f.children || []) {
      const sub = document.createElement("div");
      sub.className = "grow";
      const sl = document.createElement("label");
      sl.className = "muted"; sl.textContent = c.label;
      const inp = fieldInput(c, getPath(data, c.path));
      inp.oninput = () => { setPath(data, c.path, inp.type === "number" ? parseFloat(inp.value) : inp.value); state.dirty = true; $("e-saved").classList.add("hidden"); };
      sub.appendChild(sl); sub.appendChild(inp);
      row.appendChild(sub);
    }
    wrap.appendChild(row);
    return wrap;
  }

  const label = document.createElement("label");
  label.textContent = f.label + (f.required ? " *" : "");
  wrap.appendChild(label);
  if (f.help) { const h = document.createElement("p"); h.className = "muted"; h.textContent = f.help; wrap.appendChild(h); }
  const input = fieldInput(f, has ? value : "");
  input.oninput = () => {
    let v = input.value;
    if (input.type === "checkbox") v = input.checked;
    else if (input.type === "number") v = input.value === "" ? null : parseFloat(input.value);
    else if (v === "" && !f.required) v = null;
    setPath(data, f.path, v);
    state.dirty = true;
    $("e-saved").classList.add("hidden");
  };
  wrap.appendChild(input);
  return wrap;
}

async function saveModule() {
  const name = currentModuleName();
  let content;
  if (state.formModel && state.showForm) {
    // Form view: serialize the edited object back to module JSON — the
    // SAME shape the raw editor produces, so the backend is untouched.
    try {
      content = JSON.stringify(state.parsed || {}, null, 2);
    } catch (e) { flash("Form state invalid: " + e.message, false); return; }
  } else {
    content = $("e-editor").value;
    try { JSON.parse(content); } catch (e) {
      flash("Invalid JSON: " + e.message, false); return;
    }
  }
  const baseDigest = (state.current.moduleDigests || {})[name] || null;
  const d = await api("PUT", `/api/tournament/${state.current.org}/${state.current.slug}/module/${name}`,
            { content, baseDigest });
  if (d && d.error) { flash(d.error, false); return; }
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
                      {}, pt);
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
