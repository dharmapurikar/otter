.pragma library

// Pure state machine and window/audio matching logic for meeting detection.
// No QML types, no timers, no IO: export a pure reducer (state, event) -> { state, actions }.
// Testable directly under Node.js without Wayland or Quickshell mocks.

var BUILTIN_PATTERNS = [
  { needle: "meet.google.com",     label: "Google Meet",     callScoped: true },
  { needle: "whereby.com",         label: "Whereby",         callScoped: true },
  { needle: "meet.jit.si",         label: "Jitsi",           callScoped: true },
  { needle: "zoom.us",             label: "Zoom" },
  { needle: "us.zoom.zoom",        label: "Zoom" },
  { needle: "teams.microsoft.com", label: "Microsoft Teams" },
  { needle: "teams.live.com",      label: "Microsoft Teams" },
  { needle: "app.slack.com/huddle", label: "Slack huddle" },
  { needle: "discord",             label: "Discord call" }
];

var TITLE_PATTERNS = [
  { needle: "google meet",     label: "Google Meet" },
  { needle: "zoom meeting",    label: "Zoom" },
  { needle: "microsoft teams", label: "Microsoft Teams" }
];

function matchLabel(appId, title, extraPatterns) {
  var id = String(appId || "").toLowerCase();
  var ttl = String(title || "").toLowerCase();

  var best = null;
  for (var i = 0; i < BUILTIN_PATTERNS.length; i++) {
    var p = BUILTIN_PATTERNS[i];
    if (id.indexOf(p.needle) === -1) continue;
    if (!best || (p.callScoped && !best.callScoped)) best = p;
  }
  if (best) return { needle: best.needle, label: best.label, callScoped: best.callScoped === true };

  for (var j = 0; j < TITLE_PATTERNS.length; j++) {
    if (ttl.indexOf(TITLE_PATTERNS[j].needle) !== -1)
      return { needle: TITLE_PATTERNS[j].needle, label: TITLE_PATTERNS[j].label, callScoped: TITLE_PATTERNS[j].callScoped === true };
  }

  var extra = String(extraPatterns || "").toLowerCase().split(",");
  for (var k = 0; k < extra.length; k++) {
    var needle = extra[k].replace(/^\s+|\s+$/g, "");
    if (needle !== "" && (id.indexOf(needle) !== -1 || ttl.indexOf(needle) !== -1))
      return { needle: needle, label: needle, callScoped: false };
  }
  return null;
}

function expectedBinary(appId) {
  var id = String(appId || "").toLowerCase();
  if (id.indexOf("chrome") !== -1) return "chrome";
  if (id.indexOf("chromium") !== -1) return "chromium";
  if (id.indexOf("firefox") !== -1) return "firefox";
  if (id.indexOf("zoom") !== -1) return "zoom";
  if (id.indexOf("discord") !== -1) return "discord";
  if (id.indexOf("slack") !== -1) return "slack";
  return "";
}

function captureMatches(matchAppId, cap) {
  var want = expectedBinary(matchAppId);
  if (want === "") return true;
  return String(cap && cap.binary || "").toLowerCase() === want;
}

function isMeetingActive(match, micCaptures, mode) {
  if (mode === "off" || !match) return false;
  if (match.callScoped) return true;
  var caps = micCaptures || [];
  for (var i = 0; i < caps.length; i++) {
    if (captureMatches(match.appId, caps[i])) return true;
  }
  return false;
}

function initialState(opts) {
  var o = opts || {};
  return {
    mode: o.mode || "countdown", // "off" | "countdown" | "notify"
    countdownSec: Number(o.countdownSec) || 5,
    extraPatterns: o.extraPatterns || "",
    recording: o.recording === true,
    match: null,                 // { appId, label, callScoped }
    active: false,
    activeLabel: "",
    leavePending: false,
    pendingAction: "",           // "" | "start" | "stop"
    countdownLeft: 0,
    starting: false,             // waiting for helper "started" confirmation
    stopping: false              // waiting for helper "stopped" confirmation
  };
}

function cloneState(s) {
  return {
    mode: s.mode,
    countdownSec: s.countdownSec,
    extraPatterns: s.extraPatterns,
    recording: s.recording,
    match: s.match ? { appId: s.match.appId, label: s.match.label, callScoped: s.match.callScoped } : null,
    active: s.active,
    activeLabel: s.activeLabel,
    leavePending: s.leavePending,
    pendingAction: s.pendingAction,
    countdownLeft: s.countdownLeft,
    starting: s.starting,
    stopping: s.stopping
  };
}

function reduce(state, event) {
  var next = cloneState(state);
  var actions = [];
  var ev = event || {};

  switch (ev.type) {
  case "SCAN": {
    var list = ev.toplevels || [];
    var match = null;
    if (state.mode !== "off") {
      for (var i = 0; i < list.length; i++) {
        var t = list[i];
        if (!t) continue;
        var m = matchLabel(t.appId, t.title, state.extraPatterns);
        if (!m) continue;
        var cand = { appId: String(t.appId || ""), label: m.label, callScoped: m.callScoped === true };
        if (!match || (cand.callScoped && !match.callScoped)) match = cand;
      }
    }
    next.match = match;

    var currentlyActive = isMeetingActive(match, ev.micCaptures || [], state.mode);

    if (currentlyActive) {
      if (state.leavePending) {
        actions.push({ type: "NOTE", event: "leave_cancelled" });
        actions.push({ type: "STOP_LEAVE_TIMER" });
        next.leavePending = false;
      }
      if (!state.active) {
        next.active = true;
        next.activeLabel = match ? match.label : "";
        actions.push({ type: "NOTE", event: "join", fields: { label: next.activeLabel } });

        // onJoin:
        if (state.pendingAction === "stop") {
          next.pendingAction = "";
          next.countdownLeft = 0;
          actions.push({ type: "STOP_COUNTDOWN_TIMER" });
          actions.push({ type: "STOP_TICK_TIMER" });
          actions.push({ type: "NOTE", event: "countdown_cancel", fields: { action: "stop", by: "cross-cancel" } });
          actions.push({ type: "TOAST_OUTCOME", headline: "Cancelled", body: "" });
        } else if (!state.recording) {
          if (state.mode === "countdown") {
            next.pendingAction = "start";
            next.countdownLeft = state.countdownSec;
            actions.push({ type: "NOTE", event: "countdown_arm", fields: { action: "start", label: next.activeLabel } });
            actions.push({
              type: "TOAST_COUNTDOWN",
              headline: next.activeLabel + " detected",
              body: "Recording starts in " + state.countdownSec + " s — click to cancel.",
              urgent: true,
              withCancel: true
            });
            actions.push({ type: "START_TICK_TIMER" });
            actions.push({ type: "START_COUNTDOWN_TIMER", ms: state.countdownSec * 1000 });
          } else {
            actions.push({
              type: "NOTIFY",
              headline: next.activeLabel + " detected",
              body: "Click to record this meeting with Otter.",
              action: "record",
              urgent: true
            });
          }
        }
      }
    } else if (state.active && state.mode !== "off") {
      if (!state.leavePending) {
        next.leavePending = true;
        actions.push({ type: "NOTE", event: "leave_pending", fields: { label: state.activeLabel } });
        actions.push({ type: "START_LEAVE_TIMER", ms: 3000 });
      }
    } else if (state.active) {
      // mode became "off"
      next.active = false;
      next.activeLabel = "";
      next.leavePending = false;
      actions.push({ type: "STOP_LEAVE_TIMER" });
      actions.push({ type: "NOTE", event: "mode_dropped_state", fields: { label: state.activeLabel } });
    }
    break;
  }

  case "LEAVE_CONFIRMED": {
    if (state.leavePending) {
      next.leavePending = false;
      next.active = false;
      var lbl = state.activeLabel;
      actions.push({ type: "NOTE", event: "leave", fields: { label: lbl } });

      // onLeave:
      if (state.pendingAction === "start") {
        next.pendingAction = "";
        next.countdownLeft = 0;
        actions.push({ type: "STOP_COUNTDOWN_TIMER" });
        actions.push({ type: "STOP_TICK_TIMER" });
        actions.push({ type: "NOTE", event: "countdown_cancel", fields: { action: "start", by: "cross-cancel" } });
        actions.push({ type: "TOAST_OUTCOME", headline: "Cancelled", body: "" });
      } else if (state.recording) {
        if (state.mode === "countdown") {
          next.pendingAction = "stop";
          next.countdownLeft = state.countdownSec;
          actions.push({ type: "NOTE", event: "countdown_arm", fields: { action: "stop", label: lbl } });
          actions.push({
            type: "TOAST_COUNTDOWN",
            headline: lbl + " ended",
            body: "Stopping in " + state.countdownSec + " s — click to keep recording.",
            urgent: true,
            withCancel: true
          });
          actions.push({ type: "START_TICK_TIMER" });
          actions.push({ type: "START_COUNTDOWN_TIMER", ms: state.countdownSec * 1000 });
        } else {
          actions.push({
            type: "NOTIFY",
            headline: lbl + " ended",
            body: "Still recording — click to stop.",
            action: "stop",
            urgent: true
          });
        }
      }
    }
    break;
  }

  case "TICK": {
    if (state.pendingAction !== "") {
      var left = state.countdownLeft - 1;
      next.countdownLeft = left;
      if (left > 0) {
        var head = state.activeLabel + (state.pendingAction === "start" ? " detected" : " ended");
        var body = state.pendingAction === "start"
          ? "Recording starts in " + left + " s — click to cancel."
          : "Stopping in " + left + " s — click to keep recording.";
        actions.push({
          type: "TOAST_COUNTDOWN",
          headline: head,
          body: body,
          urgent: true,
          withCancel: true
        });
      }
    }
    break;
  }

  case "COUNTDOWN_EXPIRED": {
    var act = state.pendingAction;
    next.pendingAction = "";
    next.countdownLeft = 0;
    actions.push({ type: "STOP_TICK_TIMER" });
    actions.push({ type: "NOTE", event: "countdown_fire", fields: { action: act } });

    if (act === "start" && !state.recording) {
      if (state.active) {
        next.starting = true;
        actions.push({ type: "REQUEST_START" });
        actions.push({
          type: "TOAST_OUTCOME",
          headline: "Starting…",
          body: "Connecting to Otter…"
        });
      } else {
        actions.push({ type: "NOTE", event: "start_skipped_meeting_gone" });
        actions.push({
          type: "TOAST_OUTCOME",
          headline: "Not started",
          body: "The meeting ended before the countdown finished."
        });
      }
    } else if (act === "stop" && state.recording) {
      next.stopping = true;
      actions.push({ type: "REQUEST_STOP" });
      actions.push({
        type: "TOAST_OUTCOME",
        headline: "Stopping…",
        body: "Finalizing upload…"
      });
    }
    break;
  }

  case "RECORDING_STARTED": {
    next.recording = true;
    if (state.starting) {
      next.starting = false;
      actions.push({
        type: "TOAST_OUTCOME",
        headline: "Recording started",
        body: "Otter is recording this meeting."
      });
    }
    break;
  }

  case "RECORDING_START_FAILED": {
    next.recording = false;
    if (state.starting) {
      next.starting = false;
      // A start the helper deliberately suppressed is not a failure: it
      // declined because something was already recording, or because a
      // restart had just auto-started this same meeting. Saying "failed"
      // there reads as a bug in the plugin rather than the plugin working.
      var suppressed = String(ev.errorClass || "") === "suppressed";
      actions.push({
        type: "TOAST_OUTCOME",
        headline: suppressed ? "Not started" : "Recording failed",
        body: ev.friendlyError || ev.message
          || (suppressed ? "Already recording" : "Failed to start recording")
      });
    }
    break;
  }

  case "RECORDING_STOPPED": {
    next.recording = false;
    if (state.stopping) {
      next.stopping = false;
      actions.push({
        type: "TOAST_OUTCOME",
        headline: "Recording stopped",
        body: ""
      });
    }
    break;
  }

  case "RECORDING_STOP_FAILED": {
    next.recording = false;
    if (state.stopping) {
      next.stopping = false;
      actions.push({
        type: "TOAST_OUTCOME",
        headline: "Stop failed",
        body: ev.friendlyError || ev.message || "Could not finalize recording"
      });
    }
    break;
  }

  case "CANCEL_CLICKED": {
    var wasAction = state.pendingAction;
    if (wasAction !== "") {
      next.pendingAction = "";
      next.countdownLeft = 0;
      next.starting = false;
      next.stopping = false;
      actions.push({ type: "STOP_COUNTDOWN_TIMER" });
      actions.push({ type: "STOP_TICK_TIMER" });
      actions.push({
        type: "NOTE",
        event: "countdown_cancel",
        fields: { action: wasAction, by: ev.by || "click" }
      });
      if (ev.announce === false) {
        actions.push({ type: "TOAST_OUTCOME", headline: "Cancelled", body: "" });
      } else if (wasAction === "start") {
        actions.push({
          type: "TOAST_OUTCOME",
          headline: "Cancelled",
          body: "The meeting will not be recorded. Start anyway from the Otter bar icon."
        });
      } else {
        actions.push({
          type: "TOAST_OUTCOME",
          headline: "Recording continues",
          body: "Stopping was cancelled."
        });
      }
    }
    break;
  }

  case "SET_RECORDING": {
    next.recording = ev.recording === true;
    if (!next.recording) {
      next.starting = false;
      next.stopping = false;
    }
    break;
  }

  case "SET_MODE": {
    next.mode = ev.mode || "countdown";
    if (next.mode === "off") {
      if (state.active) {
        next.active = false;
        next.activeLabel = "";
        next.leavePending = false;
        actions.push({ type: "STOP_LEAVE_TIMER" });
        actions.push({ type: "NOTE", event: "mode_dropped_state", fields: { label: state.activeLabel } });
      }
      if (state.pendingAction !== "") {
        next.pendingAction = "";
        next.countdownLeft = 0;
        actions.push({ type: "STOP_COUNTDOWN_TIMER" });
        actions.push({ type: "STOP_TICK_TIMER" });
        actions.push({ type: "NOTE", event: "countdown_cancel",
                       fields: { action: state.pendingAction, by: "cross-cancel" } });
        actions.push({ type: "TOAST_OUTCOME", headline: "Cancelled", body: "" });
      }
    }
    break;
  }

  case "SET_EXTRA_PATTERNS": {
    next.extraPatterns = ev.extraPatterns || "";
    break;
  }

  case "TIMEOUT": {
    if (state.starting) {
      next.starting = false;
      actions.push({
        type: "TOAST_OUTCOME",
        headline: "Recording failed",
        body: "Otter did not respond; try again"
      });
    }
    if (state.stopping) {
      next.stopping = false;
      actions.push({
        type: "TOAST_OUTCOME",
        headline: "Recording stopped",
        body: "Finalize timed out; audio preserved on disk"
      });
    }
    break;
  }
  }

  return { state: next, actions: actions };
}
