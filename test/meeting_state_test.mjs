// Tests for MeetingState.js -- pure reducer and meeting matching logic.
//
//   node test/meeting_state_test.mjs

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "MeetingState.js"), "utf8")
  .replace(/^\.pragma library\s*$/m, "");

const MS = {};
new Function(
  "exports",
  src +
  "\nexports.matchLabel = matchLabel;" +
  "\nexports.expectedBinary = expectedBinary;" +
  "\nexports.captureMatches = captureMatches;" +
  "\nexports.isMeetingActive = isMeetingActive;" +
  "\nexports.initialState = initialState;" +
  "\nexports.reduce = reduce;"
)(MS);

let failures = 0;
function eq(got, want, label) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(
    `${ok ? "ok  " : "FAIL"} ${label}: ${JSON.stringify(got)}` +
    (ok ? "" : ` != ${JSON.stringify(want)}`)
  );
}

function assertTrue(cond, label) {
  if (!cond) failures++;
  console.log(`${cond ? "ok  " : "FAIL"} ${label}`);
}

// 1. Matching logic
const meet = MS.matchLabel("chrome-meet.google.com__-Default", "Google Meet", "");
eq(meet.label, "Google Meet", "match Google Meet appId");
eq(meet.callScoped, true, "Google Meet is call-scoped");

const teams = MS.matchLabel("chrome-teams.microsoft.com__-Default", "Microsoft Teams", "");
eq(teams.label, "Microsoft Teams", "match Teams appId");
eq(teams.callScoped, false, "Teams is persistent (not call-scoped)");

const zoomTitle = MS.matchLabel("some.random.app", "Zoom Meeting - Standup", "");
eq(zoomTitle.label, "Zoom", "match Zoom by title");

const nomatch = MS.matchLabel("firefox", "Wikipedia", "");
eq(nomatch, null, "unmatched window is null");

// 2. Conjunction & Mic matching
const meetCand = { appId: "chrome-meet.google.com__", label: "Google Meet", callScoped: true };
assertTrue(MS.isMeetingActive(meetCand, [], "countdown"), "Meet is active even without mic captures");

const teamsCand = { appId: "chrome-teams.microsoft.com__", label: "Microsoft Teams", callScoped: false };
assertTrue(!MS.isMeetingActive(teamsCand, [], "countdown"), "Teams is inactive without mic captures");
assertTrue(!MS.isMeetingActive(teamsCand, [{ binary: "pavucontrol" }], "countdown"), "Teams ignores unrelated mic capture");
assertTrue(MS.isMeetingActive(teamsCand, [{ binary: "chrome" }], "countdown"), "Teams is active with chrome mic capture");

// 3. State machine: Google Meet Join -> Countdown -> Start Confirmed
let s = MS.initialState({ mode: "countdown", countdownSec: 5 });
let res = MS.reduce(s, {
  type: "SCAN",
  toplevels: [{ appId: "chrome-meet.google.com__", title: "Meet" }],
  micCaptures: []
});
s = res.state;
eq(s.active, true, "Meet is active");
eq(s.pendingAction, "start", "start countdown armed");
eq(s.countdownLeft, 5, "countdown left is 5");
assertTrue(res.actions.some(a => a.type === "START_COUNTDOWN_TIMER" && a.ms === 5000), "countdown timer started");
assertTrue(res.actions.some(a => a.type === "TOAST_COUNTDOWN"), "countdown toast sent");

// Ticks
res = MS.reduce(s, { type: "TICK" });
s = res.state;
eq(s.countdownLeft, 4, "tick decrements countdown");
assertTrue(res.actions.some(a => a.type === "TOAST_COUNTDOWN" && a.body.includes("4 s")), "tick updates toast");

// Expire countdown
res = MS.reduce(s, { type: "COUNTDOWN_EXPIRED" });
s = res.state;
eq(s.pendingAction, "", "pendingAction cleared on expire");
eq(s.starting, true, "state is starting");
assertTrue(res.actions.some(a => a.type === "REQUEST_START"), "start requested on fire");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.headline === "Starting…"), "toast shows Starting…");

// Terminal frame: RECORDING_STARTED
res = MS.reduce(s, { type: "RECORDING_STARTED", otid: "otid-1" });
s = res.state;
eq(s.recording, true, "recording is true");
eq(s.starting, false, "starting cleared on confirm");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.headline === "Recording started"), "toast confirmed recording started");

// 4. Meeting ends during countdown -> start_skipped_meeting_gone
s = MS.initialState({ mode: "countdown", countdownSec: 5 });
res = MS.reduce(s, {
  type: "SCAN",
  toplevels: [{ appId: "chrome-meet.google.com__", title: "Meet" }],
  micCaptures: []
});
s = res.state;
// Window closes during countdown!
res = MS.reduce(s, { type: "SCAN", toplevels: [], micCaptures: [] });
s = res.state;
// Leave confirmed
res = MS.reduce(s, { type: "LEAVE_CONFIRMED" });
s = res.state;
eq(s.pendingAction, "", "start countdown cancelled because meeting left");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.headline === "Cancelled"), "toast cancelled");

// 5. User clicks Cancel on countdown
s = MS.initialState({ mode: "countdown", countdownSec: 5 });
res = MS.reduce(s, {
  type: "SCAN",
  toplevels: [{ appId: "chrome-meet.google.com__", title: "Meet" }],
  micCaptures: []
});
s = res.state;
res = MS.reduce(s, { type: "CANCEL_CLICKED", announce: true });
s = res.state;
eq(s.pendingAction, "", "pendingAction cleared on cancel click");
assertTrue(res.actions.some(a => a.type === "STOP_COUNTDOWN_TIMER"), "countdown timer stopped");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.headline === "Cancelled"), "outcome toast Cancelled");

// 6. Mic blink debounce (leave confirmation)
s = MS.initialState({ mode: "countdown", countdownSec: 5, recording: true });
s.active = true;
s.activeLabel = "Microsoft Teams";
s.match = teamsCand;

// Mic drops briefly
res = MS.reduce(s, { type: "SCAN", toplevels: [teamsCand], micCaptures: [] });
s = res.state;
eq(s.leavePending, true, "leave is pending");
assertTrue(res.actions.some(a => a.type === "START_LEAVE_TIMER" && a.ms === 3000), "leave timer started for 3s");

// Mic returns before 3s timer expires
res = MS.reduce(s, { type: "SCAN", toplevels: [teamsCand], micCaptures: [{ binary: "chrome" }] });
s = res.state;
eq(s.leavePending, false, "leavePending cancelled");
eq(s.active, true, "still active");
assertTrue(res.actions.some(a => a.type === "STOP_LEAVE_TIMER"), "leave timer stopped");
assertTrue(res.actions.some(a => a.type === "NOTE" && a.event === "leave_cancelled"), "leave_cancelled logged");

// 7. Leave persists for 3s -> Stop countdown armed
res = MS.reduce(s, { type: "SCAN", toplevels: [teamsCand], micCaptures: [] });
s = res.state;
res = MS.reduce(s, { type: "LEAVE_CONFIRMED" });
s = res.state;
eq(s.active, false, "active is false after leave confirmed");
eq(s.pendingAction, "stop", "stop countdown armed");
assertTrue(res.actions.some(a => a.type === "START_COUNTDOWN_TIMER"), "stop countdown timer started");

// 8. Rejoin while stop countdown is running -> cross-cancels stop
res = MS.reduce(s, { type: "SCAN", toplevels: [teamsCand], micCaptures: [{ binary: "chrome" }] });
s = res.state;
eq(s.active, true, "rejoined, active is true");
eq(s.pendingAction, "", "stop countdown cross-cancelled");
assertTrue(res.actions.some(a => a.type === "STOP_COUNTDOWN_TIMER"), "stop timer cancelled");

// 9. Stop countdown expires -> requests stop -> confirms stop
s.pendingAction = "stop";
s.active = false;
res = MS.reduce(s, { type: "COUNTDOWN_EXPIRED" });
s = res.state;
eq(s.stopping, true, "state is stopping");
assertTrue(res.actions.some(a => a.type === "REQUEST_STOP"), "stop requested");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.headline === "Stopping…"), "toast shows Stopping…");

res = MS.reduce(s, { type: "RECORDING_STOPPED", url: "https://otter.ai/u/abc" });
s = res.state;
eq(s.recording, false, "recording false");
eq(s.stopping, false, "stopping cleared");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.headline === "Recording stopped"), "toast shows Recording stopped");

// 10. Start failed terminal frame
s = MS.initialState({ mode: "countdown" });
s.starting = true;
res = MS.reduce(s, {
  type: "RECORDING_START_FAILED",
  errorClass: "internal",
  friendlyError: "Internal error · See events.log"
});
s = res.state;
eq(s.starting, false, "starting cleared on failure");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.headline === "Recording failed" && a.body.includes("Internal error")), "toast shows error");

// 11. Turning detection off mid-countdown cancels it, and the cancel is
// logged -- a mode change must not silently eat an armed action.
s = MS.initialState({ mode: "countdown", countdownSec: 5 });
res = MS.reduce(s, { type: "SCAN", toplevels: [meetCand], micCaptures: [] });
s = res.state;
res = MS.reduce(s, { type: "SET_MODE", mode: "off" });
s = res.state;
eq(s.pendingAction, "", "mode off clears the armed countdown");
assertTrue(res.actions.some(a => a.type === "NOTE" && a.event === "countdown_cancel"
  && a.fields.by === "cross-cancel"), "mode-cancel is logged as countdown_cancel");
assertTrue(res.actions.some(a => a.type === "NOTE" && a.event === "mode_dropped_state"), "mode off drops state loudly");

// 12. A start the helper suppressed on purpose is not a failure. It declines
// when something is already recording, or when a restart just auto-started
// this same meeting -- saying "Recording failed" there reads as a bug in the
// plugin rather than the plugin doing its job.
s = MS.initialState({ mode: "countdown" });
s.starting = true;
res = MS.reduce(s, {
  type: "RECORDING_START_FAILED",
  errorClass: "suppressed",
  message: "Microsoft Teams was auto-recorded 12s ago; not starting a second recording"
});
s = res.state;
eq(s.starting, false, "starting cleared on a suppressed start");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.headline === "Not started"), "suppressed start is not called a failure");
assertTrue(res.actions.some(a => a.type === "TOAST_OUTCOME" && a.body.includes("auto-recorded")), "suppressed start explains itself");

// 13. The label travels with a start request, so the helper can hold a
// per-meeting cooldown across its own restarts.
s = MS.initialState({ mode: "countdown", countdownSec: 5 });
res = MS.reduce(s, { type: "SCAN", toplevels: [meetCand], micCaptures: [] });
s = res.state;
eq(s.activeLabel, "Google Meet", "the active meeting is labelled for the cooldown");
res = MS.reduce(s, { type: "COUNTDOWN_EXPIRED" });
assertTrue(res.actions.some(a => a.type === "REQUEST_START"), "the countdown still asks to start");

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
