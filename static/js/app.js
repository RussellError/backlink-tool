"use strict";
/* Backlink Builder front end. All dynamic text goes through textContent / text nodes (no innerHTML). */
const $ = (s, r = document) => r.querySelector(s);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, { method = "GET", json, form } = {}) {
  const opts = { method, headers: { "X-Requested-With": "BacklinkBuilder" } };
  if (json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(json); }
  if (form) opts.body = form;
  let res;
  try { res = await fetch(path, opts); }
  catch (e) { throw new Error("Cannot reach the app. Check that it is still running in Termux."); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

function toast(message, kind) {
  const t = h("div", { class: "toast " + (kind || ""), text: message });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 7000 : 3800);
}
async function run(fn) { try { return await fn(); } catch (e) { toast(e.message, "error"); return null; } }

const fmt = ts => ts ? new Date(ts).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
const OPP = { new: ["Not assessed", "muted"], eligible: ["Eligible", "ok"], review: ["Needs review", "warn"],
  login: ["Login required", "info"], captcha: ["CAPTCHA", "violet"], outreach: ["Outreach", "info"], rejected: ["Rejected", "bad"] };
const SUB = { submitted: ["Submitted", "ok"], awaiting_approval: ["Awaiting approval", "warn"],
  pending_verification: ["Pending verification", "warn"], captcha_required: ["CAPTCHA required", "violet"],
  login_required: ["Login required", "info"], manual_action: ["Manual action required", "warn"],
  rejected: ["Rejected", "bad"], failed: ["Failed", "bad"], already_submitted: ["Already submitted", "muted"] };
const BL = { pending: ["Pending first check", "muted"], live: ["Live", "ok"], live_nofollow: ["Live (nofollow)", "ok"],
  live_sponsored: ["Live (sponsored)", "ok"], live_ugc: ["Live (ugc)", "ok"], redirected: ["Redirected", "warn"],
  not_found: ["Not found", "bad"], access_restricted: ["Access restricted", "info"],
  temporary_failure: ["Temporary failure", "warn"], inconclusive: ["Inconclusive", "warn"] };
const JOBS = { discovery: "Discovery", evaluate: "Assessment", submit: "Submissions", verify: "Verification" };
const JSTATUS = { queued: "Queued", running: "Running", paused: "Paused", interrupted: "Interrupted (resume to continue)",
  cancelled: "Cancelled", done: "Finished", failed: "Failed" };

function badge(map, key) {
  const [label, cls] = map[key] || [key, "muted"];
  return h("span", { class: "badge " + cls, text: label });
}
function safeLink(url, text) {
  if (!/^https?:\/\//i.test(url || "")) return h("span", { text: url || "" });
  return h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: text || url });
}
const empty = msg => h("div", { class: "empty", text: msg });
function btn(label, onclick, cls) { return h("button", { class: "btn " + (cls || ""), type: "button", onclick, text: label }); }

const S = { targets: [], target: null, categories: {} };
async function loadTargets() {
  const d = await api("/api/targets");
  S.targets = d.targets; S.categories = d.categories;
  const saved = Number(localStorage.getItem("bb_target"));
  S.target = d.targets.find(t => t.id === saved) || d.targets[0] || null;
  if (S.target) localStorage.setItem("bb_target", S.target.id);
  const sel = $("#target-select");
  sel.replaceChildren();
  if (!d.targets.length) sel.append(h("option", { text: "No website yet" }));
  for (const t of d.targets) {
    sel.append(h("option", { value: t.id, selected: S.target && t.id === S.target.id, text: t.name || t.domain }));
  }
  sel.disabled = !d.targets.length;
  sel.addEventListener("change", () => { localStorage.setItem("bb_target", sel.value); location.reload(); });
}
const tq = () => `target_id=${S.target.id}`;

/* ---------- jobs UI ---------- */
function jobRow(job, after) {
  const act = (name) => () => run(async () => { await api(`/api/jobs/${job.id}/${name}`, { method: "POST" }); after && after(); });
  const buttons = [];
  if (job.status === "running") buttons.push(btn("Pause", act("pause"), "small"));
  if (job.status === "paused" || job.status === "interrupted") buttons.push(btn("Resume", act("resume"), "small"));
  if (["running", "paused", "interrupted", "queued"].includes(job.status)) buttons.push(btn("Cancel", act("cancel"), "small danger"));
  const bar = h("progress", { max: job.total || 100, value: job.total ? job.progress : null });
  return h("div", { class: "job" },
    h("div", { class: "row wrap" }, h("strong", { text: JOBS[job.kind] || job.kind }),
      h("span", { class: "badge " + (job.status === "running" ? "ok" : "warn"), text: JSTATUS[job.status] || job.status })),
    bar,
    h("div", { class: "muted small", text: job.total ? `${job.progress} of ${job.total}. ${job.message || ""}` : (job.message || "Starting...") }),
    h("div", { class: "row wrap" }, buttons));
}
async function jobBar(onFinished) {
  const box = $("#job-bar");
  if (!box || !S.target) return;
  let was = 0;
  const tick = async () => {
    let d;
    try { d = await api(`/api/state?${tq()}`); } catch (e) { setTimeout(tick, 8000); return; }
    box.replaceChildren();
    if (d.jobs.length) {
      const card = h("section", { class: "card" }, h("h3", { text: "Running jobs" }), d.jobs.map(j => jobRow(j, tick)));
      box.append(card);
    }
    if (was && !d.jobs.length && onFinished) onFinished();
    was = d.jobs.length;
    setTimeout(tick, d.jobs.length ? 3000 : 10000);
  };
  tick();
}

/* ---------- dashboard ---------- */
function typeBoxes(selected) {
  const box = $("#opp-types");
  box.replaceChildren();
  const sel = new Set((selected || "").split(",").filter(Boolean));
  for (const [key, label] of Object.entries(S.categories)) {
    box.append(h("label", { class: "check" }, h("input", { type: "checkbox", name: "opp_types", value: key, checked: sel.has(key) }), label));
  }
}
function fillSite(t) {
  const f = $("#site-form");
  t = t || {};
  for (const k of ["id", "url", "name", "description", "category", "keywords", "languages", "countries", "contact_email"]) {
    f.elements[k].value = t[k] || "";
  }
  typeBoxes(t.opp_types);
}
function stat(label, value, cls, wide) { return h("div", { class: `stat ${cls || ""} ${wide ? "wide" : ""}` }, h("div", { class: "label", text: label }), h("div", { class: "value", text: value })); }

async function pageIndex() {
  fillSite(S.target);
  $("#site-form").addEventListener("submit", ev => { ev.preventDefault(); run(async () => {
    const f = ev.target, data = { opp_types: [...f.querySelectorAll('input[name="opp_types"]:checked')].map(i => i.value) };
    for (const k of ["id", "url", "name", "description", "category", "keywords", "languages", "countries", "contact_email"]) data[k] = f.elements[k].value;
    if (!data.id) delete data.id;
    const r = await api("/api/targets", { method: "POST", json: data });
    localStorage.setItem("bb_target", r.target.id);
    toast("Website saved", "ok"); setTimeout(() => location.reload(), 500);
  }); });
  $("#new-site").addEventListener("click", () => { fillSite(null); $("#site-card").open = true; $("#site-form").elements.url.focus(); });
  if (!S.target) { $("#no-site").hidden = false; ["discovery-card", "stats", "jobs-card"].forEach(i => $("#" + i) && ($("#" + i).closest("section")?.setAttribute("hidden", ""))); return; }
  $("#site-card").open = false;
  let active = null;
  const refresh = async () => {
    let d;
    try { d = await api(`/api/state?${tq()}`); } catch (e) { $("#discovery-status").textContent = e.message; setTimeout(refresh, 8000); return; }
    const s = d.stats;
    const grid = $("#stats"); grid.replaceChildren(
      stat("Target website", `${s.target.name ? s.target.name + " - " : ""}${s.target.url}`, "", true),
      stat("Opportunities discovered", s.total_opportunities),
      stat("Awaiting review", s.awaiting_review, s.awaiting_review ? "warn" : ""),
      stat("Eligible for submission", s.eligible),
      stat("Submission attempts", s.submission_attempts),
      stat("Confirmed live backlinks", s.confirmed_live, "good"),
      stat("Failed submissions", s.failed_submissions, s.failed_submissions ? "bad" : ""),
      stat("Links needing a recheck", s.needs_recheck, s.needs_recheck ? "warn" : ""));
    const pipe = $("#pipeline"); pipe.replaceChildren();
    const total = s.total_opportunities || 1;
    for (const k of ["eligible", "review", "login", "captcha", "outreach", "rejected", "new"]) {
      const n = s.by_status[k] || 0;
      pipe.append(h("div", { class: "pipe-row" }, h("a", { href: `/opportunities#status=${k}`, text: OPP[k][0] }),
        h("progress", { max: total, value: n }), h("span", { text: n })));
    }
    if (!s.total_opportunities) pipe.replaceChildren(empty("Nothing discovered yet. Start discovery, import a list, or add a page by hand."));
    $("#jobs").replaceChildren(...d.jobs.map(j => jobRow(j, refresh)));
    $("#jobs-card").hidden = !d.jobs.length;
    active = d.jobs.find(j => j.kind === "discovery") || null;
    $("#btn-discover").disabled = !!active;
    $("#btn-pause").hidden = !(active && active.status === "running");
    $("#btn-resume").hidden = !(active && ["paused", "interrupted"].includes(active.status));
    $("#btn-cancel").hidden = !active;
    const last = d.recent_jobs.find(j => j.kind === "discovery");
    $("#discovery-status").textContent = active ? (active.message || "Working...") :
      (last ? `Last discovery: ${JSTATUS[last.status].toLowerCase()} ${fmt(last.updated_at)}${last.error ? " - " + last.error : ""}` : "");
    const p = d.providers, srcs = [];
    if (p.seed_sources) srcs.push("starter list"); if (p.github_search) srcs.push("GitHub search"); if (p.searxng) srcs.push("SearXNG");
    $("#sources-note").textContent = "Active sources: " + (srcs.join(", ") || "none (import URLs or add reference pages)") + ".";
    const act = $("#activity"); act.replaceChildren();
    for (const l of d.activity) act.append(h("li", { class: l.level }, h("time", { text: fmt(l.ts) }), h("span", { text: `${l.message}${l.detail ? " - " + l.detail.slice(0, 140) : ""}` })));
    if (!d.activity.length) act.append(h("li", { class: "muted", text: "No activity yet." }));
    setTimeout(refresh, d.jobs.length ? 3000 : 10000);
  };
  const ex = $("#exports");
  for (const k of ["opportunities", "submissions", "backlinks"]) for (const f of ["csv", "json"]) {
    ex.append(h("a", { class: "btn small", href: `/export/${k}.${f}?${tq()}`, text: `${k[0].toUpperCase() + k.slice(1)} (${f.toUpperCase()})` }));
  }
  $("#btn-discover").addEventListener("click", () => run(async () => {
    await api("/api/jobs", { method: "POST", json: { kind: "discovery", target_id: S.target.id, reference_urls: $("#reference-urls").value } });
    toast("Discovery started", "ok"); refresh();
  }));
  const ctl = name => () => run(async () => { await api(`/api/jobs/${active.id}/${name}`, { method: "POST" }); refresh(); });
  $("#btn-pause").addEventListener("click", ctl("pause"));
  $("#btn-resume").addEventListener("click", ctl("resume"));
  $("#btn-cancel").addEventListener("click", ctl("cancel"));
  refresh();
}

/* ---------- opportunities ---------- */
function kv(obj) {
  const dl = h("dl", { class: "kv" });
  for (const [k, v] of Object.entries(obj || {})) {
    if (k === "dead_links") continue;
    let val = Array.isArray(v) ? v.join(", ") : (v && typeof v === "object") ? JSON.stringify(v) : String(v ?? "");
    dl.append(h("dt", { text: k.replace(/_/g, " ") }), h("dd", { text: val || "(none)" }));
  }
  return dl;
}
async function pageOpportunities() {
  if (!S.target) { $("#no-site").hidden = false; document.querySelectorAll("main > :not(#no-site)").forEach(e => e.hidden = true); return; }
  const Q = { status: "", category: "", q: "", offset: 0, limit: 50 };
  const m = /status=(\w+)/.exec(location.hash); if (m && (OPP[m[1]] || m[1] === "all")) Q.status = m[1] === "all" ? "" : m[1];
  const list = $("#opp-list"), more = $("#more");
  for (const sel of [$("#add-category"), $("#import-category")]) for (const [k, v] of Object.entries(S.categories)) sel.append(h("option", { value: k, text: v }));
  for (const [k, v] of Object.entries(S.categories)) $("#f-category").append(h("option", { value: k, text: v }));

  async function chips() {
    const d = await api(`/api/state?${tq()}`);
    const box = $("#chips"); box.replaceChildren();
    const mk = (key, label, n) => box.append(h("button", { class: "chip", type: "button", "aria-pressed": String(Q.status === key),
      onclick: () => { Q.status = key; load(true); chips(); }, text: `${label} (${n})` }));
    mk("", "All", d.stats.total_opportunities);
    for (const k of Object.keys(OPP)) mk(k, OPP[k][0], d.stats.by_status[k] || 0);
  }
  async function load(reset) {
    if (reset) { Q.offset = 0; list.replaceChildren(); }
    const p = new URLSearchParams({ target_id: S.target.id, limit: Q.limit, offset: Q.offset });
    if (Q.status) p.set("status", Q.status); if (Q.category) p.set("category", Q.category); if (Q.q) p.set("q", Q.q);
    const d = await run(() => api("/api/opportunities?" + p)); if (!d) return;
    d.opportunities.forEach(o => list.append(oppItem(o)));
    Q.offset += d.opportunities.length;
    more.hidden = Q.offset >= d.total;
    if (!list.children.length) list.append(empty(Q.status || Q.q || Q.category ? "No opportunities match this filter." : "No opportunities yet. Start discovery on the Dashboard, import a list, or add one by hand."));
  }
  function oppItem(o) {
    const wrap = h("article", { class: "item", id: "opp-" + o.id });
    const reload = () => { chips(); const fresh = h("div"); api(`/api/opportunities?${tq()}&q=${encodeURIComponent(o.url)}&limit=1`).then(d => { const n = d.opportunities.find(x => x.id === o.id); if (n) wrap.replaceWith(oppItem(n)); }); };
    const badges = h("div", { class: "badges" }, badge(OPP, o.status), h("span", { class: "badge muted", text: S.categories[o.category] || o.category }));
    if (o.score != null) badges.append(h("span", { class: "badge info", text: `Score ${o.score}/100 (heuristic)` }));
    if (o.submission_status) badges.append(badge(SUB, o.submission_status));
    if (o.approved) badges.append(h("span", { class: "badge ok", text: "Approved" }));
    const actions = h("div", { class: "actions" });
    let detail = null;
    actions.append(btn("Details", async () => {
      if (detail) { detail.remove(); detail = null; return; }
      detail = h("div", { class: "detail" }, h("span", { class: "muted", text: "Loading..." })); wrap.append(detail);
      const d = await run(() => api(`/api/opportunities/${o.id}`)); if (!d) { detail.remove(); detail = null; return; }
      detail.replaceChildren(...detailBody(o, d, reload));
    }, "small"));
    if (o.status === "eligible") actions.append(btn(o.approved ? "Remove approval" : "Approve for submission", () => run(async () => {
      await api(`/api/opportunities/${o.id}/approve`, { method: "POST", json: { approved: !o.approved } }); reload(); }), "small"));
    wrap.append(h("div", { class: "top" }, h("div", {}, h("div", { class: "title", text: o.title || o.domain }), h("div", { class: "url" }, safeLink(o.url)))), badges,
      o.notes ? h("div", { class: "muted small", text: o.notes }) : null, o.last_error ? h("div", { class: "small", text: "Last error: " + o.last_error }) : null, actions);
    return wrap;
  }
  function detailBody(o, d, reload) {
    const out = [], a = d.assessment;
    out.push(h("div", {}, h("h4", { text: "Assessment" }), h("div", { text: a ? a.summary : "Not assessed yet. Use Re-assess." }),
      a ? h("div", { class: "muted small", text: `Assessed ${fmt(a.assessed_at)}. HTTP status ${a.http_status ?? "n/a"}.` }) : null));
    if (a && a.flags.length) out.push(h("div", {}, h("h4", { text: "Things to know" }), h("ul", { class: "plain" }, a.flags.map(f => h("li", { text: f })))));
    if (a && a.breakdown.length) {
      const t = h("table", {}, h("thead", {}, h("tr", {}, ["Factor", "Points", "Basis", "Detail"].map(x => h("th", { text: x })))), h("tbody"));
      for (const b of a.breakdown) t.tBodies[0].append(h("tr", {}, h("td", { text: b.factor }), h("td", { text: (b.points > 0 ? "+" : "") + b.points }),
        h("td", {}, h("span", { class: "badge " + (b.kind === "verified" ? "ok" : "warn"), text: b.kind === "verified" ? "Verified" : "Estimate" })), h("td", { text: b.note })));
      out.push(h("div", {}, h("h4", { text: "Score breakdown" }), h("div", { class: "tablewrap" }, t),
        h("p", { class: "muted small", text: "Verified = read directly from the page. Estimate = heuristic. No Domain Authority, traffic or index counts are used." })));
    }
    if (a) { out.push(h("div", {}, h("h4", { text: "Verified facts" }), kv(a.verified))); if (Object.keys(a.estimated).length) out.push(h("div", {}, h("h4", { text: "Estimates" }), kv(a.estimated))); }
    const prepOut = h("div");
    const manualUrl = h("input", { type: "text", inputmode: "url", placeholder: "Listing URL (optional)" });
    const sel = h("select", {}, ["review", "eligible", "login", "captcha", "outreach", "rejected"].map(k => h("option", { value: k, text: OPP[k][0], selected: k === o.status })));
    out.push(h("div", { class: "stack" }, h("h4", { text: "Actions" }),
      h("div", { class: "actions" },
        btn("Re-assess", () => run(async () => { await api("/api/jobs", { method: "POST", json: { kind: "evaluate", target_id: S.target.id, ids: [o.id] } }); toast("Assessment queued", "ok"); }), "small"),
        btn("Preview submission", () => run(async () => {
          const p = await api(`/api/opportunities/${o.id}/prepare`, { method: "POST" });
          prepOut.replaceChildren(h("div", {}, h("span", { class: "badge " + (p.can_auto ? "ok" : "warn"), text: p.can_auto ? "Automatic submission is permitted" : "Manual action needed" }),
            h("ul", { class: "plain" }, p.reasons.map(r => h("li", { text: r }))),
            p.payload_preview.length ? kv(Object.fromEntries(p.payload_preview.map(x => [x.field, x.value]))) : null,
            h("p", {}, "Manual page: ", safeLink(p.manual_url))));
        }), "small"),
        o.status === "eligible" && o.approved ? btn("Submit now", () => run(async () => {
          if (!confirm("Submit your website details to this site now?")) return;
          await api("/api/jobs", { method: "POST", json: { kind: "submit", target_id: S.target.id, ids: [o.id] } }); toast("Submission started", "ok"); }), "small primary") : null,
        btn("Draft outreach", () => run(async () => { await api(`/api/opportunities/${o.id}/outreach`, { method: "POST" });
          const d2 = await api(`/api/opportunities/${o.id}`); draftBox.replaceChildren(...drafts(d2.drafts)); }), "small")),
      prepOut,
      h("div", { class: "row" }, sel, btn("Set status", () => run(async () => { await api(`/api/opportunities/${o.id}/status`, { method: "POST", json: { status: sel.value } }); toast("Status saved (your choice is kept on re-assessment)", "ok"); reload(); }), "small")),
      h("div", { class: "row" }, manualUrl, btn("I submitted this by hand", () => run(async () => {
        const r = await api(`/api/opportunities/${o.id}/manual-submission`, { method: "POST", json: { listing_url: manualUrl.value } }); toast(r.message, "ok"); reload(); }), "small"))));
    const subs = d.submissions;
    out.push(h("div", {}, h("h4", { text: "Submissions" }), subs.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["When", "Status", "How", "Detail"].map(x => h("th", { text: x })))),
      h("tbody", {}, subs.map(s => h("tr", {}, h("td", { text: fmt(s.submitted_at) }), h("td", {}, badge(SUB, s.status)), h("td", { text: s.method }), h("td", { text: s.detail })))))) : h("span", { class: "muted small", text: "None yet." })));
    const drafts = list => list.length ? list.map(dr => h("div", {}, h("strong", { text: dr.subject }), h("pre", { class: "draft", text: dr.body }),
      btn("Copy draft", () => navigator.clipboard.writeText(`Subject: ${dr.subject}\n\n${dr.body}`).then(() => toast("Copied", "ok"), () => toast("Copy not available in this browser", "error")), "small"))) : [h("span", { class: "muted small", text: "No draft yet. Drafts are never sent automatically." })];
    const draftBox = h("div", { class: "stack" }, ...drafts(d.drafts));
    out.push(h("div", {}, h("h4", { text: "Outreach drafts" }), draftBox));
    return out;
  }
  more.addEventListener("click", () => load(false));
  let timer; $("#f-q").addEventListener("input", e => { clearTimeout(timer); timer = setTimeout(() => { Q.q = e.target.value.trim(); load(true); }, 350); });
  $("#f-category").addEventListener("change", e => { Q.category = e.target.value; load(true); });
  $("#btn-assess").addEventListener("click", () => run(async () => { await api("/api/jobs", { method: "POST", json: { kind: "evaluate", target_id: S.target.id } }); toast("Assessment started", "ok"); }));
  $("#btn-approve-all").addEventListener("click", () => run(async () => { const r = await api(`/api/opportunities/approve-all?${tq()}`, { method: "POST", json: {} }); toast(`${r.approved} opportunity(ies) approved`, "ok"); chips(); load(true); }));
  $("#btn-submit").addEventListener("click", () => run(async () => {
    if (!confirm("Submit your website details to every approved, eligible site? Sites that fail the safety checks are skipped and reported.")) return;
    await api("/api/jobs", { method: "POST", json: { kind: "submit", target_id: S.target.id } }); toast("Submissions started", "ok"); }));
  $("#btn-outreach").addEventListener("click", () => run(async () => { const r = await api(`/api/outreach/bulk?${tq()}`, { method: "POST", json: {} }); toast(`${r.count} draft(s) saved. See History > Outreach drafts.`, "ok"); }));
  $("#add-form").addEventListener("submit", ev => { ev.preventDefault(); run(async () => {
    const f = ev.target, r = await api(`/api/opportunities?${tq()}`, { method: "POST", json: { url: f.url.value, category: f.category.value, title: f.title.value, notes: f.notes.value } });
    toast(r.message, "ok"); if (r.created) { f.reset(); load(true); chips(); } }); });
  $("#import-form").addEventListener("submit", ev => { ev.preventDefault(); run(async () => {
    const r = await api(`/api/opportunities/import?${tq()}`, { method: "POST", form: new FormData(ev.target) });
    const box = $("#import-result"); box.replaceChildren(h("div", { text: `Added ${r.added}, duplicates skipped ${r.duplicates}, invalid ${r.invalid.length}.` }),
      ...r.invalid.map(i => h("div", { class: "muted", text: `${i.value}: ${i.reason}` })));
    load(true); chips(); }); });
  await chips(); await load(true);
  jobBar(() => { chips(); load(true); });
}

/* ---------- backlinks ---------- */
async function pageBacklinks() {
  if (!S.target) { $("#no-site").hidden = false; document.querySelectorAll("main > :not(#no-site)").forEach(e => e.hidden = true); return; }
  const Q = { state: "", offset: 0, limit: 50 }, list = $("#bl-list"), more = $("#more");
  async function chips() {
    const d = await api(`/api/state?${tq()}`), box = $("#bl-summary"); box.replaceChildren();
    const counts = d.stats.backlink_states; let total = 0; for (const v of Object.values(counts)) total += v;
    const mk = (key, label, n) => box.append(h("button", { class: "chip", type: "button", "aria-pressed": String(Q.state === key), onclick: () => { Q.state = key; chips(); load(true); }, text: `${label} (${n})` }));
    mk("", "All", total); for (const k of Object.keys(BL)) if (counts[k]) mk(k, BL[k][0], counts[k]);
  }
  async function load(reset) {
    if (reset) { Q.offset = 0; list.replaceChildren(); }
    const p = new URLSearchParams({ target_id: S.target.id, limit: Q.limit, offset: Q.offset }); if (Q.state) p.set("state", Q.state);
    const d = await run(() => api("/api/backlinks?" + p)); if (!d) return;
    d.backlinks.forEach(b => list.append(blItem(b))); Q.offset += d.backlinks.length; more.hidden = Q.offset >= d.total;
    if (!list.children.length) list.append(empty("No backlinks are being monitored yet. When a submission is recorded, or you add a page below, it appears here as Pending until it is checked."));
  }
  function blItem(b) {
    const wrap = h("article", { class: "item" });
    const histBox = h("div");
    const info = h("dl", { class: "kv" });
    for (const [k, v] of [["Anchor text", b.anchor_text || "n/a"], ["Link attributes", b.rel || (b.state === "live" ? "none (normal link)" : "n/a")], ["HTTP status", b.http_status ?? "n/a"],
      ["First seen live", b.first_seen ? fmt(b.first_seen) : "not seen yet"], ["Last checked", fmt(b.last_checked)], ["Next check", b.next_check ? fmt(b.next_check) : "n/a"], ["Final URL", b.final_url || "n/a"]]) info.append(h("dt", { text: k }), h("dd", { text: String(v) }));
    const badges = h("div", { class: "badges" }, badge(BL, b.state));
    if (b.disappeared) badges.append(h("span", { class: "badge bad", text: "Link disappeared" }));
    wrap.append(h("div", { class: "top" }, h("div", { class: "url" }, safeLink(b.source_url))), badges, info, b.error ? h("div", { class: "muted small", text: b.error }) : null,
      h("div", { class: "actions" },
        btn("Check now", () => run(async () => { const r = await api(`/api/backlinks/${b.id}/check`, { method: "POST", json: {} }); wrap.replaceWith(blItem(r.backlink)); chips(); toast("Checked: " + BL[r.result.state][0], "ok"); }), "small"),
        btn("History", () => run(async () => {
          if (histBox.children.length) { histBox.replaceChildren(); return; }
          const d = await api(`/api/backlinks/${b.id}/history`);
          histBox.replaceChildren(d.history.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["When", "State", "HTTP", "Note"].map(x => h("th", { text: x })))),
            h("tbody", {}, d.history.map(v => h("tr", {}, h("td", { text: fmt(v.checked_at) }), h("td", {}, badge(BL, v.state)), h("td", { text: v.http_status ?? "" }), h("td", { text: v.error })))))) : h("span", { class: "muted small", text: "No checks yet." }));
        }), "small")), histBox);
    return wrap;
  }
  more.addEventListener("click", () => load(false));
  $("#btn-verify").addEventListener("click", () => run(async () => { await api("/api/jobs", { method: "POST", json: { kind: "verify", target_id: S.target.id } }); toast("Verification started", "ok"); }));
  $("#btn-verify-all").addEventListener("click", () => run(async () => { await api("/api/jobs", { method: "POST", json: { kind: "verify", target_id: S.target.id, force: true } }); toast("Re-checking all links", "ok"); }));
  $("#bl-form").addEventListener("submit", ev => { ev.preventDefault(); run(async () => {
    const r = await api(`/api/backlinks?${tq()}`, { method: "POST", json: { source_url: ev.target.source_url.value } });
    toast(r.created ? "Added. Use Check now or Check due links." : "Already monitored.", "ok"); ev.target.reset(); chips(); load(true); }); });
  await chips(); await load(true);
  jobBar(() => { chips(); load(true); });
}

/* ---------- history ---------- */
async function pageHistory() {
  if (!S.target) { $("#no-site").hidden = false; document.querySelectorAll("main > :not(#no-site)").forEach(e => e.hidden = true); return; }
  const tabs = { jobs: "Jobs", submissions: "Submissions", verifications: "Verifications", outreach: "Outreach drafts", logs: "Activity log" };
  let cur = "jobs"; const body = $("#hist-body"), bar = $("#hist-tabs");
  const row = (title, sub, badges, extra) => h("article", { class: "item" }, h("div", { class: "top" }, h("div", { class: "title", text: title }), h("span", { class: "muted small", text: sub })), badges, extra);
  async function show() {
    bar.replaceChildren(...Object.entries(tabs).map(([k, v]) => h("button", { class: "chip", type: "button", role: "tab", "aria-pressed": String(k === cur), onclick: () => { cur = k; show(); }, text: v })));
    body.replaceChildren(h("span", { class: "muted", text: "Loading..." }));
    const items = [];
    if (cur === "jobs") { const d = await api(`/api/jobs?${tq()}&limit=50`); d.jobs.forEach(j => items.push(row(JOBS[j.kind], fmt(j.created_at), h("div", { class: "badges" }, h("span", { class: "badge " + (j.status === "failed" ? "bad" : j.status === "done" ? "ok" : "warn"), text: JSTATUS[j.status] })), h("div", { class: "muted small", text: `${j.progress} of ${j.total}. ${j.message || ""} ${j.error || ""}` })))); }
    if (cur === "submissions") { const d = await api(`/api/submissions?${tq()}&limit=100`); d.submissions.forEach(s => items.push(row(s.domain, fmt(s.submitted_at), h("div", { class: "badges" }, badge(SUB, s.status), h("span", { class: "badge muted", text: s.method === "manual" ? "Manual" : "Automatic" })), h("div", { class: "muted small", text: s.detail })))); }
    if (cur === "verifications") { const d = await api(`/api/verifications?${tq()}&limit=100`); d.verifications.forEach(v => items.push(row(v.source_url, fmt(v.checked_at), h("div", { class: "badges" }, badge(BL, v.state)), h("div", { class: "muted small", text: `${v.anchor_text ? "Anchor: " + v.anchor_text + ". " : ""}${v.rel ? "rel=" + v.rel + ". " : ""}${v.error}` })))); }
    if (cur === "outreach") { const d = await api(`/api/outreach?${tq()}`); d.drafts.forEach(x => items.push(row(x.domain, fmt(x.created_at), null, h("div", { class: "stack" }, h("strong", { text: x.subject }), h("pre", { class: "draft", text: x.body }),
      btn("Copy draft", () => navigator.clipboard.writeText(`Subject: ${x.subject}\n\n${x.body}`).then(() => toast("Copied", "ok"), () => toast("Copy not available in this browser", "error")), "small"))))); }
    if (cur === "logs") { const d = await api(`/api/logs?${tq()}&limit=150`); d.logs.forEach(l => items.push(row(l.message, fmt(l.ts), h("div", { class: "badges" }, h("span", { class: "badge " + (l.level === "ERROR" ? "bad" : l.level === "WARNING" ? "warn" : "muted"), text: l.level })), l.detail ? h("div", { class: "muted small", text: l.detail.slice(0, 600) }) : null))); }
    body.replaceChildren(...(items.length ? items : [empty("Nothing here yet.")]));
  }
  run(show);
}

/* ---------- settings ---------- */
async function pageSettings() {
  const d = await api("/api/settings"), f = $("#settings-form");
  for (const el of f.elements) {
    if (!el.name || !(el.name in d.settings)) continue;
    if (el.type === "checkbox") el.checked = !!d.settings[el.name]; else el.value = d.settings[el.name];
  }
  const p = d.providers, box = $("#providers");
  const chip = (label, on) => box.append(h("span", { class: "badge " + (on ? "ok" : "muted"), text: `${label}: ${on ? "on" : "off"}` }));
  chip("Starter list", p.seed_sources); chip("GitHub search", p.github_search); chip("GitHub token", p.github_token_present); chip("SearXNG", p.searxng);
  f.addEventListener("submit", ev => { ev.preventDefault(); run(async () => {
    const data = {};
    for (const el of f.elements) { if (!el.name) continue; data[el.name] = el.type === "checkbox" ? el.checked : el.value; }
    const r = await api("/api/settings", { method: "POST", json: data }); toast(r.note, "ok"); }); });
}

document.addEventListener("DOMContentLoaded", async () => {
  const page = document.body.dataset.page;
  try { await loadTargets(); } catch (e) { toast(e.message, "error"); return; }
  const handler = { index: pageIndex, opportunities: pageOpportunities, backlinks: pageBacklinks, history: pageHistory, settings: pageSettings }[page];
  if (handler) run(handler);
});
