import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import "MeetingState.js" as MeetingState

// Notices when you join a meeting, and when you leave.
//
// Conjunction of two signals:
// - Wayland toplevels identify meeting apps and meeting titles.
// - PipeWire microphone captures identify active calls for persistent apps.
//
// All decision logic lives in MeetingState.js (pure reducer, unit-tested under node).
// This component manages Wayland bindings, timers, and desktop notifications.
Item {
  id: root

  property string mode: "countdown"
  property bool recording: false
  property string extraPatterns: ""
  property QtObject service: null
  readonly property bool micInUse: service !== null && service.micInUseByOther
  property int countdownSec: 5

  signal startRequested()
  signal stopRequested()

  property var mstate: MeetingState.initialState({
    mode: root.mode,
    countdownSec: root.countdownSec,
    extraPatterns: root.extraPatterns,
    recording: root.recording
  })

  // Notification id of the live countdown toast, for in-place updates.
  property string _toastId: ""

  function note(event, fields) {
    if (service && typeof service.note === "function")
      service.note(event, fields)
  }

  function dispatch(event) {
    var res = MeetingState.reduce(mstate, event)
    mstate = res.state
    executeActions(res.actions)
  }

  function executeActions(actions) {
    if (!actions) return
    for (var i = 0; i < actions.length; i++) {
      var a = actions[i]
      switch (a.type) {
      case "NOTE":
        root.note(a.event, a.fields)
        break
      case "START_LEAVE_TIMER":
        leaveConfirm.interval = a.ms
        leaveConfirm.restart()
        break
      case "STOP_LEAVE_TIMER":
        leaveConfirm.stop()
        break
      case "START_COUNTDOWN_TIMER":
        countdown.interval = a.ms
        countdown.restart()
        break
      case "STOP_COUNTDOWN_TIMER":
        countdown.stop()
        break
      case "START_TICK_TIMER":
        countTick.restart()
        break
      case "STOP_TICK_TIMER":
        countTick.stop()
        break
      case "REQUEST_START":
        startRequested()
        break
      case "REQUEST_STOP":
        stopRequested()
        break
      case "TOAST_COUNTDOWN":
        if (mstate.countdownLeft === root.countdownSec)
          _toastId = ""
        countdownToast(a.headline, a.body, a.urgent, a.withCancel)
        break
      case "TOAST_OUTCOME":
        finishToast(a.headline, a.body)
        break
      case "NOTIFY":
        notify(a.headline, a.body, a.action, a.urgent)
        break
      }
    }
  }

  function scan() {
    var rawList = ToplevelManager.toplevels ? ToplevelManager.toplevels.values : []
    var toplevels = []
    if (rawList) {
      for (var i = 0; i < rawList.length; i++) {
        var t = rawList[i]
        if (t) toplevels.push({ appId: String(t.appId || ""), title: String(t.title || "") })
      }
    }
    var caps = service ? (service.micCaptures || []) : []
    dispatch({ type: "SCAN", toplevels: toplevels, micCaptures: caps })
  }

  function cancelCountdown(announce) {
    var was = mstate.pendingAction
    dispatch({ type: "CANCEL_CLICKED", announce: announce !== false,
               by: announce === false ? "cross-cancel" : "click" })
    return was === "" ? "none" : was
  }

  function finishToast(headline, body) {
    countdownToast(headline, body, false, false)
  }

  Process {
    id: toastProc
    running: false
    command: []
    stdout: SplitParser {
      onRead: function (line) {
        var text = String(line).trim()
        if (/^[0-9]+$/.test(text)) root._toastId = text
      }
    }
    onExited: function (exitCode) { toastProc.running = false }
  }

  function countdownToast(headline, body, urgent, withCancel) {
    if (toastProc.running) return
    var args = ["omarchy-notification-send", "--app-name", "Otter",
                "-g", "󰗋", "-u", urgent ? "normal" : "low", "-p",
                headline, body]
    if (_toastId !== "") args.push("-r", _toastId)
    if (withCancel)
      args.push("--exec", "omarchy-shell", "io.github.dharmapurikar.otter", "cancelCountdown")
    toastProc.command = args
    toastProc.running = true
  }

  function notify(headline, body, action, urgent) {
    var args = ["omarchy-notification-send", "--app-name", "Otter",
                "-g", "󰗋", "-u", urgent ? "normal" : "low", headline, body]
    if (action !== "") {
      args.push("--exec")
      args.push("omarchy-shell")
      args.push("io.github.dharmapurikar.otter")
      args.push(action)
    }
    Quickshell.execDetached(args)
  }

  onModeChanged: {
    note("mode", { mode: mode })
    dispatch({ type: "SET_MODE", mode: mode })
    scan()
  }

  onRecordingChanged: {
    dispatch({ type: "SET_RECORDING", recording: recording })
  }

  onExtraPatternsChanged: {
    dispatch({ type: "SET_EXTRA_PATTERNS", extraPatterns: extraPatterns })
    scan()
  }

  onMicInUseChanged: scan()

  Connections {
    target: root.service
    function onRecordingStarted(otid, sourceLabel) {
      root.dispatch({ type: "RECORDING_STARTED", otid: otid, sourceLabel: sourceLabel })
    }
    function onRecordingStartFailed(errorClass, message) {
      root.dispatch({ type: "RECORDING_START_FAILED", errorClass: errorClass, message: message })
    }
    function onRecordingStopped(url) {
      root.dispatch({ type: "RECORDING_STOPPED", url: url })
    }
    function onRecordingStopFailed(errorClass, message) {
      root.dispatch({ type: "RECORDING_STOP_FAILED", errorClass: errorClass, message: message })
    }

    function onPendingTimedOut() {
      root.dispatch({ type: "TIMEOUT" })
    }
  }

  Connections {
    target: ToplevelManager.toplevels
    ignoreUnknownSignals: true
    function onValuesChanged() { settle.restart() }
  }

  Timer {
    id: settle
    interval: 1200
    repeat: false
    onTriggered: root.scan()
  }

  // A leave is committed only against a fresh scan: the reducer is pure
  // and cannot see Wayland, so the dispatcher re-reads the world at the
  // moment of commitment. A meeting that came back inside the confirm
  // window clears leavePending in that scan and this dispatch no-ops --
  // the blink tolerance the window exists for.
  Timer {
    id: leaveConfirm
    interval: 3000
    repeat: false
    onTriggered: {
      scan()
      root.dispatch({ type: "LEAVE_CONFIRMED" })
    }
  }

  Timer {
    id: countTick
    interval: 1000
    repeat: true
    onTriggered: root.dispatch({ type: "TICK" })
  }

  // Same re-check at countdown fire: the meeting must still exist in the
  // world, not just in the reducer's possibly-stale copy of it. scan() is
  // synchronous, so active is current when EXPIRED decides.
  Timer {
    id: countdown
    interval: root.countdownSec * 1000
    repeat: false
    onTriggered: {
      scan()
      root.dispatch({ type: "COUNTDOWN_EXPIRED" })
    }
  }

  Timer {
    interval: 5000
    repeat: true
    running: root.mode !== "off"
    onTriggered: root.scan()
  }
}
