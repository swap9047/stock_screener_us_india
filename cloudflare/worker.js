// Cloudflare Worker: the clock for every scheduled job of this repo.
//
// GitHub's own scheduled runs are throttled to a few a day and start hours late, so each
// job is started from here instead, with workflow_dispatch (owner, 2026-11-02).
//
// Cron trigger (set in the dashboard): 0,30 * * * *   (UTC; ticks at :00 and :30)
// Secret (set in the dashboard):       GH_DISPATCH_TOKEN  (fine-grained PAT, Actions: write,
//                                      this code repo ONLY -- never the data repo)
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
  { workflow: "news-summary.yml", slots: [[6, 0]] },
  { workflow: "market-read.yml", slots: [[6, 30], [17, 30]], days: WEEKDAYS },
  { workflow: "market-library.yml", slots: [[7, 0], [17, 0]] },
  { workflow: "quarterly-results.yml", slots: [[4, 0]] },
  { workflow: "expert-views.yml", slots: [[1, 0]] },
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

async function dispatch(env, { workflow, inputs }) {
  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "stock-screener-cron", // GitHub rejects requests without one
        "Content-Type": "application/json",
      },
      body: JSON.stringify(inputs ? { ref: REF, inputs } : { ref: REF }),
    },
  );
  // 204 on older API docs, 200 on newer: both are res.ok.
  if (!res.ok) {
    throw new Error(`${workflow}: HTTP ${res.status} ${await res.text()}`);
  }
  console.log(`${workflow}: dispatched (HTTP ${res.status})`);
}

export default {
  async scheduled(event, env, ctx) {
    const jobs = dispatchesFor(new Date(event.scheduledTime));
    if (!jobs.length) return; // nothing due on this tick
    // allSettled: one failing dispatch must not stop the others.
    const results = await Promise.allSettled(jobs.map((j) => dispatch(env, j)));
    const failed = results.filter((r) => r.status === "rejected");
    failed.forEach((r) => console.error(String(r.reason)));
    if (failed.length) throw new Error(`${failed.length} dispatch(es) failed`);
  },

  // No public trigger: anyone who found the URL could otherwise spam dispatches.
  async fetch() {
    return new Response("not found", { status: 404 });
  },
};
