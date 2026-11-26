// Cloudflare Worker: the clock for every scheduled job of this repo.
//
// GitHub's own scheduled runs are throttled to a few a day and start hours late, so each
// job is started from here instead, with workflow_dispatch (owner, 2026-11-02).
//
// Cron trigger (set in the dashboard): 0,30 * * * *   (UTC; ticks at :00 and :30)
// Secret (set in the dashboard):       GH_DISPATCH_TOKEN  (fine-grained PAT, Actions: write,
//                                      this code repo ONLY -- never the data repo; Actions: read
//                                      comes with write, and lists a job's running runs)
// Optional secret:                     DISCORD_WEBHOOK_URL (failed dispatches, token expiry)
//
// Deployed by hand: paste this file into the Worker's editor in the Cloudflare dashboard
// (Workers & Pages -> the worker -> Edit code). It lives in the repo so the schedule is
// versioned and checked (checks/test_worker_schedule_110226.py runs it under node and
// compares it with an independent model); the repo never deploys it.
//
// THREE KINDS OF JOB
//
// 1. Ungated, the original two: data-refresh.yml on every active tick (every 30 minutes)
//    and market-breadth.yml on the :00 ticks only, inside the active period below. A plain
//    dispatch runs them straight away (they skip their own gate).
//
// 2. Gated: each other job has a slot (New York time). The Worker dispatches it at the slot
//    and again every hour for RETRY_HOURS afterwards, always with scheduled=true. That input
//    makes the run go through the job's slot gate, which skips it when the slot's work has
//    already succeeded -- so the repeats are a retry (a failed or part-finished run is picked
//    up at the next hour) and never a second post to Discord. A dispatch WITHOUT the input is
//    a run by hand and bypasses the gate (and forces every alert rule), so it must be here.
//    A gate's `slots` / `days` in the workflow file must say the same as the table below
//    (checks/test_worker_schedule_110226.py compares them): the gate decides which slot a
//    late retry belongs to.
//
// 3. keep-alive.yml: every 4 hours (00, 04, 08, 12, 16 and 20 UTC). No gate.
//
// Times are New York time (Intl handles the clock change), so a slot is at the same local
// time in winter and summer. NSE closes at 10:00 UTC: 5:00 AM EST, 6:00 AM EDT -- in summer
// the 6:30 market read therefore finds India's reading not final and the next retry (7:30)
// writes it.

const OWNER = "swap9047";
const REPO = "stock_screener_us_india";
const REF = "main";

// ---- 1. the ungated jobs' active period (New York time) ------------------------------------
// A tick is active when:
//   Sunday     from 22:00
//   Mon-Thu    22:00-23:59 and 00:00-17:00 (so never 17:30-21:30)
//   Friday     until 17:00 (the 17:00 tick is the last of the week)
//   Saturday   never
// A dispatch skips market-breadth.yml's own gate, so this rule has to live here; keep it equal
// to that gate's slots and window.
const START = 22 * 60; // 22:00 as minutes since midnight
const END = 17 * 60; // 17:00

// ---- 2. the gated jobs ----------------------------------------------------------------------
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"];

// Hourly re-dispatches after a slot (the gate skips them once the slot is done). A job whose
// run keeps failing for longer than this is not retried again until its next slot.
const RETRY_HOURS = 8;

// slots: [hour, minute] in New York time; days: the SLOT's weekday (the gate's `days`), absent =
// every day. Cloudflare ticks only at :00 and :30, so a slot is on one of those.
const GATED_JOBS = [
  { workflow: "news-summary.yml", slots: [[16, 0]], days: ["Sun", "Wed"] },
  { workflow: "market-read.yml", slots: [[6, 30], [17, 30]], days: WEEKDAYS },
  { workflow: "market-library.yml", slots: [[7, 0], [17, 0]] },
  { workflow: "quarterly-results.yml", slots: [[4, 0]] },
  { workflow: "expert-views.yml", slots: [[2, 0]] },
  { workflow: "fundamentals.yml", slots: [[21, 0]] },
  { workflow: "daily-alerts.yml", slots: [[21, 0]] },
  { workflow: "weekly-wrapup.yml", slots: [[21, 0]], days: ["Sun"] },
];

// ---- the schedule, as one pure function -----------------------------------------------------

function etParts(date) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour: "numeric",
    minute: "numeric",
    hourCycle: "h23",
  }).formatToParts(date);
  const get = (t) => parts.find((p) => p.type === t).value;
  return { day: get("weekday"), hour: Number(get("hour")), minute: Number(get("minute")) };
}

function isActive(date) {
  const { day, hour, minute } = etParts(date);
  const m = hour * 60 + minute;
  if (day === "Sun") return m >= START;
  if (day === "Fri") return m <= END;
  if (day === "Sat") return false;
  return m >= START || m <= END; // Mon-Thu
}

// Is `date` the slot [hour, minute] itself, or one of its hourly retries (up to RETRY_HOURS
// later)? The slot's weekday decides `days`, as the gate's does: the Sunday wrap-up's retries
// run into Monday morning and are still Sunday's.
function isSlotOrRetry(date, job, hour, minute) {
  for (let k = 0; k <= RETRY_HOURS; k++) {
    const p = etParts(new Date(date.getTime() - k * 3600 * 1000));
    if (p.hour === hour && p.minute === minute && (!job.days || job.days.includes(p.day))) return true;
  }
  return false;
}

// What to dispatch on the tick at `date`: [{ workflow, inputs? }], one entry per workflow.
function dispatchesFor(date) {
  const out = [];
  if (isActive(date)) {
    out.push({ workflow: "data-refresh.yml" });
    // Breadth once an hour (the :00 tick only).
    if (date.getUTCMinutes() === 0) out.push({ workflow: "market-breadth.yml" });
  }
  if (date.getUTCMinutes() === 0 && date.getUTCHours() % 4 === 0) out.push({ workflow: "keep-alive.yml" });
  for (const job of GATED_JOBS) {
    if (job.slots.some(([h, m]) => isSlotOrRetry(date, job, h, m))) {
      out.push({ workflow: job.workflow, inputs: { scheduled: "true" } });
    }
  }
  return out;
}

const GH_HEADERS = (env) => ({
  Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "stock-screener-cron", // GitHub rejects requests without one
});

// ---- the token's expiry ---------------------------------------------------------------------
// Every GitHub reply to a fine-grained token carries GitHub-Authentication-Token-Expiration
// ("2026-12-01 10:00:00 UTC" or "... -0800"). The token expiring stops EVERY job with no alert,
// so a token with under TOKEN_WARN_DAYS left is posted to Discord -- on the 00:00 UTC tick only,
// so once a day (keep-alive.yml is dispatched on that tick every day, so a reply is always read).
const TOKEN_WARN_DAYS = 14;

function parseExpiry(text) {
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s*(UTC|Z|[+-]\d{2}:?\d{2})?$/.exec((text || "").trim());
  if (!m) return null;
  let zone = m[3] || "Z";
  if (zone === "UTC") zone = "Z";
  else if (zone !== "Z" && !zone.includes(":")) zone = `${zone.slice(0, 3)}:${zone.slice(3)}`;
  const t = Date.parse(`${m[1]}T${m[2]}${zone}`);
  return Number.isNaN(t) ? null : t;
}

function noteExpiry(tick, res) {
  const header = res && res.headers && res.headers.get("github-authentication-token-expiration");
  if (header && tick.expiry === undefined) tick.expiry = parseExpiry(header);
}

// ---- a gated job already running or queued --------------------------------------------------
// GitHub keeps ONE pending run per concurrency group and cancels the older pending one when a
// new run joins (2026-11-22): every hourly retry dispatched while a run was in progress
// cancelled whatever was waiting behind it -- a Re-analyze All or Retry Pending the owner had
// just clicked. So a gated job with a run in progress or queued is not dispatched on this tick:
// that run either does the slot's work or fails, and the next hourly retry (RETRY_HOURS of them)
// asks again. A listing that fails says nothing either way: dispatch as before (fail open), so
// an API blip never costs a slot.
async function hasActiveRun(env, tick, workflow) {
  for (const status of ["in_progress", "queued"]) {
    let res;
    try {
      res = await fetch(
        `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/runs?status=${status}&per_page=1`,
        { method: "GET", headers: GH_HEADERS(env) },
      );
    } catch (e) {
      console.warn(`${workflow}: could not list ${status} runs (${e}) -- dispatching anyway`);
      return false;
    }
    noteExpiry(tick, res);
    if (!res.ok) {
      console.warn(`${workflow}: could not list ${status} runs (HTTP ${res.status}) -- dispatching anyway`);
      return false;
    }
    let body;
    try {
      body = await res.json();
    } catch (e) {
      console.warn(`${workflow}: unreadable ${status} runs listing -- dispatching anyway`);
      return false;
    }
    if ((body.total_count || 0) > 0 || (body.workflow_runs || []).length > 0) {
      console.log(`${workflow}: a run is ${status} -- not dispatched on this tick (the next hourly retry covers the slot)`);
      return true;
    }
  }
  return false;
}

async function dispatch(env, tick, { workflow, inputs }) {
  if (inputs && inputs.scheduled === "true" && (await hasActiveRun(env, tick, workflow))) return;
  let res;
  try {
    res = await fetch(
      `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`,
      {
        method: "POST",
        headers: { ...GH_HEADERS(env), "Content-Type": "application/json" },
        body: JSON.stringify(inputs ? { ref: REF, inputs } : { ref: REF }),
      },
    );
  } catch (e) {
    tick.failures.push(`${workflow} (network error)`);
    throw new Error(`${workflow}: ${e}`);
  }
  noteExpiry(tick, res);
  // 204 on older API docs, 200 on newer: both are res.ok.
  if (!res.ok) {
    tick.failures.push(`${workflow} (HTTP ${res.status})`);
    throw new Error(`${workflow}: HTTP ${res.status} ${await res.text()}`);
  }
  console.log(`${workflow}: dispatched (HTTP ${res.status})`);
}

// ---- Discord (optional) -----------------------------------------------------------------------
// Set DISCORD_WEBHOOK_URL as a Worker secret to be told when a dispatch fails or the token is near
// its expiry; without it these are console lines only. Workflow names and dates only -- never a
// ticker, a token or a URL.
async function notify(env, content) {
  if (!env.DISCORD_WEBHOOK_URL) return;
  try {
    const res = await fetch(env.DISCORD_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, allowed_mentions: { parse: [] } }),
    });
    if (!res.ok) console.error(`Discord notice failed: HTTP ${res.status}`);
  } catch (e) {
    console.error(`Discord notice failed: ${e}`);
  }
}

async function runTick(date, env) {
  const jobs = dispatchesFor(date);
  if (!jobs.length) return; // nothing due on this tick
  const tick = { failures: [], expiry: undefined };
  // allSettled: one failing dispatch must not stop the others.
  const results = await Promise.allSettled(jobs.map((j) => dispatch(env, tick, j)));
  const failed = results.filter((r) => r.status === "rejected");
  failed.forEach((r) => console.error(String(r.reason)));
  if (tick.failures.length) {
    await notify(env, `⚠️ Scheduler: ${tick.failures.length} workflow dispatch(es) failed: ${tick.failures.join(", ")}. `
      + "The next tick tries again; a 401 means the GitHub token needs renewing.");
  }
  if (date.getUTCHours() === 0 && date.getUTCMinutes() === 0 && tick.expiry) {
    const days = (tick.expiry - date.getTime()) / 86400000;
    if (days < TOKEN_WARN_DAYS) {
      const when = new Date(tick.expiry).toISOString().slice(0, 10);
      console.warn(`GitHub dispatch token expires ${when}`);
      await notify(env, `⚠️ Scheduler: the GitHub dispatch token expires on ${when} `
        + `(${Math.max(0, Math.floor(days))} day(s) left). Renew it and update GH_DISPATCH_TOKEN, or every scheduled job stops.`);
    }
  }
  if (failed.length) throw new Error(`${failed.length} dispatch(es) failed`);
}

export default {
  async scheduled(event, env, ctx) {
    await runTick(new Date(event.scheduledTime), env);
  },

  // No public trigger: anyone who found the URL could otherwise spam dispatches.
  async fetch() {
    return new Response("not found", { status: 404 });
  },
};
