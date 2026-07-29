/*
 * Camp Radar dashboard.
 *
 * Vanilla ES modules, no build step, no framework — the page loads one JSON
 * file and renders it.
 *
 * PRIVACY INVARIANT: child profiles live in localStorage and never leave the
 * device. There is no server to send them to, no analytics, and no fetch call
 * anywhere in this file other than the one that loads sessions.json. Anyone
 * modifying this file should keep it that way; see docs/privacy.md.
 */

const STORAGE_KEY = "camp-radar.kids.v1";
const DATA_URL = "assets/data/sessions.json";

/** @type {{sessions: any[], breaks: any[], providers: Object, generated_at: string}} */
let dataset = { sessions: [], breaks: [], providers: {}, generated_at: null };

/** Kid profiles: [{id, name, age}]. Local to this browser. */
let kids = [];

/** Break name currently isolated in the gap chart, or null for "all". */
let activeBreak = null;

// ---------------------------------------------------------------- storage

function loadKids() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    // Private browsing, disabled storage, or corrupted JSON. Losing profiles
    // is a minor annoyance; a thrown error would blank the whole page.
    return [];
  }
}

function saveKids() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(kids));
  } catch {
    /* Nothing useful to do — the dashboard still works unpersonalised. */
  }
}

// ------------------------------------------------------------------ dates

const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Parse an ISO date as *local* midnight, not UTC.
 *  `new Date("2027-04-05")` is UTC and renders as April 4th in Atlanta. */
function parseDay(iso) {
  const m = ISO_DAY.exec(iso);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function formatRange(startIso, endIso) {
  const start = parseDay(startIso);
  const end = parseDay(endIso);
  if (!start || !end) return startIso;
  const opts = { month: "short", day: "numeric" };
  const left = start.toLocaleDateString("en-US", opts);
  if (startIso === endIso) return left;
  const right = end.toLocaleDateString("en-US", opts);
  return `${left} – ${right}`;
}

/** Weekdays in a break. Weekends are excluded: nobody needs camp on Saturday. */
function weekdaysBetween(startIso, endIso) {
  const days = [];
  const cursor = parseDay(startIso);
  const end = parseDay(endIso);
  while (cursor && end && cursor <= end) {
    const dow = cursor.getDay();
    if (dow !== 0 && dow !== 6) days.push(new Date(cursor));
    cursor.setDate(cursor.getDate() + 1);
  }
  return days;
}

function toIso(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// --------------------------------------------------------------- filtering

/** Sessions matching the current break selection and kid ages. */
function visibleSessions() {
  return dataset.sessions.filter((s) => {
    if (activeBreak) {
      const brk = dataset.breaks.find((b) => b.name === activeBreak);
      if (brk && (s.end_date < brk.start || s.start_date > brk.end)) return false;
    }
    if (kids.length > 0) {
      // Unstated bounds are permissive, matching the Python model. Hiding a
      // camp because ages weren't published is the expensive mistake.
      const anyKidFits = kids.some(
        (k) =>
          (s.min_age === null || k.age >= s.min_age) &&
          (s.max_age === null || k.age <= s.max_age)
      );
      if (!anyKidFits) return false;
    }
    return true;
  });
}

// ---------------------------------------------------------------- render

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderGapChart() {
  const track = document.getElementById("gapchart-track");
  track.replaceChildren();

  if (dataset.breaks.length === 0) {
    track.append(el("div", "gapchart__term"));
    return;
  }

  dataset.breaks.forEach((brk, index) => {
    if (index > 0) track.append(el("div", "gapchart__term"));

    const button = el("button", "gapchart__break");
    button.type = "button";
    button.setAttribute("aria-current", String(activeBreak === brk.name));
    button.addEventListener("click", () => {
      activeBreak = activeBreak === brk.name ? null : brk.name;
      render();
    });

    button.append(el("span", "gapchart__label", brk.name));
    button.append(el("span", "gapchart__dates", formatRange(brk.start, brk.end)));

    // One tick per weekday. Pink = no camp found covering that day.
    const strip = el("div", "daystrip");
    const days = weekdaysBetween(brk.start, brk.end);
    const inBreak = dataset.sessions.filter(
      (s) => !(s.end_date < brk.start || s.start_date > brk.end)
    );

    let coveredCount = 0;
    days.forEach((day) => {
      const iso = toIso(day);
      const covered = inBreak.some((s) => s.start_date <= iso && iso <= s.end_date);
      if (covered) coveredCount += 1;
      const tick = el("div", `daystrip__day${covered ? " daystrip__day--covered" : ""}`);
      tick.title = `${iso}: ${covered ? "camps found" : "nothing found"}`;
      strip.append(tick);
    });
    button.append(strip);

    const gaps = days.length - coveredCount;
    button.append(
      el(
        "span",
        "gapchart__count",
        gaps === 0 ? `${inBreak.length} options` : `${gaps} day${gaps === 1 ? "" : "s"} uncovered`
      )
    );

    track.append(button);
  });

  track.append(el("div", "gapchart__term"));
}

function renderKids() {
  const list = document.getElementById("kid-list");
  list.replaceChildren();

  kids.forEach((kid) => {
    const chip = el("span", "chip");
    chip.append(document.createTextNode(kid.name));
    chip.append(el("span", "chip__age", `${kid.age}y`));

    const remove = el("button", "chip__remove", "\u00d7");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${kid.name}`);
    remove.addEventListener("click", () => {
      kids = kids.filter((k) => k.id !== kid.id);
      saveKids();
      render();
    });
    chip.append(remove);
    list.append(chip);
  });
}

function sessionCard(session) {
  const isNew = session.is_new === true;
  const isOpen = session.registration_status === "open";

  const card = el(
    "article",
    `session${isNew ? " session--new" : ""}${!isNew && isOpen ? " session--open" : ""}`
  );

  const left = document.createElement("div");
  const heading = el("h3", "session__title");
  if (session.url) {
    const link = el("a", null, session.title);
    link.href = session.url;
    link.rel = "noopener";
    link.target = "_blank";
    heading.append(link);
  } else {
    heading.textContent = session.title;
  }
  left.append(heading);

  const provider = dataset.providers[session.provider_slug];
  left.append(
    el(
      "div",
      "session__provider",
      provider ? `${provider.name}${provider.locality ? ` · ${provider.locality}` : ""}`
               : session.provider_slug
    )
  );
  card.append(left);

  card.append(el("div", "session__when", formatRange(session.start_date, session.end_date)));

  const facts = el("div", "session__facts");
  if (isNew) facts.append(el("span", "fact fact--new", "new this week"));
  if (isOpen) facts.append(el("span", "fact fact--open", "registration open"));
  if (session.registration_status === "full") facts.append(el("span", "fact", "full"));
  if (session.registration_status === "unknown") {
    facts.append(el("span", "fact fact--unknown", "status not published"));
  }
  facts.append(
    el(
      "span",
      "fact",
      session.min_age !== null && session.max_age !== null
        ? `ages ${session.min_age}–${session.max_age}`
        : "ages not stated"
    )
  );
  if (session.price_usd !== null) {
    facts.append(el("span", "fact", `$${Math.round(session.price_usd).toLocaleString()}`));
  }
  card.append(facts);

  return card;
}

function renderSessions() {
  const container = document.getElementById("session-list");
  const counter = document.getElementById("session-count");
  container.replaceChildren();

  const sessions = visibleSessions();
  counter.textContent = `${sessions.length} shown`;

  if (sessions.length === 0) {
    container.append(emptyState());
    return;
  }

  sessions.forEach((s) => container.append(sessionCard(s)));
}

/*
 * Empty states are the most important copy on this page, because "no camps"
 * has at least four causes and they need completely different responses. A
 * single generic message would send someone hunting for a bug when they
 * simply haven't enabled a source yet.
 */
function emptyState() {
  const run = dataset.run || {};
  const ok = run.sources_ok || [];
  const failed = run.sources_failed || [];
  const box = el("div", "empty");

  // 1. Nothing configured. By far the most likely state on a fresh install.
  if (ok.length === 0 && failed.length === 0) {
    box.append(el("strong", null, "No sources are enabled yet"));
    box.append(
      document.createTextNode(
        "Every source in config/sources.yaml ships disabled with a placeholder URL. " +
          "Run `campradar probe <url>` on a provider's camp page, then set enabled: true. " +
          "See docs/adding-a-source.md."
      )
    );
    return box;
  }

  // 2. Sources ran but all of them broke.
  if (ok.length === 0 && failed.length > 0) {
    box.append(el("strong", null, `All ${failed.length} source(s) failed`));
    box.append(document.createTextNode(`Failed: ${failed.join(", ")}. Check the Actions log.`));
    return box;
  }

  // 3. Sources worked but genuinely returned nothing.
  if (dataset.sessions.length === 0) {
    box.append(el("strong", null, "Sources ran, but found no camps"));
    box.append(
      document.createTextNode(
        `${ok.length} source(s) responded and returned zero sessions. ` +
          "The listing page may have changed layout, or registration may not have opened yet."
      )
    );
    return box;
  }

  // 4. Data exists; the filters are just too narrow.
  box.append(el("strong", null, "Nothing matches your filters"));
  box.append(
    document.createTextNode(
      `${dataset.sessions.length} camps are loaded. Clear the selected break, or remove a kid.`
    )
  );
  return box;
}

/** How long ago, in the coarsest unit that still says something useful. */
function humanAge(milliseconds) {
  const minutes = Math.round(milliseconds / 60000);
  if (minutes < 2) return "just now";
  if (minutes < 60) return `${minutes} minutes ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  if (days < 31) return `${days} day${days === 1 ? "" : "s"} ago`;
  const months = Math.round(days / 30);
  return `${months} month${months === 1 ? "" : "s"} ago`;
}

/**
 * "Last updated" in the masthead.
 *
 * Shows the exact local time *and* how long ago that was. The relative form is
 * what answers the question people actually have — a date alone reads as fresh
 * long after it stopped being. Past STALE_AFTER_DAYS the line is marked so an
 * abandoned dashboard admits it rather than presenting old camps as current.
 *
 * The timestamp is written by the pipeline on every refresh, so it moves on
 * every `make update` whether or not any camp changed. That distinction
 * matters: unchanged data is not the same as unchecked data.
 */
const STALE_AFTER_DAYS = 14;

function renderUpdatedAt() {
  const node = document.getElementById("updated-at");
  if (!dataset.generated_at) {
    node.textContent = "never updated";
    node.classList.add("is-stale");
    return;
  }

  const when = new Date(dataset.generated_at);
  if (Number.isNaN(when.getTime())) {
    node.textContent = "update time unreadable";
    node.classList.add("is-stale");
    return;
  }

  const elapsed = Date.now() - when.getTime();
  const stamp = when.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

  node.textContent = `updated ${stamp} · ${humanAge(elapsed)}`;
  node.title = when.toString();
  node.classList.toggle("is-stale", elapsed > STALE_AFTER_DAYS * 86400000);
}

/** Status line in the masthead: what the last run actually managed to do. */
function renderRunStatus() {
  const node = document.getElementById("run-status");
  const run = dataset.run;
  if (!run) {
    node.textContent = "";
    return;
  }
  const ok = (run.sources_ok || []).length;
  const failed = (run.sources_failed || []).length;

  if (ok === 0 && failed === 0) {
    node.textContent = "no sources enabled";
    return;
  }
  node.textContent =
    `${ok} source${ok === 1 ? "" : "s"} ok` + (failed > 0 ? `, ${failed} failed` : "");
}

function render() {
  renderRunStatus();
  renderGapChart();
  renderKids();
  renderSessions();
}

// ------------------------------------------------------- ICS export (client)

/*
 * Mirrors src/campradar/icsgen.py so a file downloaded here matches one made
 * by `campradar export`. Kept in sync by hand; both are short and the rules
 * (exclusive DTEND, escaped text, CRLF) are documented in the Python module.
 */

function icsEscape(text) {
  return String(text).replace(/\\/g, "\\\\").replace(/;/g, "\\;")
    .replace(/,/g, "\\,").replace(/\n/g, "\\n");
}

function icsDate(iso, addDays = 0) {
  const d = parseDay(iso);
  d.setDate(d.getDate() + addDays);
  return toIso(d).replace(/-/g, "");
}

function buildCalendar(sessions) {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const lines = [
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Camp Radar//EN",
    "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Camp Radar",
  ];
  sessions.forEach((s) => {
    lines.push(
      "BEGIN:VEVENT",
      `UID:${s.key}@camp-radar`,
      `DTSTAMP:${stamp}`,
      `DTSTART;VALUE=DATE:${icsDate(s.start_date)}`,
      // DTEND is exclusive for all-day events — without the +1 the last day
      // of camp silently vanishes from the calendar.
      `DTEND;VALUE=DATE:${icsDate(s.end_date, 1)}`,
      `SUMMARY:${icsEscape(s.title)}`
    );
    if (s.url) lines.push(`URL:${s.url}`);
    lines.push("END:VEVENT");
  });
  lines.push("END:VCALENDAR");
  return lines.join("\r\n") + "\r\n";
}

function downloadIcs() {
  const sessions = visibleSessions();
  if (sessions.length === 0) return;
  const blob = new Blob([buildCalendar(sessions)], { type: "text/calendar;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = activeBreak ? `camps-${activeBreak.toLowerCase().replace(/\s+/g, "-")}.ics`
                                : "camps.ics";
  anchor.click();
  URL.revokeObjectURL(url);
}

// -------------------------------------------------------------------- init

/* Rewrite the URL placeholder in the raw-data snippets to this deployment's
 * actual address, so they are copy-pasteable from wherever the site is hosted
 * — a fork, a rename or a local server all get correct commands. */
function renderRawDataUrls() {
  const absolute = new URL(DATA_URL, window.location.href).href;
  document.querySelectorAll("#raw-url, #raw-count, #raw-new, #raw-jq").forEach((node) => {
    node.textContent = node.textContent.replace(/URL/g, absolute);
  });
}

function wireControls() {
  document.getElementById("add-kid").addEventListener("click", () => {
    const nameInput = document.getElementById("kid-name");
    const ageInput = document.getElementById("kid-age");
    const name = nameInput.value.trim();
    const age = Number(ageInput.value);
    if (!name || !Number.isFinite(age) || age < 0 || age > 21) return;

    kids.push({ id: crypto.randomUUID(), name, age });
    saveKids();
    nameInput.value = "";
    ageInput.value = "";
    render();
  });

  document.getElementById("export-ics").addEventListener("click", downloadIcs);

  document.getElementById("clear-break").addEventListener("click", () => {
    activeBreak = null;
    render();
  });
}

async function init() {
  kids = loadKids();
  wireControls();
  renderRawDataUrls();

  try {
    const response = await fetch(DATA_URL, { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    dataset = await response.json();
  } catch (error) {
    // Fail visibly. A blank page would look like "no camps found", which is a
    // very different and much more alarming message than "data didn't load".
    document.getElementById("session-list").replaceChildren(
      Object.assign(document.createElement("div"), {
        className: "empty",
        textContent: `Couldn't load camp data (${error.message}). Try reloading.`,
      })
    );
    return;
  }

  renderUpdatedAt();
  document.getElementById("total-count").textContent =
    `${dataset.sessions.length} sessions · ${dataset.breaks.length} breaks`;

  render();
}

document.addEventListener("DOMContentLoaded", init);
