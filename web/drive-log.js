// Drive log viewer: reads the parsed log from /api/drive-log/history and renders it by route.

const $ = (sel) => document.querySelector(sel);
const REFRESH_MS = 5000;
const COORD_RE = /(-?\d{1,2}\.\d{3,}), (-?\d{1,3}\.\d{3,})/g;
const units = localStorage.getItem("units") === "kmh" ? "kmh" : "mph"; // same setting as the app

const state = { data: null, filter: "all", query: "", signature: "" };

const ICONS = {
  disconnected: "!", real_location: "!", computer_slept: "z", reconnected: "✓", spoof_restored: "✓",
  arrived: "⚑", tunnel_rebuilt: "↻", device_check: "?", unresolved: "…",
};
const PROBLEM_KINDS = new Set(["disconnected", "real_location", "computer_slept", "tunnel_rebuilt", "unresolved", "device_check"]);

// ---------- formatting ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

function fmtDuration(s) {
  if (s === null || s === undefined) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return sec ? `${m}m ${sec}s` : `${m}m`;
  return `${sec}s`;
}

function fmtDistance(mi) {
  if (mi === null || mi === undefined) return "";
  return units === "kmh" ? `${(mi * 1.609344).toFixed(1)} km` : `${mi.toFixed(1)} mi`;
}

const toDate = (iso) => (iso ? new Date(iso) : null);

function fmtDay(iso) {
  const d = toDate(iso);
  if (!d) return "Earlier entries";
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  const same = (a, b) => a.toDateString() === b.toDateString();
  const day = same(d, today) ? "Today" : same(d, yesterday) ? "Yesterday" : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  return `${day} · ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

const fmtClock = (iso) => (toDate(iso)?.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }) ?? "");

function fmtOffset(iso, startIso) {
  const a = toDate(iso);
  const b = toDate(startIso);
  if (!a || !b || a < b) return "";
  return `+${fmtDuration((a - b) / 1000)}`;
}

// ---------- message → readable summary ----------

const pick = (msg, re) => msg.match(re)?.[1];
const firstCoord = (msg) => msg.match(/(-?\d{1,2}\.\d{3,}, -?\d{1,3}\.\d{3,})/)?.[1];
const phase = (msg) => pick(msg, /\((\d+% of the route|parked at the destination|route start)\)/);

function summarize(e) {
  const m = e.message;
  const chips = [];
  let text = "";
  switch (e.kind) {
    case "disconnected": {
      const where = pick(m, /Spoofed position at the time: (-?\d+\.\d+, -?\d+\.\d+)/);
      text = `Lost the iPhone at ${where ?? "unknown"}${phase(m) ? ` (${phase(m)})` : ""}.`;
      break;
    }
    case "real_location": {
      const cause = pick(m, /Cause: (.+?)\. Details/) ?? "unknown";
      const was = pick(m, /Spoofed position was (-?\d+\.\d+, -?\d+\.\d+)/);
      text = `Cause: ${cause}. The phone likely showed its real location${was ? `; the route was at ${was}` : ""}.`;
      break;
    }
    case "reconnected": {
      const via = pick(m, /via (.+?) after/);
      text = `Back${via ? ` over ${via}` : ""}${pick(m, /Route position now: (-?\d+\.\d+, -?\d+\.\d+)/) ? ` at ${pick(m, /Route position now: (-?\d+\.\d+, -?\d+\.\d+)/)}` : ""}.`;
      if (e.duration_s !== null) chips.push(`away ${fmtDuration(e.duration_s)}`);
      break;
    }
    case "spoof_restored":
      text = `Spoofed location re-applied${firstCoord(m) ? ` at ${firstCoord(m)}` : ""}.`;
      if (e.duration_s !== null) chips.push(`down ${fmtDuration(e.duration_s)}`);
      break;
    case "computer_slept":
      text = "The app was paused, so the phone was most likely at its real location meanwhile. Keep computer awake prevents idle sleep.";
      if (e.duration_s !== null) chips.push(`asleep ${fmtDuration(e.duration_s)}`);
      break;
    case "tunnel_rebuilt":
      const why = pick(m, /responding \((.+)\) and is being/)?.replace(/\.$/, "");
      text = `The developer tunnel stopped responding${why ? `: ${why}` : ""}. Rebuilding it.`;
      break;
    case "arrived": {
      const inc = pick(m, /, (no incidents|\d+ incidents?)\./);
      text = `Route completed${inc ? ` with ${inc}` : ""}. Holding at ${firstCoord(m) ?? "the destination"}.`;
      if (e.duration_s !== null) chips.push(`took ${fmtDuration(e.duration_s)}`);
      break;
    }
    default:
      text = m;
  }
  return { text, chips };
}

// Text with coordinates turned into map links and search terms highlighted.
function richText(text, query) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const match of text.matchAll(COORD_RE)) {
    frag.append(highlight(text.slice(last, match.index), query));
    const [lat, lng] = [match[1], match[2]];
    frag.append(el("a", {
      href: `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lng}#map=16/${lat}/${lng}`,
      target: "_blank", rel: "noopener", title: "Open on a map",
    }, `${lat}, ${lng}`));
    last = match.index + match[0].length;
  }
  frag.append(highlight(text.slice(last), query));
  return frag;
}

function highlight(text, query) {
  const frag = document.createDocumentFragment();
  if (!query) {
    frag.append(text);
    return frag;
  }
  const lower = text.toLowerCase();
  let i = 0;
  for (;;) {
    const hit = lower.indexOf(query, i);
    if (hit === -1) break;
    frag.append(text.slice(i, hit), el("mark", {}, text.slice(hit, hit + query.length)));
    i = hit + query.length;
  }
  frag.append(text.slice(i));
  return frag;
}

// ---------- rendering ----------

function visibleEntries(route) {
  return route.entries.filter((e) => {
    if (state.filter === "problems" && !PROBLEM_KINDS.has(e.kind)) return false;
    if (state.filter === "problems" && e.kind === "device_check" && e.level === "ok") return false;
    if (state.query && !`${e.label} ${e.message}`.toLowerCase().includes(state.query)) return false;
    return true;
  });
}

function routeBadge(r) {
  if (r.unresolved) return el("span", { class: "badge error" }, "Stopped with a problem open");
  if (r.arrived && r.incidents === 0) return el("span", { class: "badge ok" }, "✓ Arrived · no incidents");
  if (r.arrived) return el("span", { class: "badge warn" }, `Arrived · ${r.incidents} incident${r.incidents === 1 ? "" : "s"}`);
  if (r.incidents) return el("span", { class: "badge error" }, `${r.incidents} incident${r.incidents === 1 ? "" : "s"} · not arrived`);
  return el("span", { class: "badge neutral" }, "Not arrived");
}

function renderEntry(e, route) {
  const { text, chips } = summarize(e);
  return el(
    "li",
    { class: `entry ${e.level}` },
    el("div", { class: "entry-time" }, fmtClock(e.time_iso), el("span", { class: "offset" }, fmtOffset(e.time_iso, route.started_iso))),
    el("div", { class: "entry-rail" }, el("span", { class: "entry-dot", "aria-hidden": "true" }, ICONS[e.kind] ?? "•")),
    el(
      "div",
      { class: "entry-body" },
      el("div", { class: "entry-label" }, highlight(e.label, state.query), chips.map((c) => el("span", { class: "chip" }, c))),
      el("div", { class: "entry-msg" }, richText(text, state.query)),
      text !== e.message
        ? el("details", { class: "entry-full" }, el("summary", {}, "Full entry"), el("div", { class: "entry-msg muted" }, richText(e.message, state.query)))
        : null,
    ),
  );
}

function renderRoute(r) {
  const entries = visibleEntries(r);
  const meta = [];
  if (r.real_location) meta.push(el("span", {}, "Real location: ", el("b", {}, richText(r.real_location, ""))));
  for (const d of r.details) {
    const [k, v] = d.split(/:\s*/, 2);
    meta.push(el("span", {}, `${k}: `, el("b", {}, v ?? "")));
  }
  if (r.interrupted_s) meta.push(el("span", {}, "Spoof interrupted: ", el("b", {}, fmtDuration(r.interrupted_s))));

  const sub = [fmtDistance(r.distance_mi), r.trip_s ? `took ${fmtDuration(r.trip_s)}` : null, `${r.entries.length} entr${r.entries.length === 1 ? "y" : "ies"}`]
    .filter(Boolean)
    .join(" · ");

  return el(
    "article",
    { class: "route" },
    el(
      "div",
      { class: "route-head" },
      el("div", {}, el("h2", { class: "route-title" }, fmtDay(r.started_iso)), el("div", { class: "route-sub" }, sub), el("div", { class: "route-meta" }, meta)),
      routeBadge(r),
    ),
    entries.length
      ? el("ol", { class: "timeline" }, entries.map((e) => renderEntry(e, r)))
      : el("div", { class: "route-quiet" }, state.filter === "problems" ? "No problems on this route." : "No entries match."),
  );
}

function render() {
  const d = state.data;
  if (!d) return;
  const t = d.totals;
  $("#t-routes").textContent = t.routes;
  $("#t-incidents").textContent = t.incidents;
  $("#t-incidents").parentElement.classList.toggle("bad", t.incidents > 0);
  $("#t-down").textContent = t.interrupted_s ? fmtDuration(t.interrupted_s) : "0s";
  $("#t-down").parentElement.classList.toggle("bad", t.interrupted_s > 0);
  $("#t-arrived").textContent = t.routes ? `${t.arrived} of ${t.routes}` : "0";
  $("#t-arrived").parentElement.classList.toggle("good", t.routes > 0 && t.arrived === t.routes);

  const container = $("#routes");
  container.innerHTML = "";
  const filtering = state.filter !== "all" || state.query;
  const routes = filtering ? d.routes.filter((r) => visibleEntries(r).length) : d.routes;
  routes.forEach((r) => container.append(renderRoute(r)));

  $("#empty").classList.toggle("hidden", d.routes.length > 0);
  $("#no-match").classList.toggle("hidden", !(d.routes.length > 0 && routes.length === 0));

  const live = $("#live");
  const on = !!d.logging?.enabled;
  live.classList.toggle("on", on);
  $("#live-text").textContent = on ? "Logging · live" : "Logging off";
  live.title = on ? "Logging is on. This page refreshes on its own." : "Turn on 'Start disconnect log' on the Route card to record a drive.";
  const updated = d.updated ? new Date(d.updated * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
  $("#path").textContent = `Saved at ${d.path} · last written ${updated}`;
}

async function load() {
  try {
    const res = await fetch("/api/drive-log/history", { cache: "no-store" });
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    // Only re-render when something changed, so open "Full entry" panels and scroll stay put.
    const signature = `${data.updated}|${data.logging?.enabled}|${data.routes.length}`;
    if (signature !== state.signature) {
      state.signature = signature;
      state.data = data;
      render();
    }
  } catch {
    $("#live").classList.remove("on");
    $("#live-text").textContent = "Can't reach the app";
  }
}

// ---------- controls ----------

$("#filter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.filter = b.dataset.value;
  document.querySelectorAll("#filter button").forEach((x) => {
    x.classList.toggle("active", x === b);
    x.setAttribute("aria-checked", String(x === b));
  });
  render();
});

let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = e.target.value.trim().toLowerCase();
    render();
  }, 150);
});

load();
setInterval(load, REFRESH_MS);
