// LocationSpoof UI. Plain JS + Leaflet, talks to the local FastAPI server.

const $ = (sel) => document.querySelector(sel);
const KMH_PER_MPH = 1.609344;

const state = {
  units: localStorage.getItem("units") || "mph",
  devices: [],
  udid: localStorage.getItem("udid") || null,
  places: [],
  travel: "auto",
  speedMode: "limit",
  route: null,
  playback: { state: "idle" },
  autoPrepared: new Set(),
};

// ---------- helpers ----------

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = data?.detail;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : res.statusText;
    throw new Error(msg);
  }
  return data;
}

function toast(message, kind = "info", ms = 4000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

const fmtCoord = (lat, lng) => `${lat.toFixed(5)}, ${lng.toFixed(5)}`;

function fmtSpeed(kmh) {
  if (kmh === null || kmh === undefined) return "-";
  return state.units === "mph" ? `${Math.round(kmh / KMH_PER_MPH)} mph` : `${Math.round(kmh)} km/h`;
}

function fmtDistance(m) {
  if (state.units === "mph") {
    const mi = m / 1609.344;
    return mi < 0.1 ? `${Math.round(m * 3.28084)} ft` : `${mi.toFixed(mi < 10 ? 1 : 0)} mi`;
  }
  return m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`;
}

function fmtDuration(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${s} s`;
}

const toKmh = (v) => (state.units === "mph" ? v * KMH_PER_MPH : v);
const fromKmh = (v) => (state.units === "mph" ? v / KMH_PER_MPH : v);

function currentDevice() {
  return state.devices.find((d) => d.udid === state.udid) || null;
}

function requireDevice() {
  const d = currentDevice();
  if (!d) throw new Error("Connect a device first");
  return d;
}

// ---------- map ----------

const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
const map = L.map("map", { zoomControl: false }).setView([39.5, -98.35], 4);
L.control.zoom({ position: "topright" }).addTo(map);
// Served through the local server, which caches every tile you view for offline use.
L.tileLayer("/tiles/{z}/{x}/{y}.png", {
  maxZoom: 19,
  className: "osm-tiles",
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const placesLayer = L.layerGroup().addTo(map);
const stopsLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
let spoofMarker = null;
let pinMarker = null;

function setSpoofMarker(lat, lng, { pan = false } = {}) {
  if (lat === null || lat === undefined) {
    if (spoofMarker) { spoofMarker.remove(); spoofMarker = null; }
    return;
  }
  if (!spoofMarker) {
    spoofMarker = L.marker([lat, lng], {
      icon: L.divIcon({ className: "", html: '<div class="spoof-dot"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }),
      zIndexOffset: 1000,
      keyboard: false,
    }).addTo(map);
    spoofMarker.bindTooltip("Spoofed location", { direction: "top", offset: [0, -10] });
  } else {
    spoofMarker.setLatLng([lat, lng]);
  }
  if (pan) map.panTo([lat, lng]);
}

// Popup with actions for any point (search result, map click, saved place).
function showPointPopup(point, { marker = null } = {}) {
  const { lat, lng } = point;
  const title = el("div", { class: "popup-title" }, point.title || "Dropped pin");
  const sub = el("div", { class: "popup-sub" }, point.subtitle || fmtCoord(lat, lng));
  const content = el(
    "div",
    {},
    title,
    sub,
    el(
      "div",
      { class: "popup-actions" },
      el("button", { class: "btn primary small", onclick: () => teleport(lat, lng) }, "Teleport here"),
      el("button", { class: "btn small", onclick: () => setEndpoint("start", point) }, "Route from"),
      el("button", { class: "btn small", onclick: () => setEndpoint("dest", point) }, "Route to"),
      point.placeId
        ? null
        : el("button", { class: "btn small", onclick: () => savePlace(point) }, "Save"),
    ),
  );

  if (pinMarker) pinMarker.remove();
  pinMarker = marker || L.marker([lat, lng]).addTo(map);
  pinMarker.bindPopup(content, { closeButton: true, autoPan: true }).openPopup();
  pinMarker.on("popupclose", () => {
    if (pinMarker && !marker) { pinMarker.remove(); pinMarker = null; }
  });

  // Fill in an address for bare coordinates.
  if (!point.title) {
    api(`/api/reverse?lat=${lat}&lng=${lng}`)
      .then((r) => {
        if (!r) return;
        point.title = r.title;
        point.subtitle = r.subtitle;
        point.address = r.address;
        title.textContent = r.title;
        sub.textContent = r.subtitle || fmtCoord(lat, lng);
      })
      .catch(() => {});
  }
}

map.on("click", (e) => showPointPopup({ lat: e.latlng.lat, lng: e.latlng.lng }));

// ---------- devices ----------

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const banner = $("#server-banner");
    if (!s.root || s.tunnel_error) {
      banner.innerHTML = "";
      if (!s.root) {
        banner.append(
          "Not running as administrator.",
          el("div", { class: "small" }, "iOS 17+ needs it for device tunnels. Quit and start with ", el("code", {}, "./run.sh"), "."),
        );
      } else {
        banner.append(s.tunnel_error);
      }
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  } catch {
    const banner = $("#server-banner");
    banner.textContent = "Can't reach the LocationSpoof server. Is it running?";
    banner.classList.remove("hidden");
  }
}

async function refreshDevices() {
  try {
    state.devices = await api("/api/devices");
  } catch (e) {
    state.devices = [];
  }
  if (!currentDevice() && state.devices.length) state.udid = state.devices[0].udid;
  if (state.udid) localStorage.setItem("udid", state.udid);
  renderDevices();
  maybeAutoPrepare();
}

function isReady(d) {
  return d.connected && d.paired && d.developer_mode && (!d.uses_tunnel || d.tunnel);
}

function maybeAutoPrepare() {
  const d = currentDevice();
  if (!d || !d.connected || d.busy || d.error || d.image_mounted) return;
  if (d.paired && d.developer_mode && (d.tunnel || !d.uses_tunnel) && !state.autoPrepared.has(d.udid)) {
    state.autoPrepared.add(d.udid);
    deviceAction("prepare", { quiet: true });
  }
}

async function deviceAction(action, { quiet = false } = {}) {
  const d = currentDevice();
  if (!d) return;
  try {
    await api(`/api/devices/${d.udid}/${action}`, { method: "POST" });
    if (!quiet) toast("Working on it… follow any prompts on the phone.");
    setTimeout(refreshDevices, 400);
  } catch (e) {
    toast(e.message, "error");
  }
}

function renderDevices() {
  const select = $("#device-select");
  const d = currentDevice();

  select.classList.toggle("hidden", state.devices.length < 2);
  select.innerHTML = "";
  for (const dev of state.devices) {
    const how = dev.connected ? dev.connections.join(" + ") : "disconnected";
    select.append(el("option", { value: dev.udid, ...(dev.udid === state.udid ? { selected: "" } : {}) }, `${dev.name} (${how})`));
  }

  $("#device-empty").classList.toggle("hidden", !!d);
  $("#device-info").classList.toggle("hidden", !d);
  if (!d) { renderSpoofState(null); return; }

  $("#device-name").textContent = d.name;
  $("#device-meta").textContent = [d.product_type, d.ios_version ? `iOS ${d.ios_version}` : null].filter(Boolean).join(" · ");
  const badges = $("#device-badges");
  badges.innerHTML = "";
  for (const c of d.connections) badges.append(el("span", { class: "badge" }, c));
  if (!d.connected) badges.append(el("span", { class: "badge off" }, "Disconnected"));

  const steps = $("#device-steps");
  steps.innerHTML = "";
  const step = (label, value, na = false) => {
    const cls = na || value === null || value === undefined ? "" : value ? "ok" : "bad";
    steps.append(el("li", { class: cls }, el("span", { class: "dot" }), label + (na ? " (n/a)" : "")));
  };
  step("Connected", d.connected);
  step("Trusted", d.paired);
  step("Developer Mode", d.developer_mode);
  step("Tunnel", d.uses_tunnel ? d.tunnel_state === "ok" : null, !d.uses_tunnel);

  // One plain-English line about what's going on right now.
  const status = $("#device-status");
  const h = d.health || { level: "ok", message: "" };
  status.className = `status-line ${h.level}`;
  status.innerHTML = "";
  if (h.level === "busy") status.append(el("span", { class: "spinner" }));
  status.append(h.message);
  status.classList.toggle("hidden", !h.message);

  // Recent connection events, newest first.
  const log = $("#device-log");
  const events = d.events || [];
  log.classList.toggle("hidden", events.length === 0);
  const list = $("#device-log-list");
  list.innerHTML = "";
  for (const ev of events) {
    list.append(el("li", { class: ev.level }, el("span", { class: "log-time" }, ev.time), el("span", {}, ev.msg)));
  }
  const problems = events.filter((ev) => ev.level === "error" || ev.level === "warn").length;
  $("#device-log-summary").textContent = problems ? `Connection log (${problems} issue${problems > 1 ? "s" : ""})` : "Connection log";

  const actions = $("#device-actions");
  actions.innerHTML = "";
  const btn = (label, action, cls = "btn") =>
    actions.append(el("button", { class: cls, disabled: d.busy || !d.connected ? "" : null, onclick: () => deviceAction(action) }, label));
  if (d.connected) {
    if (d.needs === "developer_mode" || d.needs === "passcode" || d.developer_mode === false) {
      btn("Show toggle in Settings", "reveal-devmode", "btn primary");
      btn("Enable automatically", "enable-devmode");
    }
    if (!d.paired) btn("Trust & prepare", "prepare", "btn primary");
    else if (d.developer_mode && !d.image_mounted) btn("Prepare device", "prepare", isReady(d) ? "btn" : "btn primary");
    if (d.paired && d.connections.includes("USB") && d.wifi_enabled === false) btn("Enable Wi-Fi", "enable-wifi");
    if (d.paired && d.wifi_enabled && d.connections.length === 1 && d.connections[0] === "USB") {
      actions.append(el("span", { class: "muted small" }, "Wi-Fi ready: unplug to go wireless."));
    }
  }

  renderSpoofState(d);
}

function renderSpoofState(d) {
  const loc = d?.location;
  const label = $("#spoof-state");
  const active = !!loc?.active;
  label.classList.toggle("active", active);
  label.textContent = active ? fmtCoord(loc.lat, loc.lng) : "Real location";
  if (active && d && !d.connected) label.textContent += " (re-applies when the iPhone reconnects)";
  $("#clear-location").disabled = !active;
  if (state.playback.state !== "playing") setSpoofMarker(active ? loc.lat : null, active ? loc.lng : null);
}

$("#device-select").addEventListener("change", (e) => {
  state.udid = e.target.value;
  localStorage.setItem("udid", state.udid);
  renderDevices();
});
$("#refresh-devices").addEventListener("click", refreshDevices);

// ---------- location ----------

async function teleport(lat, lng) {
  try {
    const d = requireDevice();
    await api("/api/location/set", { method: "POST", body: { udid: d.udid, lat, lng } });
    setSpoofMarker(lat, lng);
    map.closePopup();
    toast("Location set", "ok", 2000);
    refreshDevices();
    refreshPlayback();
  } catch (e) {
    toast(e.message, "error", 6000);
  }
}

$("#clear-location").addEventListener("click", async () => {
  try {
    const d = requireDevice();
    await api("/api/location/clear", { method: "POST", body: { udid: d.udid } });
    setSpoofMarker(null);
    toast("Real location restored", "ok");
    refreshDevices();
    refreshPlayback();
  } catch (e) {
    toast(e.message, "error");
  }
});

// Keyboard nudging
let nudgeInFlight = false;
document.addEventListener("keydown", async (e) => {
  if (e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
  const dirs = {
    ArrowUp: [1, 0], w: [1, 0], ArrowDown: [-1, 0], s: [-1, 0],
    ArrowLeft: [0, -1], a: [0, -1], ArrowRight: [0, 1], d: [0, 1],
  };
  const dir = dirs[e.key];
  const dev = currentDevice();
  if (!dir || !dev?.location?.active) return;
  e.preventDefault();
  if (nudgeInFlight) return;
  nudgeInFlight = true;
  const step = Number($("#nudge-step").value);
  try {
    const r = await api("/api/location/nudge", {
      method: "POST",
      body: { udid: dev.udid, north_m: dir[0] * step, east_m: dir[1] * step },
    });
    dev.location.lat = r.lat;
    dev.location.lng = r.lng;
    setSpoofMarker(r.lat, r.lng);
    renderSpoofState(dev);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    nudgeInFlight = false;
  }
});

// ---------- your real location ----------

let meMarker = null;

function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms))]);
}

function browserPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("unavailable"));
    navigator.geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, timeout: 10000, maximumAge: 120000 });
  });
}

// Exact location from the browser when permitted (asks only if `ask`), else approximate from the network.
async function locateMe({ ask = false } = {}) {
  let permission = "prompt";
  try { permission = (await navigator.permissions.query({ name: "geolocation" })).state; } catch { /* unsupported */ }
  if (navigator.geolocation && (permission === "granted" || (ask && permission !== "denied"))) {
    try {
      const pos = await withTimeout(browserPosition(), 15000);
      state.me = { lat: pos.coords.latitude, lng: pos.coords.longitude, precise: true, label: "Your location" };
      drawMe();
      return state.me;
    } catch { /* fall through to approximate */ }
  }
  if (!state.me || !state.me.precise) {
    try {
      const a = await api("/api/approximate-location");
      state.me = { lat: a.lat, lng: a.lng, precise: false, label: `Near ${a.label}` };
      drawMe();
    } catch { /* offline */ }
  }
  return state.me || null;
}

function drawMe() {
  if (!state.me) return;
  const { lat, lng, precise } = state.me;
  if (!meMarker) {
    meMarker = L.marker([lat, lng], {
      icon: L.divIcon({ className: "", html: '<div class="me-dot"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }),
      keyboard: false,
      interactive: true,
    }).addTo(map);
  } else {
    meMarker.setLatLng([lat, lng]);
  }
  meMarker.unbindTooltip().bindTooltip(precise ? "Your real location" : "Your approximate real location", { direction: "top", offset: [0, -8] });
}

// Where searches should look first: the area you're viewing, or your own area when zoomed out.
function searchBias() {
  const c = map.getCenter();
  if (map.getZoom() >= 9 || !state.me) return { lat: c.lat, lng: c.lng };
  return { lat: state.me.lat, lng: state.me.lng };
}

function withDistance(item) {
  if (item.distance_m === undefined || item.distance_m === null) return item;
  return { ...item, subtitle: `${fmtDistance(item.distance_m)} · ${item.subtitle}` };
}

// ---------- search (reusable autocomplete) ----------

// Wires an input + results list. `extras(q)` returns local suggestions (current location, saved places)
// shown above the geocoder results; `onPick(point)` receives { lat, lng, title, subtitle, address }.
function createAutocomplete(rootSel, { onPick, extras = () => [], showOnFocus = false }) {
  const root = $(rootSel);
  const input = root.querySelector("input");
  const list = root.querySelector(".search-results");
  let timer = null;
  let seq = 0;
  let items = [];
  let active = -1;

  const hide = () => list.classList.add("hidden");

  function render(local, remote, loading = false) {
    items = [...local, ...remote];
    active = -1;
    list.innerHTML = "";
    if (local.length) {
      local.forEach((item) =>
        list.append(
          el("li", { class: item.special ? "special" : "", onclick: () => pick(item) },
            el("div", { class: "title" }, item.title), el("div", { class: "sub" }, item.subtitle || "")),
        ),
      );
    }
    if (remote.length && local.length) list.append(el("li", { class: "group" }, "Search results"));
    remote.forEach((item) =>
      list.append(el("li", { onclick: () => pick(item) }, el("div", { class: "title" }, item.title), el("div", { class: "sub" }, item.subtitle))),
    );
    if (!remote.length) {
      list.append(el("li", { class: "group" }, loading ? "Searching… press Enter to search now" : "No suggestions. Press Enter to search"));
    }
    list.classList.remove("hidden");
  }

  let remoteShown = false;

  // Suggestions while typing (Photon).
  async function run(q) {
    const mySeq = ++seq;
    const local = extras(q);
    remoteShown = false;
    if (q.length < 2) {
      if (local.length) render(local, []);
      else hide();
      return;
    }
    render(local, [], true);
    const b = searchBias();
    try {
      const remote = (await api(`/api/suggest?q=${encodeURIComponent(q)}&lat=${b.lat}&lng=${b.lng}`)).map(withDistance);
      if (mySeq === seq) { render(local, remote); remoteShown = remote.length > 0; }
    } catch {
      if (mySeq === seq) render(local, []);
    }
  }

  // Deliberate search on Enter (Nominatim, fast).
  async function runNow(q) {
    clearTimeout(timer);
    const mySeq = ++seq;
    const local = extras(q);
    render(local, [], true);
    try {
      const b = searchBias();
      const remote = (await api(`/api/search?q=${encodeURIComponent(q)}&lat=${b.lat}&lng=${b.lng}`)).map(withDistance);
      if (mySeq !== seq) return;
      if (remote.length === 1 && !local.length) pick(remote[0]);
      else { render(local, remote); remoteShown = remote.length > 0; }
    } catch (e) {
      if (mySeq === seq) toast(e.message, "error");
    }
  }

  function pick(item) {
    if (!item) return;
    hide();
    input.value = item.title;
    input.blur();
    onPick({ ...item });
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => run(input.value.trim()), 250);
  });
  if (showOnFocus) input.addEventListener("focus", () => run(input.value.trim()));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && active < 0 && !remoteShown && input.value.trim().length >= 2) {
      e.preventDefault();
      runNow(input.value.trim());
      return;
    }
    if (list.classList.contains("hidden")) return;
    const rows = [...list.querySelectorAll("li:not(.group)")];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!rows.length) return;
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
      rows.forEach((li, i) => li.classList.toggle("active", i === active));
    } else if (e.key === "Enter") {
      e.preventDefault();
      pick(items[Math.max(active, 0)]);
    } else if (e.key === "Escape") {
      hide();
    }
  });
  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) hide();
  });

  return { input, setValue: (v) => (input.value = v) };
}

function placeSuggestions(q, limit = 5) {
  const needle = q.toLowerCase();
  return state.places
    .filter((p) => !needle || p.name.toLowerCase().includes(needle) || (p.address || "").toLowerCase().includes(needle))
    .slice(0, limit)
    .map((p) => ({ lat: p.lat, lng: p.lng, title: `★ ${p.name}`, subtitle: p.address || fmtCoord(p.lat, p.lng), address: p.address, name: p.name }));
}

function currentLocationSuggestion(q) {
  const loc = currentDevice()?.location;
  if (!loc?.active) return [];
  if (q && !"phone's location current spoofed".includes(q.toLowerCase())) return [];
  return [{ lat: loc.lat, lng: loc.lng, title: "Phone's current location", subtitle: `Spoofed · ${fmtCoord(loc.lat, loc.lng)}`, special: true }];
}

function myLocationSuggestion(q) {
  if (q && !"your location my location current".includes(q.toLowerCase())) return [];
  const me = state.me;
  return [{
    lat: me?.lat, lng: me?.lng, title: "Your location",
    subtitle: me ? (me.precise ? "Real location" : `Approximate · ${me.label.replace(/^Near /, "")}`) : "Find where you are",
    special: true, isMe: true,
  }];
}

// Start point for a route when none was chosen: where the phone is now, else where you are.
async function defaultStart({ ask = false } = {}) {
  const [phone] = currentLocationSuggestion("");
  if (phone) return phone;
  const me = await locateMe({ ask });
  return me ? { lat: me.lat, lng: me.lng, title: me.label } : null;
}

createAutocomplete("#main-search", {
  extras: (q) => (q.length >= 1 ? placeSuggestions(q, 3) : []),
  onPick: (item) => {
    map.flyTo([item.lat, item.lng], Math.max(map.getZoom(), 16), { duration: 0.8 });
    showPointPopup(item);
  },
});

// ---------- saved places ----------

async function loadPlaces() {
  try {
    state.places = await api("/api/places");
  } catch {
    state.places = [];
  }
  renderPlaces();
}

async function savePlace(point) {
  const suggested = point.title && point.title !== "Dropped pin" ? point.title : "";
  const name = prompt("Name this place", suggested || "");
  if (name === null) return;
  try {
    await api("/api/places", {
      method: "POST",
      body: { name: name || suggested || "Saved place", lat: point.lat, lng: point.lng, address: point.address || point.subtitle || null },
    });
    toast("Place saved", "ok", 2000);
    map.closePopup();
    loadPlaces();
  } catch (e) {
    toast(e.message, "error");
  }
}

function renderPlaces() {
  const list = $("#places-list");
  list.innerHTML = "";
  placesLayer.clearLayers();
  $("#places-empty").classList.toggle("hidden", state.places.length > 0);
  $("#places-count").textContent = state.places.length ? state.places.length : "";

  for (const p of state.places) {
    const point = { lat: p.lat, lng: p.lng, title: p.name, subtitle: p.address || fmtCoord(p.lat, p.lng), address: p.address, placeId: p.id };
    const marker = L.marker([p.lat, p.lng], {
      icon: L.divIcon({ className: "", html: '<div class="place-pin"></div>', iconSize: [12, 12], iconAnchor: [6, 6] }),
    }).addTo(placesLayer);
    marker.bindTooltip(p.name, { direction: "top", offset: [0, -6] });
    marker.on("click", () => showPointPopup(point, { marker }));

    list.append(
      el(
        "li",
        { class: "place" },
        el(
          "div",
          { class: "place-main", title: "Show on map", onclick: () => { map.flyTo([p.lat, p.lng], Math.max(map.getZoom(), 16), { duration: 0.6 }); showPointPopup(point, { marker }); } },
          el("div", { class: "place-name" }, p.name),
          el("div", { class: "place-addr" }, p.address || fmtCoord(p.lat, p.lng)),
        ),
        el(
          "div",
          { class: "row-actions" },
          el("button", { title: "Teleport here", onclick: () => teleport(p.lat, p.lng) }, "➤"),
          el("button", { title: "Directions to here", onclick: () => setEndpoint("dest", { ...point, name: p.name }) }, "⇢"),
          el("button", { title: "Rename", onclick: () => renamePlace(p) }, "✎"),
          el("button", { class: "del", title: "Delete", onclick: () => deletePlace(p) }, "✕"),
        ),
      ),
    );
  }
}

async function renamePlace(p) {
  const name = prompt("Rename place", p.name);
  if (!name) return;
  try {
    await api(`/api/places/${p.id}`, { method: "PATCH", body: { name } });
    loadPlaces();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function deletePlace(p) {
  if (!confirm(`Delete "${p.name}"?`)) return;
  try {
    await api(`/api/places/${p.id}`, { method: "DELETE" });
    loadPlaces();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------- route builder ----------

const FIXED_DEFAULT_KMH = { auto: 50, bicycle: 18, pedestrian: 5 };

const routeEnds = { start: null, dest: null };
const endpointLabel = (p) => (p.name ? p.name : p.title || "Dropped pin");

const startBox = createAutocomplete("#start-search", {
  showOnFocus: true,
  extras: (q) => [...currentLocationSuggestion(q), ...myLocationSuggestion(q), ...placeSuggestions(q)],
  onPick: (p) => (p.isMe ? useMyLocationAsStart() : setEndpoint("start", p)),
});
const destBox = createAutocomplete("#dest-search", {
  showOnFocus: true,
  extras: (q) => placeSuggestions(q),
  onPick: (p) => setEndpoint("dest", p),
});

// Locate button / "Your location": phone's spoofed spot if active, else your real location.
async function useMyLocationAsStart({ preferPhone = false } = {}) {
  const btn = $("#use-current-location");
  if (btn.classList.contains("loading")) return;
  btn.classList.add("loading");
  startBox.setValue("Finding your location…");
  try {
    const start = preferPhone ? await defaultStart({ ask: true }) : await locateMe({ ask: true }).then((me) => me && { lat: me.lat, lng: me.lng, title: me.label });
    if (!start) {
      startBox.setValue(routeEnds.start?.title || "");
      toast("Couldn't find your location. Check your internet connection, or type a start address.", "error", 6000);
      return;
    }
    setEndpoint("start", start);
    if (!preferPhone && state.me && !state.me.precise) {
      toast("Using your approximate location from your network. For your exact spot, allow location access for this page in your browser.", "info", 7000);
    }
  } finally {
    btn.classList.remove("loading");
  }
}

$("#use-current-location").addEventListener("click", () => useMyLocationAsStart({ preferPhone: true }));

function setEndpoint(which, point) {
  routeEnds[which] = { lat: point.lat, lng: point.lng, title: endpointLabel(point).replace(/^★ /, "") };
  (which === "start" ? startBox : destBox).setValue(routeEnds[which].title);
  map.closePopup();
  // Like a GPS: picking only a destination starts from where the phone is (or where you are).
  if (which === "dest" && !routeEnds.start) {
    defaultStart().then((start) => {
      if (start && !routeEnds.start) setEndpoint("start", start);
    });
  }
  renderEndpoints();
  scheduleRoute();
}

function renderEndpoints() {
  stopsLayer.clearLayers();
  for (const which of ["start", "dest"]) {
    const p = routeEnds[which];
    if (!p) continue;
    L.marker([p.lat, p.lng], {
      icon: L.divIcon({ className: "", html: `<div class="route-pin ${which === "start" ? "start" : "end"}"></div>`, iconSize: [16, 16], iconAnchor: [8, 8] }),
      zIndexOffset: 500,
    })
      .bindTooltip(`${which === "start" ? "Start" : "Destination"}: ${p.title}`, { direction: "top", offset: [0, -8] })
      .addTo(stopsLayer);
  }
  const any = routeEnds.start || routeEnds.dest;
  $("#clear-route").classList.toggle("hidden", !any);
  const hint = $("#route-hint");
  hint.classList.toggle("hidden", !!(routeEnds.start && routeEnds.dest));
  hint.textContent = !routeEnds.start && routeEnds.dest ? "Now pick where to start." : "Pick a start and destination to see the route.";
}

// Clearing an input clears that endpoint.
for (const [box, which] of [[startBox, "start"], [destBox, "dest"]]) {
  box.input.addEventListener("input", () => {
    if (!box.input.value.trim() && routeEnds[which]) {
      routeEnds[which] = null;
      invalidateRoute();
      renderEndpoints();
    }
  });
}

$("#swap-route").addEventListener("click", () => {
  [routeEnds.start, routeEnds.dest] = [routeEnds.dest, routeEnds.start];
  startBox.setValue(routeEnds.start?.title || "");
  destBox.setValue(routeEnds.dest?.title || "");
  renderEndpoints();
  scheduleRoute();
});

$("#clear-route").addEventListener("click", () => {
  routeEnds.start = routeEnds.dest = null;
  startBox.setValue("");
  destBox.setValue("");
  invalidateRoute();
  renderEndpoints();
});

function setupSegmented(id, onChange) {
  const root = $(id);
  root.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    root.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    onChange(b.dataset.value);
  });
}

function syncSpeedFields() {
  const driving = state.travel === "auto";
  const mode = driving ? state.speedMode : "fixed";
  $("#speed-mode-field").classList.toggle("hidden", !driving);
  $("#limit-factor-field").classList.toggle("hidden", mode !== "limit");
  $("#fixed-speed-field").classList.toggle("hidden", mode !== "fixed");
  document.querySelectorAll(".unit-label").forEach((n) => (n.textContent = state.units === "mph" ? "mph" : "km/h"));
}

function setFixedSpeedDefault() {
  $("#fixed-speed").value = Math.round(fromKmh(FIXED_DEFAULT_KMH[state.travel]));
}

setupSegmented("#travel-mode", (v) => {
  state.travel = v;
  setFixedSpeedDefault();
  syncSpeedFields();
  scheduleRoute();
});
setupSegmented("#speed-mode", (v) => {
  state.speedMode = v;
  syncSpeedFields();
  scheduleRoute();
});

$("#limit-factor").addEventListener("input", (e) => {
  $("#limit-factor-label").textContent = `${e.target.value}%`;
  scheduleRoute(500);
});
$("#fixed-speed").addEventListener("input", () => scheduleRoute(600));

const playbackActive = () => state.playback.state === "playing" || state.playback.state === "paused";

function invalidateRoute() {
  if (playbackActive()) return;
  state.route = null;
  routeLayer.clearLayers();
  $("#route-summary").classList.add("hidden");
  $("#map-legend").classList.add("hidden");
}

let routeTimer = null;
let routeSeq = 0;

function scheduleRoute(delay = 150) {
  clearTimeout(routeTimer);
  if (!routeEnds.start || !routeEnds.dest) { invalidateRoute(); return; }
  if (playbackActive()) {
    toast("Stop the current route to change it.", "info");
    return;
  }
  routeTimer = setTimeout(buildRoute, delay);
}

async function buildRoute() {
  const seq = ++routeSeq;
  const hint = $("#route-hint");
  hint.textContent = "Finding the best route…";
  hint.classList.remove("hidden");
  try {
    const mode = state.travel === "auto" ? state.speedMode : "fixed";
    const route = await api("/api/route", {
      method: "POST",
      body: {
        stops: [[routeEnds.start.lat, routeEnds.start.lng], [routeEnds.dest.lat, routeEnds.dest.lng]],
        travel: state.travel,
        speed_mode: mode,
        fixed_kmh: toKmh(Number($("#fixed-speed").value) || 5),
        limit_factor: Number($("#limit-factor").value) / 100,
      },
    });
    if (seq !== routeSeq) return;
    state.route = route;
    hint.classList.add("hidden");
    drawRoute(route);
    renderRouteSummary();
  } catch (e) {
    if (seq !== routeSeq) return;
    hint.textContent = "Couldn't find a route.";
    toast(e.message, "error", 6000);
  }
}

// Speed color bands, in round numbers of the display unit.
const SPEED_BANDS = { mph: [15, 30, 45, 60], kmh: [25, 50, 70, 100] };
const SPEED_PALETTE = ["#38bdf8", "#22c55e", "#eab308", "#f97316", "#ef4444"];
function colorFor(kmh) {
  const v = fromKmh(kmh);
  const i = SPEED_BANDS[state.units].findIndex((max) => v <= max);
  return SPEED_PALETTE[i === -1 ? SPEED_PALETTE.length - 1 : i];
}

function drawRoute(route, { fit = true } = {}) {
  routeLayer.clearLayers();
  const pts = route.points;
  const speeds = route.segment_speeds_kmh;

  // Casing underneath, then colored runs on top (merge consecutive segments with the same color).
  L.polyline(pts, { color: dark ? "#000" : "#fff", weight: 9, opacity: 0.8 }).addTo(routeLayer);
  let start = 0;
  for (let i = 1; i <= speeds.length; i++) {
    if (i === speeds.length || colorFor(speeds[i]) !== colorFor(speeds[start])) {
      const run = pts.slice(start, i + 1);
      const speed = speeds[start];
      L.polyline(run, { color: colorFor(speed), weight: 5, opacity: 0.95 })
        .bindTooltip(fmtSpeed(speed), { sticky: true })
        .addTo(routeLayer);
      start = i;
    }
  }
  if (fit) map.fitBounds(L.latLngBounds(pts), { padding: [40, 40] });
  renderLegend();
}

function renderLegend() {
  const legend = $("#map-legend");
  legend.innerHTML = "";
  const unit = state.units === "mph" ? "mph" : "km/h";
  const bands = SPEED_BANDS[state.units];
  const labels = [
    `≤ ${bands[0]} ${unit}`,
    ...bands.slice(1).map((max, i) => `${bands[i]}–${max} ${unit}`),
    `> ${bands[bands.length - 1]} ${unit}`,
  ];
  labels.forEach((label, i) =>
    legend.append(el("div", { class: "legend-row" }, el("span", { class: "legend-swatch", style: `background:${SPEED_PALETTE[i]}` }), label)),
  );
  legend.classList.toggle("hidden", !state.route);
}

function renderRouteSummary() {
  const r = state.route;
  $("#route-summary").classList.toggle("hidden", !r);
  if (!r) return;
  $("#route-distance").textContent = fmtDistance(r.distance_m);
  $("#route-duration").textContent = fmtDuration(r.duration_s);
  const driving = state.travel === "auto";
  $("#route-coverage").textContent = driving ? `${Math.round(r.limit_coverage * 100)}%` : "n/a";
  const saveBtn = $("#save-route-btn");
  saveBtn.textContent = r.savedName ? `Saved as "${r.savedName}"` : "Save route";
  saveBtn.classList.toggle("saved", !!r.savedName);
  saveBtn.disabled = !!r.savedName;
  $("#save-route-form").classList.add("hidden");
  saveBtn.classList.remove("hidden");
  renderPlayback();
}

// ---------- saved routes ----------

state.savedRoutes = [];

function routeSettings() {
  return {
    travel: state.travel,
    speed_mode: state.speedMode,
    limit_percent: Number($("#limit-factor").value),
    fixed_kmh: toKmh(Number($("#fixed-speed").value) || 5),
  };
}

function applyRouteSettings(settings = {}) {
  if (settings.travel) state.travel = settings.travel;
  if (settings.speed_mode) state.speedMode = settings.speed_mode;
  document.querySelectorAll("#travel-mode button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.travel));
  document.querySelectorAll("#speed-mode button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.speedMode));
  if (settings.limit_percent) {
    $("#limit-factor").value = settings.limit_percent;
    $("#limit-factor-label").textContent = `${settings.limit_percent}%`;
  }
  if (settings.fixed_kmh) $("#fixed-speed").value = Math.round(fromKmh(settings.fixed_kmh));
  syncSpeedFields();
}

async function loadSavedRoutes() {
  try {
    state.savedRoutes = await api("/api/saved-routes");
  } catch {
    state.savedRoutes = [];
  }
  renderSavedRoutes();
}

function renderSavedRoutes() {
  const list = $("#saved-routes-list");
  list.innerHTML = "";
  $("#saved-routes-empty").classList.toggle("hidden", state.savedRoutes.length > 0);
  $("#saved-routes-count").textContent = state.savedRoutes.length || "";
  for (const r of state.savedRoutes) {
    const meta = [fmtDistance(r.distance_m || 0), fmtDuration(r.duration_s || 0), { auto: "Drive", bicycle: "Bike", pedestrian: "Walk" }[r.settings?.travel] || ""]
      .filter(Boolean)
      .join(" · ");
    list.append(
      el(
        "li",
        { class: "place" },
        el(
          "div",
          { class: "place-main", title: "Load this route", onclick: () => loadSavedRoute(r.id) },
          el("div", { class: "place-name" }, r.name),
          el("div", { class: "place-addr" }, `${r.start?.title || "?"} → ${r.dest?.title || "?"}`),
          el("div", { class: "place-meta" }, meta),
        ),
        el(
          "div",
          { class: "row-actions" },
          el("button", { title: "Load", onclick: () => loadSavedRoute(r.id) }, "↗"),
          el("button", { title: "Rename", onclick: () => renameSavedRoute(r) }, "✎"),
          el("button", { class: "del", title: "Delete", onclick: () => deleteSavedRoute(r) }, "✕"),
        ),
      ),
    );
  }
}

async function loadSavedRoute(id) {
  if (playbackActive()) {
    toast("Stop the current route first.", "info");
    return;
  }
  try {
    // Everything comes from disk on the server: no internet needed.
    const r = await api(`/api/saved-routes/${id}/load`, { method: "POST" });
    clearTimeout(routeTimer);
    routeSeq++;
    applyRouteSettings(r.settings);
    routeEnds.start = r.start;
    routeEnds.dest = r.dest;
    startBox.setValue(r.start?.title || "");
    destBox.setValue(r.dest?.title || "");
    renderEndpoints();
    state.route = { ...r, savedName: r.name };
    $("#route-hint").classList.add("hidden");
    drawRoute(state.route);
    renderRouteSummary();
    toast(`Loaded "${r.name}"`, "ok", 2000);
  } catch (e) {
    toast(e.message, "error");
  }
}

$("#save-route-btn").addEventListener("click", () => {
  if (!state.route) return;
  const suggested = routeEnds.start && routeEnds.dest ? `${routeEnds.start.title} → ${routeEnds.dest.title}` : "";
  $("#save-route-name").value = suggested;
  $("#save-route-btn").classList.add("hidden");
  $("#save-route-form").classList.remove("hidden");
  $("#save-route-name").select();
});

$("#save-route-cancel").addEventListener("click", () => {
  $("#save-route-form").classList.add("hidden");
  $("#save-route-btn").classList.remove("hidden");
});

$("#save-route-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.route) return;
  const name = $("#save-route-name").value.trim() || "Saved route";
  try {
    const saved = await api("/api/saved-routes", {
      method: "POST",
      body: { route_id: state.route.id, name, start: routeEnds.start, dest: routeEnds.dest, settings: routeSettings() },
    });
    state.route.id = saved.id;
    state.route.savedName = saved.name;
    renderRouteSummary();
    loadSavedRoutes();
    toast("Route saved. It will load and play without internet.", "ok");
  } catch (err) {
    toast(err.message, "error");
  }
});

async function renameSavedRoute(r) {
  const name = prompt("Rename route", r.name);
  if (!name) return;
  try {
    await api(`/api/saved-routes/${r.id}`, { method: "PATCH", body: { name } });
    if (state.route?.id === r.id) { state.route.savedName = name; renderRouteSummary(); }
    loadSavedRoutes();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function deleteSavedRoute(r) {
  if (!confirm(`Delete the saved route "${r.name}"?`)) return;
  try {
    await api(`/api/saved-routes/${r.id}`, { method: "DELETE" });
    if (state.route?.id === r.id) { delete state.route.savedName; renderRouteSummary(); }
    loadSavedRoutes();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------- playback ----------

let playbackTimer = null;

async function refreshPlayback() {
  try {
    state.playback = await api("/api/playback");
  } catch {
    return;
  }
  renderPlayback();
  const active = state.playback.state === "playing" || state.playback.state === "paused";
  clearTimeout(playbackTimer);
  if (active) playbackTimer = setTimeout(refreshPlayback, 1000);
}

function renderPlayback() {
  const p = state.playback;
  const playing = p.state === "playing";
  const paused = p.state === "paused";
  $("#play").classList.toggle("hidden", playing);
  $("#play").textContent = paused ? "▶ Resume" : "▶ Play";
  $("#pause").classList.toggle("hidden", !playing);
  $("#stop").disabled = !(playing || paused);
  $("#progress-bar").style.width = `${Math.round((p.progress || 0) * 100)}%`;

  if (playing || paused) {
    $("#live-speed").textContent = playing ? fmtSpeed(p.speed_kmh) : "Paused";
    $("#live-remaining").textContent = `${fmtDistance(Math.max(p.total_m - p.traveled_m, 0))} · ${fmtDuration(p.remaining_s)} left · target ${fmtSpeed(p.target_kmh)}`;
    if (p.position) setSpoofMarker(p.position[0], p.position[1]);
  } else if (p.state === "finished") {
    $("#live-speed").textContent = "Arrived";
    $("#live-remaining").textContent = "";
  } else {
    $("#live-speed").textContent = "-";
    $("#live-remaining").textContent = "";
  }

  const err = $("#playback-error");
  err.textContent = p.error || "";
  err.classList.toggle("hidden", !p.error);
}

$("#play").addEventListener("click", async () => {
  try {
    if (state.playback.state === "paused") {
      await api("/api/playback/resume", { method: "POST" });
    } else {
      const d = requireDevice();
      if (!state.route) throw new Error("Build a route first");
      await api("/api/playback/start", {
        method: "POST",
        body: {
          udid: d.udid,
          route_id: state.route.id,
          loop: $("#loop").checked,
          speed_scale: Number($("#speed-scale").value),
        },
      });
    }
    refreshPlayback();
  } catch (e) {
    toast(e.message, "error", 6000);
  }
});

$("#pause").addEventListener("click", async () => {
  await api("/api/playback/pause", { method: "POST" }).catch((e) => toast(e.message, "error"));
  refreshPlayback();
});

$("#stop").addEventListener("click", async () => {
  await api("/api/playback/stop", { method: "POST" }).catch((e) => toast(e.message, "error"));
  refreshPlayback();
  refreshDevices();
});

$("#speed-scale").addEventListener("input", (e) => {
  const v = Number(e.target.value);
  $("#speed-scale-label").textContent = `${v}×`;
  if (state.playback.state === "playing" || state.playback.state === "paused") {
    api("/api/playback/speed", { method: "POST", body: { speed_scale: v } }).catch(() => {});
  }
});

// ---------- units ----------

function applyUnits() {
  $("#units-toggle").textContent = state.units;
  syncSpeedFields();
  renderRouteSummary();
  if (state.route) drawRoute(state.route, { fit: false });
  renderPlayback();
}

$("#units-toggle").addEventListener("click", () => {
  const kmh = toKmh(Number($("#fixed-speed").value) || 0);
  state.units = state.units === "mph" ? "kmh" : "mph";
  localStorage.setItem("units", state.units);
  $("#fixed-speed").value = Math.round(fromKmh(kmh));
  applyUnits();
});

// ---------- boot ----------

setFixedSpeedDefault();
applyUnits();
renderEndpoints();
refreshStatus();
loadPlaces();
loadSavedRoutes();
refreshPlayback();
setInterval(refreshDevices, 3000);
setInterval(refreshStatus, 10000);

// Start the map where it matters: the phone's spoofed spot, otherwise where you are.
let userMovedMap = false;
for (const ev of ["mousedown", "wheel", "touchstart"]) {
  map.getContainer().addEventListener(ev, () => (userMovedMap = true), { once: true, passive: true });
}
(async () => {
  await refreshDevices();
  const loc = currentDevice()?.location;
  if (loc?.active) {
    map.setView([loc.lat, loc.lng], 14);
    userMovedMap = true;
  }
  const me = await locateMe();
  if (me && !userMovedMap && !state.route) map.setView([me.lat, me.lng], me.precise ? 14 : 11);
})();
