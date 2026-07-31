
const $ = (id) => document.getElementById(id);
let state = { tournaments: [], current: null, modules: [], dirty: false,
              formModel: null, showForm: false, rawContent: "{}", parsed: null,
              formErrors: {} };

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
  showWizard();
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
    toggleWrap.className = "row form-toggle-wrap";
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
        // raw → form: re-parse the LIVE textarea, never a cached copy —
        // typing in raw view only updates the DOM (and dirty flag), so
        // trusting state.rawContent here would silently discard edits.
        try { state.parsed = JSON.parse($("e-editor").value); }
        catch (e) { flash("Invalid JSON in raw editor: " + e.message, false); return; }
        state.rawContent = $("e-editor").value;
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
      editor.oninput = () => { state.dirty = true; state.rawContent = editor.value; $("e-saved").classList.add("hidden"); };
    }
  } else {
    editor.classList.remove("hidden");
    editor.value = state.rawContent;
    editor.oninput = () => { state.dirty = true; state.rawContent = editor.value; $("e-saved").classList.add("hidden"); };
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

// ── Form validation (client-side, per-field) ───────────────────────────
// The server-side Guide Health Report stays the publish gate; this is
// instant inline feedback so a club admin fixes fields as they type.

function validateField(f, value) {
  const empty = value === undefined || value === null || value === "";
  if (f.required && empty) return "Required";
  if (empty) return null;
  if (f.type === "number" || f.type === "integer") {
    const n = Number(value);
    if (isNaN(n)) return "Must be a number";
    if (f.minimum != null && n < f.minimum) return `Min ${f.minimum}`;
    if (f.maximum != null && n > f.maximum) return `Max ${f.maximum}`;
  }
  if (typeof value === "string") {
    if (f.minLength != null && value.length < f.minLength) return `At least ${f.minLength} characters`;
    if (f.pattern) {
      try { if (!new RegExp(f.pattern).test(value)) return "Doesn't match the required format"; }
      catch (e) { /* schema pattern not JS-compatible — skip client check */ }
    }
  }
  if (f.widget === "select" && f.options && !f.options.includes(value))
    return "Pick from the list";
  if (f.widget === "url") {
    try { const u = new URL(value); if (!/^https?:$/.test(u.protocol)) throw 0; }
    catch (e) { return "Enter a full link (https://…)"; }
  }
  if (f.widget === "email" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value))
    return "Enter a valid email";
  if (f.widget === "date") {
    // Shape check + REAL calendar validation: 2026-99-99 matches the
    // regex but is not a date — parse components and round-trip.
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Use YYYY-MM-DD";
    const [y, m, d] = value.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d));
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d)
      return "Not a real date";
  }
  return null;
}

function validateAll(model, data) {
  const errors = {};
  function walk(fields) {
    for (const f of fields) {
      if (f.widget === "section") { walk(f.children || []); continue; }
      if (f.widget === "coords") {
        for (const c of f.children || []) {
          const msg = validateField(c, getPath(data, c.path));
          if (msg) errors[c.path] = msg;
        }
        continue;
      }
      if (f.widget === "repeater") {
        const rows = getPath(data, f.path);
        if (f.required && !Array.isArray(rows)) { errors[f.path] = "Add at least one"; continue; }
        if (!Array.isArray(rows)) continue;
        rows.forEach((_, i) => {
          for (const c of f.children || []) {
            const cf = Object.assign({}, c, { path: f.path + "[" + i + "]." + c.name });
            const msg = validateField(cf, getPath(data, cf.path));
            if (msg) errors[cf.path] = msg;
          }
        });
        continue;
      }
      if (f.widget === "keyvalue") {
        // Blank keys (from an unfinished "+ Add" row) must block save —
        // a "" key in the JSON would be confusing garbage.
        const obj = getPath(data, f.path);
        if (obj && typeof obj === "object") {
          const keys = Object.keys(obj);
          if (keys.includes("")) errors[f.path] = "Remove the empty key row";
        }
        continue;
      }
      const msg = validateField(f, getPath(data, f.path));
      if (msg) errors[f.path] = msg;
    }
  }
  walk(model.fields);
  return errors;
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
  wrap.dataset.path = f.path;
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

  if (f.widget === "keyvalue") {
    // Dynamic-key string map (e.g. venue.fields.layoutNotes): rows of
    // key + value. Keys are object properties, so rows edit in place.
    // Duplicate/blank keys are rejected inline (a mistyped duplicate
    // field number must never silently overwrite another note).
    const label = document.createElement("label");
    label.textContent = f.label + (f.required ? " *" : "");
    wrap.appendChild(label);
    if (f.help) { const h = document.createElement("p"); h.className = "muted"; h.textContent = f.help; wrap.appendChild(h); }
    const list = document.createElement("div");
    list.className = "rep-list";
    const obj = (value && typeof value === "object") ? value : {};
    Object.keys(obj).forEach((k) => {
      const row = document.createElement("div");
      row.className = "rep-row row-direction";
      const kIn = document.createElement("input");
      kIn.type = "text";
      kIn.value = k;
      kIn.className = "kv-key";
      kIn.placeholder = "Field #";
      const errEl = document.createElement("span");
      errEl.className = "field-err hidden kv-err";
      kIn.oninput = () => {
        const newKey = kIn.value.trim();
        if (newKey === k) { errEl.classList.add("hidden"); kIn.classList.remove("invalid"); return; }
        // Reject blank keys and duplicates against OTHER keys
        if (!newKey) {
          errEl.textContent = "Key can't be blank";
          errEl.classList.remove("hidden"); kIn.classList.add("invalid"); return;
        }
        if (Object.prototype.hasOwnProperty.call(obj, newKey)) {
          errEl.textContent = "Duplicate key — pick a different one";
          errEl.classList.remove("hidden"); kIn.classList.add("invalid"); return;
        }
        errEl.classList.add("hidden"); kIn.classList.remove("invalid");
        const v = obj[k];
        delete obj[k];
        obj[newKey] = v;
        state.dirty = true;
        renderModuleEditor();
      };
      const vIn = document.createElement("input");
      vIn.type = "text";
      vIn.value = obj[k];
      vIn.className = "kv-val";
      vIn.placeholder = "Note…";
      vIn.oninput = () => { obj[k] = vIn.value; state.dirty = true; $("e-saved").classList.add("hidden"); };
      const rm = document.createElement("button");
      rm.className = "btn-ghost btn-sm";
      rm.textContent = "Remove";
      rm.onclick = () => { delete obj[k]; state.dirty = true; renderModuleEditor(); };
      row.appendChild(kIn); row.appendChild(vIn); row.appendChild(rm); row.appendChild(errEl);
      list.appendChild(row);
    });
    const add = document.createElement("button");
    add.className = "btn-ghost btn-sm";
    add.textContent = "+ Add " + f.label.toLowerCase();
    add.onclick = () => {
      const cur = getPath(data, f.path);
      const map = (cur && typeof cur === "object") ? cur : {};
      setPath(data, f.path, map);
      map[""] = "";
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
  const errEl = document.createElement("span");
  errEl.className = "field-err hidden";
  // Persistent error state: a failed Save stores errors in state.formErrors
  // and re-renders — the new render must show them (not wipe them).
  const savedErr = (state.formErrors || {})[f.path];
  if (savedErr) {
    errEl.textContent = savedErr;
    errEl.classList.remove("hidden");
    input.classList.add("invalid");
  }
  input.oninput = () => {
    let v = input.value;
    if (input.type === "checkbox") v = input.checked;
    else if (input.type === "number") v = input.value === "" ? null : parseFloat(input.value);
    else if (v === "" && !f.required) {
      // Empty optional: DELETE the key — writing null can violate a
      // schema that expects a string (null is not a string).
      delPath(data, f.path);
      state.dirty = true;
      $("e-saved").classList.add("hidden");
      if (state.formErrors) delete state.formErrors[f.path];
      errEl.textContent = "";
      errEl.classList.add("hidden");
      input.classList.remove("invalid");
      return;
    }
    setPath(data, f.path, v);
    state.dirty = true;
    $("e-saved").classList.add("hidden");
    const msg = validateField(f, v);
    // clear this field's persisted error as it becomes valid
    if (state.formErrors) delete state.formErrors[f.path];
    errEl.textContent = msg || "";
    errEl.classList.toggle("hidden", !msg);
    input.classList.toggle("invalid", !!msg);
  };
  wrap.appendChild(input);
  wrap.appendChild(errEl);
  return wrap;
}

async function saveModule() {
  const name = currentModuleName();
  let content;
  if (state.formModel && state.showForm) {
    // Form view: serialize the edited object back to module JSON — the
    // SAME shape the raw editor produces, so the backend is untouched.
    // Client-side validation first: fix fields before saving.
    const errors = validateAll(state.formModel, state.parsed || {});
    if (Object.keys(errors).length) {
      state.formErrors = errors;  // renderField reads this on render
      const first = Object.keys(errors)[0];
      flash("Form has errors — fix them first (" + first + ": " + errors[first] + ")", false);
      renderModuleEditor();  // re-render WITH error states (state.formErrors)
      const el = document.querySelector('[data-path="' + CSS.escape(first) + '"]');
      if (el) el.scrollIntoView({ block: "center" });
      return;
    }
    state.formErrors = {};
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
  if (!confirmDiscardChanges()) return;  // autofill overwrites the module
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

// ── New Tournament Wizard ─────────────────────────────────────────────
// 5 guided steps (Basics → Team → Venue → Contacts → Review). Pure UI:
// creation reuses the existing scaffold endpoint, then fills the modules
// through the same atomic PUT+baseDigest save path as the edit view —
// no new server write surface.
let wizStep = 1;

const WIZARD_STEPS = [
  { id: 1, title: "Basics" },
  { id: 2, title: "Team" },
  { id: 3, title: "Venue" },
  { id: 4, title: "Contacts" },
  { id: 5, title: "Review" },
];

function showWizard() {
  wizStep = 1;
  $("view-list").classList.add("hidden");
  $("view-edit").classList.add("hidden");
  $("view-new").classList.remove("hidden");
  renderWizardStep();
}

function renderWizardStep() {
  // Panels + step indicator
  for (const s of WIZARD_STEPS) {
    $("wiz-panel-" + s.id).classList.toggle("hidden", s.id !== wizStep);
    const stepEl = document.querySelector(`.wiz-step[data-step="${s.id}"]`);
    if (stepEl) stepEl.classList.toggle("active", s.id === wizStep);
  }
  // Nav buttons
  $("btn-wiz-prev").classList.toggle("hidden", wizStep === 1);
  $("btn-wiz-next").classList.toggle("hidden", wizStep === 5);
  $("btn-create").classList.toggle("hidden", wizStep !== 5);
  // Review summary on the last step
  if (wizStep === 5) renderWizardReview();
  // Clear any step error
  $("wiz-err-" + wizStep).classList.add("hidden");
}

function wizardFields() {
  return {
    org: $("n-org").value.trim(),
    slug: $("n-slug").value.trim(),
    name: $("n-name").value.trim(),
    dateStart: $("n-date-start").value,
    dateEnd: $("n-date-end").value,
    teamName: $("n-team-name").value.trim(),
    teamShort: $("n-team-short").value.trim(),
    venueName: $("n-venue-name").value.trim(),
    venueAddress: $("n-venue-address").value.trim(),
    mgrName: $("n-mgr-name").value.trim(),
    mgrPhone: $("n-mgr-phone").value.trim(),
    coachName: $("n-coach-name").value.trim(),
    coachPhone: $("n-coach-phone").value.trim(),
  };
}

const IDENT_RE_JS = /^[a-z0-9][a-z0-9-]{0,63}$/;

function validateWizardStep(step, f) {
  const err = $("wiz-err-" + step);
  err.classList.add("hidden");
  let msg = null;
  if (step === 1) {
    if (!f.org) msg = "org is required (e.g. savannah-united)";
    else if (!IDENT_RE_JS.test(f.org)) msg = "org must be lowercase letters/numbers/hyphens, start with a letter or number";
    else if (!f.slug) msg = "slug is required (e.g. disney-showcase-2027)";
    else if (!IDENT_RE_JS.test(f.slug)) msg = "slug must be lowercase letters/numbers/hyphens, start with a letter or number";
    else if (!f.name) msg = "display name is required (parents see this)";
    else if (f.dateStart && f.dateEnd && f.dateStart > f.dateEnd) msg = "start date is after end date";
  } else if (step === 2) {
    if (!f.teamName) msg = "team name is required";
  } else if (step === 3) {
    if (!f.venueName) msg = "venue name is required";
    else if (!f.venueAddress) msg = "venue address is required";
  } else if (step === 4) {
    if (!f.mgrName) msg = "team manager name is required (logistics contact)";
    else if (!f.coachName) msg = "head coach name is required (soccer questions)";
  }
  if (msg) {
    err.textContent = msg;
    err.classList.remove("hidden");
    return false;
  }
  return true;
}

function wizardNext() {
  const f = wizardFields();
  if (!validateWizardStep(wizStep, f)) return;
  wizStep++;
  renderWizardStep();
}

function wizardPrev() {
  if (wizStep > 1) { wizStep--; renderWizardStep(); }
}

function renderWizardReview() {
  const f = wizardFields();
  const lines = [];
  const push = (label, val) => lines.push(`<div class="wiz-rev-row"><span class="muted">${esc(label)}</span><span>${esc(val || "—")}</span></div>`);
  push("org / slug", f.org + " / " + f.slug);
  push("Display name", f.name);
  push("Dates", f.dateStart ? (f.dateStart + " → " + (f.dateEnd || "TBD")) : "not set");
  push("Team", f.teamName + (f.teamShort ? " (" + f.teamShort + ")" : ""));
  push("Venue", f.venueName);
  push("Venue address", f.venueAddress);
  push("Team manager", f.mgrName + (f.mgrPhone ? " · " + f.mgrPhone : ""));
  push("Head coach", f.coachName + (f.coachPhone ? " · " + f.coachPhone : ""));
  $("wiz-review").innerHTML = lines.join("");
}

async function createTournament() {
  if (!confirmDiscardChanges()) return;
  const f = wizardFields();
  if (!validateWizardStep(5, f)) return; // re-validate everything on create

  // 1. Scaffold from the versioned template (server returns checklist)
  const d = await api("POST", "/api/tournaments/new", { org: f.org, slug: f.slug, name: f.name });
  if (d.error) { flash(d.error, false); return; }
  flash("Created " + d.tournament + " (draft)");

  // 2. Read back the fresh tournament for module digests (baseDigest
  //    required by the optimistic-concurrency save path).
  const fresh = await api("GET", `/api/tournament/${f.org}/${f.slug}`);
  const digests = fresh.moduleDigests || {};

  // 3. Fill the modules the wizard collected — through the SAME
  //    PUT+baseDigest path the edit view uses (atomic, conflict-safe).
  //    Track WHICH fields were actually confirmed written: a mid-sequence
  //    failure must never be reported as full completion.
  const modules = {};

  const tournament = { name: f.name };
  if (f.dateStart || f.dateEnd) {
    tournament.dates = { start: f.dateStart || "", end: f.dateEnd || "" };
  }
  modules["tournament.json"] = { tournament };

  modules["team.json"] = { team: { name: f.teamName } };
  if (f.teamShort) modules["team.json"].team.shortName = f.teamShort;

  modules["venue.json"] = { venue: { name: f.venueName, address: f.venueAddress } };

  const contacts = {};
  if (f.mgrName) {
    contacts.manager = { name: f.mgrName };
    if (f.mgrPhone) contacts.manager.phone = f.mgrPhone;
  }
  if (f.coachName) {
    contacts.coach = { name: f.coachName };
    if (f.coachPhone) contacts.coach.phone = f.coachPhone;
  }
  modules["contacts.json"] = { contacts };

  // Which checklist fields each module satisfies (must mirror the
  // server's _REQUIRED_CONTENT entries for these modules). Only fields
  // whose values were ACTUALLY entered count — e.g. empty dates are not
  // in the PUT content, so their checklist entries stay incomplete.
  const FIELDS_PER_MODULE = {
    "tournament.json": ["tournament.name"],
    "team.json": ["team.name"],
    "venue.json": ["venue.name", "venue.address"],
    "contacts.json": ["contacts.manager", "contacts.coach"],
  };
  if (f.dateStart) FIELDS_PER_MODULE["tournament.json"].push("tournament.dates.start");
  if (f.dateEnd) FIELDS_PER_MODULE["tournament.json"].push("tournament.dates.end");

  const completedFields = new Set();
  const failures = [];
  for (const [file, content] of Object.entries(modules)) {
    // api() THROWS on 401/409/network errors (it flashes the conflict
    // itself) — catch so a mid-sequence failure still lands in the
    // accurate completion report below instead of aborting the wizard.
    let res;
    try {
      res = await api("PUT", `/api/tournament/${f.org}/${f.slug}/module/${file}`, {
        content: JSON.stringify(content, null, 2),
        baseDigest: digests[file],
      });
    } catch (e) {
      failures.push({ file, error: String(e && e.message || e) });
      break;
    }
    if (res.error) {
      failures.push({ file, error: res.error });
      break; // later modules still need the digests we have — stop and report
    }
    (FIELDS_PER_MODULE[file] || []).forEach(fld => completedFields.add(fld));
  }

  // 4. The remaining checklist = server checklist MINUS only the fields
  //    whose module write was CONFIRMED (a failed PUT means that module's
  //    fields are still empty, whatever the server's template said).
  const completion = wizardCompletion(d.checklist, completedFields, failures);
  if (completion.failures.length) {
    const failedNames = completion.failures.map(x => x.file).join(", ");
    flash(`Created ${d.tournament}, but setup is INCOMPLETE — ` +
          `failed to save: ${failedNames}. ` +
          (completion.remaining.length ? `Still to fill: ${completion.remaining.map(c => c.field).join(", ")}. ` : "") +
          "The draft is open below — re-save those modules (Retry) and validate before publishing.",
          false);
  } else if (completion.remaining.length) {
    flash("Draft created — optional fields to fill: " + completion.remaining.map(c => c.field).join(", "));
  } else {
    flash("Draft created — all required fields set. Validate, approve, publish when ready.");
  }
  showEdit(f.org, f.slug);
}

// Pure helper (node-testable): the wizard completion decision. Takes the
// server checklist, the set of fields whose module writes were CONFIRMED,
// and any failures. A failed module write must NOT have its fields counted
// as complete — whatever the template checklist said.
function wizardCompletion(checklist, completedFields, failures) {
  const remaining = (checklist || []).filter(c => !completedFields.has(c.field));
  return { failures: failures || [], remaining };
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


// ── Revision history ───────────────────────────────────────────────────
async function toggleHistory() {
  const card = $("e-history-card");
  if (!card.classList.contains("hidden")) { card.classList.add("hidden"); return; }
  const name = currentModuleName();
  const org = state.current.org, slug = state.current.slug;
  const q = name === "manifest.json" ? "" : "?module=" + encodeURIComponent(name);
  const d = await api("GET", `/api/tournament/${org}/${slug}/history${q}`);
  if (d && d.error) { flash(d.error, false); return; }
  const list = $("e-history-list");
  list.innerHTML = "";
  const diffEl = $("e-diff");
  diffEl.textContent = "";
  (d.history || []).forEach((c) => {
    const row = document.createElement("div");
    row.className = "row history-row";
    const info = document.createElement("div");
    info.className = "grow";
    const msg = document.createElement("div");
    msg.className = "history-msg";
    msg.textContent = c.message || "(no message)";
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = `${c.sha.slice(0, 10)} · ${c.date} · ${c.author}`;
    info.appendChild(msg); info.appendChild(meta);
    const view = document.createElement("button");
    view.className = "btn-ghost btn-sm";
    view.textContent = "Diff";
    view.onclick = async () => {
      const r = await api("GET", `/api/tournament/${org}/${slug}/diff/${encodeURIComponent(name)}?from=${c.sha}`);
      if (r && r.error) { flash(r.error, false); return; }
      diffEl.textContent = r.diff || "(no changes in this revision)";
      diffEl.scrollIntoView({ block: "nearest" });
    };
    row.appendChild(info); row.appendChild(view);
    list.appendChild(row);
  });
  if (!(d.history || []).length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No committed revisions for this module yet. (Saves to the working tree appear here once published.)";
    list.appendChild(empty);
  }
  card.classList.remove("hidden");
}

// ── Event wiring (strict CSP: no inline handlers) ──────────────────────
function wire(id, fn) {
  const el = document.getElementById(id);
  if (el) el.onclick = fn;
}
wire("btn-new", showNew);
wire("btn-cancel-new", showList);
wire("btn-wiz-prev", wizardPrev);
wire("btn-wiz-next", wizardNext);
wire("btn-back", showList);
wire("btn-save", saveModule);
wire("btn-validate", runValidate);
wire("btn-preview", runPreview);
wire("btn-approve", runApprove);
wire("btn-publish", runPublish);
wire("btn-history", toggleHistory);
wire("btn-history-close", () => { $("e-history-card").classList.add("hidden"); });
wire("btn-autofill", runAutofill);
wire("btn-create", createTournament);
wire("btn-logout", clearToken);
document.querySelectorAll(".tab").forEach(() => {});  // tabs wired in renderTabs
document.addEventListener("DOMContentLoaded", () => { ensureToken(); showList(); });
