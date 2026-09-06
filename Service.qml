import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Pipewire
import qs.Commons
import "Model.js" as Model

// Otter state for the bar widget.
//
// Owns one long-lived helper process and nothing else: no network, no
// cookies, no audio here, so a hang or crash in any of that can never take
// down the bar. Commands go out on the helper's stdin as JSON lines; state
// comes back on stdout the same way. See docs/ARCHITECTURE.md.
Item {
  id: root

  property var settings: ({})
  // Directory this plugin was loaded from. A third-party plugin can't use
  // OMARCHY_PATH (that's the shell's own tree), so resolve our own location.
  property string pluginDir: ""
  // The shell root and our own widget id, needed only to persist settings.
  property QtObject shell: null
  property string moduleName: ""

  property bool authenticated: false
  property bool everLoaded: false
  property var recent: []
  property string lastError: ""
  property string lastErrorClass: ""

  property bool recording: false
  property string recordingOtid: ""
  property int elapsedSec: 0
  property real backlogSec: 0
  property string sourceLabel: ""
  // Raw input RMS, independent of the showMeter setting: the
  // silent-recording watchdog must keep working with the meter hidden.
  property real lastRms: 0
  property real level: 0
  // Daemon restart bookkeeping, reported through note() on the next start.
  property bool _sawDaemonExit: false
  property int _lastExitCode: 0
  property var devices: []

  // True from clicking start until the helper confirms, so the button can't
  // be double-fired while speech_start is in flight.
  property bool pending: false
  // Capture has ended but the upload is still finalizing.
  property bool finishing: false
  property real finishingLeftSec: 0

  // Diagnostics from the helper: what its stdin actually is, and whether it
  // can therefore accept start/stop at all.
  property string stdinMode: ""
  property bool commandable: false
  // Signed out is a state, not a fault: the panel shows a sign-in row for it
  // rather than red error text.
  property bool needsLogin: false

  // Devices are fetched once per daemon lifetime, not polled: the idle
  // summary wants to name the mic in words rather than show a PipeWire id.
  property bool _devicesRequested: false

  readonly property string helperPath: pluginDir === ""
    ? "" : pluginDir + "/helper/otter.py"
  readonly property bool busy: pending
  readonly property bool daemonRunning: daemon.running

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  readonly property int recentCount: intSetting("recentCount", 3, 1, 6)
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 300, 30, 3600)
  readonly property string chromeProfile: String(setting("chromeProfile", "Default"))
  readonly property string browser: String(setting("browser", "chrome"))
  readonly property string micDevice: String(setting("micDevice", ""))
  readonly property bool captureSystemAudio: setting("captureSystemAudio", true) !== false
  readonly property bool showMeter: setting("showMeter", true) !== false

  signal refreshed()
  signal pendingTimedOut()
  signal recordingStarted(string otid, string sourceLabel)
  signal recordingStartFailed(string errorClass, string message)
  signal recordingStopped(string url)
  signal recordingStopFailed(string errorClass, string message)

  // ------------------------------------------------------------- commands

  function send(obj) {
    if (!daemon.running) return false
    daemon.write(JSON.stringify(obj) + "\n")
    return true
  }

  // Shell-side decision points (meeting edges, countdowns), recorded
  // through the helper's events log so one file reconstructs an incident
  // end to end -- the join that fired, the leave that was confirmed, the
  // countdown that was cancelled, and by what. Best effort by design:
  // logging must never be why a stop or start goes missing.
  function note(event, fields) {
    var obj = { cmd: "note", event: event }
    for (var k in fields || {}) obj[k] = fields[k]
    send(obj)
  }

  function refresh() { send({ cmd: "refresh" }) }
  function logout() { send({ cmd: "logout" }) }
  function listDevices() { send({ cmd: "devices" }) }

  // ---- transcript / search / summary -----------------------------------
  //
  // Each reply carries the otid or query it answers, so a slow response can
  // never overwrite the view you have since moved to.

  property string detailOtid: ""
  property string detailTitle: ""
  property string transcriptText: ""
  property bool transcriptLoading: false
  property string transcriptError: ""

  property string summaryText: ""
  property bool summaryLoading: false
  property string summaryError: ""

  property string searchQuery: ""
  property var searchResults: []
  property bool searchLoading: false
  property string searchError: ""
  property bool searched: false

  // Live transcript while recording. Optional by design: an absent live feed
  // never affects whether the recording itself works.
  property string liveText: ""
  property bool liveConnected: false
  property string liveError: ""
  // Apps currently holding a microphone capture ({name, binary}), the
  // meeting join/leave signal MeetingWatch correlates with its window
  // matching. The helper excludes our own ffmpeg captures and level
  // peekers (the shell's audio panel), so neither recording nor adjusting
  // volume can look like a call.
  property var micCaptures: []
  readonly property bool micInUseByOther: micCaptures.length > 0


  function loadTranscript(item) {
    if (!item) return
    detailOtid = String(item.otid || "")
    detailTitle = Model.titleText(item.title)
    transcriptText = ""
    transcriptError = ""
    summaryText = ""
    summaryError = ""
    summaryLoading = false
    if (detailOtid === "") return
    transcriptLoading = true
    send({ cmd: "transcript", otid: detailOtid })
  }

  function summarize() {
    if (detailOtid === "" || summaryLoading) return
    summaryLoading = true
    summaryText = ""
    summaryError = ""
    send({ cmd: "summarize", otid: detailOtid })
  }

  function runSearch(query) {
    var q = String(query || "").trim()
    searchQuery = q
    searchError = ""
    if (q === "") { searchResults = []; searched = false; return }
    searchLoading = true
    searched = true
    send({ cmd: "search", query: q, size: 10 })
  }

  function clearSearch() {
    searchQuery = ""
    searchResults = []
    searchError = ""
    searchLoading = false
    searched = false
  }

  // Persist one inline setting into shell.json, the same way Omarchy's own
  // clock, tray, power and tailscale widgets do it. The whole entry is
  // rewritten because updateEntryInline replaces it, so existing keys have to
  // be carried across or they are silently dropped.
  function setSetting(key, value) {
    if (!shell || typeof shell.updateEntryInline !== "function") {
      lastError = "Cannot save settings: shell API unavailable"
      return false
    }
    var entry = { id: moduleName }
    for (var k in settings) if (k !== "id") entry[k] = settings[k]
    entry[key] = value
    return shell.updateEntryInline(moduleName, entry)
  }

  function selectMic(name) { setSetting("micDevice", String(name || "")) }
  function setCaptureSystemAudio(on) { setSetting("captureSystemAudio", on === true) }
  function setShowMeter(on) { setSetting("showMeter", on === true) }
  function setMeetingAutoRecord(on) { setSetting("meetingAutoRecord", on === true) }

  // ---- which microphone is actually in play ------------------------------
  //
  // Read live from Pipewire rather than from the helper's device list. That
  // list is fetched once and its isDefault flag goes stale the moment you
  // change the system default elsewhere -- the recording still followed the
  // real default, but the panel kept naming the old device.
  readonly property var defaultSourceNode: Pipewire.defaultAudioSource
  readonly property string defaultSourceName:
    defaultSourceNode ? String(defaultSourceNode.name || "") : ""
  readonly property string defaultSourceLabel: {
    if (!defaultSourceNode) return ""
    return String(defaultSourceNode.description
                  || defaultSourceNode.nickname
                  || defaultSourceNode.name || "")
  }

  // A property, not a function: a binding cannot re-evaluate a function call
  // when the default source changes, so as a function this stayed stale on
  // screen even once the underlying value was right.
  readonly property string micLabel: {
    if (micDevice === "") {
      return defaultSourceLabel !== ""
        ? "System default · " + defaultSourceLabel
        : "System default"
    }
    for (var j = 0; j < devices.length; j++) {
      if (devices[j] && String(devices[j].name) === micDevice)
        return String(devices[j].label || micDevice)
    }
    return micDevice  // pinned to something not currently present
  }

  // Node properties only stay live while the node is tracked.
  PwObjectTracker {
    objects: root.defaultSourceNode ? [root.defaultSourceNode] : []
  }

  // A changed default also moves the tick in the picker, so re-read the list.
  onDefaultSourceNameChanged: if (daemon.running) listDevices()

  function startRecording() {
    if (recording || pending) return
    pending = true
    pendingTimeout.restart()
    if (!send({ cmd: "start", micDevice: micDevice, captureSystem: captureSystemAudio })) {
      pending = false
      lastError = "Otter helper is not running"
    }
  }

  // A stop requested while another command is still in flight is never
  // lost: the meeting watcher asks exactly once, at the moment the
  // meeting ends, so dropping it there meant a recording that ran until
  // someone noticed. The queue fires as soon as the pending command
  // resolves, and dies with the recording.
  property bool _stopQueued: false

  function stopRecording() {
    if (!recording) { _stopQueued = false; return }
    if (pending) { _stopQueued = true; note("stop_deferred"); return }
    pendingTimeout.restart()
    send({ cmd: "stop" })
  }

  function _resolveStopQueue() {
    if (_stopQueued && !pending && recording) {
      _stopQueued = false
      stopRecording()
    }
  }

  function toggleRecording() {
    if (recording) stopRecording()
    else startRecording()
  }

  // -------------------------------------------------------------- inbound

  function handleLine(line) {
    var msg = Model.parseStatus(line)
    if (!msg || typeof msg !== "object") return

    switch (msg.type) {
    case "state":
      everLoaded = true
      pending = false
      finishing = false
      finishingLeftSec = 0
      pendingTimeout.stop()
      // Ask for the device list once, so the idle summary can name the mic
      // in words instead of a PipeWire device id. One pactl call, not a poll.
      if (!_devicesRequested) { _devicesRequested = true; listDevices() }
      authenticated = msg.authenticated === true
      if (msg.stdinMode !== undefined) stdinMode = String(msg.stdinMode)
      if (msg.commandable !== undefined) commandable = msg.commandable === true
      if (msg.needsLogin !== undefined) needsLogin = msg.needsLogin === true
      recent = msg.recent || []
      lastError = Model.shortError(msg.error || "")
      recording = msg.recording === true
      recordingOtid = String(msg.otid || "")
      sourceLabel = String(msg.sourceLabel || "")
      if (msg.elapsed !== undefined) elapsedSec = Number(msg.elapsed) || 0
      if (!recording) {
        level = 0
        backlogSec = 0
        liveText = ""
        liveConnected = false
        liveError = ""
      }
      _resolveStopQueue()
      refreshed()
      break

    case "stopping":
      // The upload is still finalizing, but capture has ended: stop the
      // clock and drop the recording indicator now rather than leaving the
      // bar claiming to record for as long as the drain takes.
      recording = false
      finishing = true
      level = 0
      pending = true
      pendingTimeout.restart()
      break

    case "finishing":
      // Progress while the tail uploads. Keeps the pending timeout at bay so
      // a slow finalize can't trip the "did not respond" warning.
      recording = false
      finishing = true
      level = 0
      pending = true
      finishingLeftSec = Number(msg.remaining) || 0
      pendingTimeout.restart()
      break

    case "recording":
      recording = true
      pending = false
      pendingTimeout.stop()
      recordingOtid = String(msg.otid || recordingOtid)
      elapsedSec = Number(msg.elapsed) || 0
      backlogSec = Number(msg.backlog) || 0
      _resolveStopQueue()
      break

    case "level":
      lastRms = Math.max(0, Math.min(1, Number(msg.rms) || 0))
      // Display only when the meter is on, so an unused setting costs
      // nothing beyond the line itself.
      if (showMeter) level = lastRms
      break

    case "started":
      everLoaded = true
      recording = true
      pending = false
      finishing = false
      finishingLeftSec = 0
      pendingTimeout.stop()
      recordingOtid = String(msg.otid || "")
      sourceLabel = String(msg.sourceLabel || "")
      elapsedSec = Number(msg.elapsed) || 0
      lastError = ""
      lastErrorClass = ""
      _resolveStopQueue()
      recordingStarted(recordingOtid, sourceLabel)
      break

    case "start_failed":
      pending = false
      finishing = false
      finishingLeftSec = 0
      pendingTimeout.stop()
      recording = false
      lastErrorClass = String(msg.errorClass || "internal")
      lastError = Model.humanError(lastErrorClass, msg.message)
      recordingStartFailed(lastErrorClass, String(msg.message || ""))
      break

    case "stop_failed":
      pending = false
      finishing = false
      finishingLeftSec = 0
      pendingTimeout.stop()
      recording = false
      _stopQueued = false
      lastErrorClass = String(msg.errorClass || "internal")
      lastError = Model.humanError(lastErrorClass, msg.message)
      recordingStopFailed(lastErrorClass, String(msg.message || ""))
      break

    case "stopped":
      recording = false
      finishing = false
      finishingLeftSec = 0
      liveText = ""
      liveConnected = false
      liveError = ""
      pending = false
      _stopQueued = false
      pendingTimeout.stop()
      level = 0
      backlogSec = 0
      elapsedSec = 0
      recordingStopped(String(msg.url || ""))
      break

    case "devices":
      devices = msg.devices || []
      break

    case "transcript":
      // Ignore an answer to a request we have already navigated away from.
      if (String(msg.otid || "") !== detailOtid) break
      transcriptLoading = false
      transcriptText = String(msg.text || "")
      transcriptError = Model.shortError(msg.error || "")
      break

    case "summary":
      if (String(msg.otid || "") !== detailOtid) break
      summaryLoading = false
      summaryText = String(msg.text || "")
      summaryError = Model.shortError(msg.error || "")
      break

    case "call":
      micCaptures = msg.captures || []
      break

    case "live":
      liveText = String(msg.text || "")
      liveConnected = msg.connected === true
      liveError = Model.shortError(msg.error || "")
      break

    case "results":
      if (String(msg.query || "") !== searchQuery) break
      searchLoading = false
      searchResults = msg.results || []
      searchError = Model.shortError(msg.error || "")
      break

    case "error":
      pending = false
      finishing = false
      finishingLeftSec = 0
      pendingTimeout.stop()
      lastErrorClass = String(msg.errorClass || "")
      lastError = lastErrorClass !== ""
        ? Model.humanError(lastErrorClass, msg.message)
        : Model.shortError(msg.message || "")
      break
    }
  }

  // ----------------------------------------------------------------- open

  function openMeeting(item) {
    if (!item) return
    var url = String(item.url || "")
    if (url === "" && item.otid) url = "https://otter.ai/u/" + item.otid
    if (url !== "") openUrl(url)
  }

  function openCurrent() {
    if (recordingOtid !== "") openUrl("https://otter.ai/u/" + recordingOtid)
  }

  function openSignIn() { openUrl("https://otter.ai/signin") }

  // omarchy-launch-webapp opens it as an app window in the default browser,
  // which is the native-feeling path other Omarchy plugins use too.
  function openUrl(url) {
    Quickshell.execDetached(["omarchy-launch-webapp", url])
  }

  // ---------------------------------------------------------------- timers

  // The helper reports elapsed once a second; this keeps the readout moving
  // smoothly between those, and is resynced by every "recording" frame.
  Timer {
    interval: 1000
    repeat: true
    running: root.recording
    onTriggered: root.elapsedSec += 1
  }

  // A start/stop that never gets confirmed must not wedge the button.
  Timer {
    id: pendingTimeout
    interval: 15000
    repeat: false
    onTriggered: {
      if (root.pending) {
        root.pending = false
        root.finishing = false
        root.finishingLeftSec = 0
        root.lastError = "Otter did not respond; try again"
        // The meeting watch's starting/stopping expectation dies here
        // too, or its next confirmed frame toasts against a stale hope.
        root.pendingTimedOut()
      }
    }
  }

  // Restart the helper if it dies, backing off so a persistently broken
  // helper doesn't spin. `running` is forced false first: after the process
  // exits the property can still read true, and assigning the value it
  // already holds changes nothing -- which silently turned this into a
  // one-shot and left the daemon dead after its first exit.
  Timer {
    id: restartTimer
    property int attempts: 0
    interval: Math.min(60000, 2000 * Math.pow(2, Math.min(attempts, 5)))
    repeat: false
    onTriggered: {
      if (root.helperPath === "") return
      attempts += 1
      daemon.running = false
      daemon.running = true
    }
  }

  // A daemon that stays up for a while is working; stop escalating the delay.
  Timer {
    id: healthyTimer
    interval: 30000
    repeat: false
    running: false
    onTriggered: restartTimer.attempts = 0
  }

  Process {
    id: daemon
    running: false
    stdinEnabled: true
    command: root.helperPath === "" ? [] : [
      "python3", root.helperPath,
      "--browser", root.browser,
      "--profile", root.chromeProfile,
      "serve",
      "--n", String(root.recentCount),
      "--refresh", String(root.refreshIntervalSec)
    ]

    stdout: SplitParser {
      onRead: function (line) { root.handleLine(line) }
    }
    stderr: SplitParser {
      onRead: function (line) {
        // The helper keeps stdout clean for JSON; anything on stderr is a
        // real fault worth surfacing.
        var text = String(line).trim()
        if (text !== "") root.lastError = Model.shortError(text)
      }
    }

    onStarted: {
      healthyTimer.restart()
      root._devicesRequested = false
      // A previous exit is worth recording once the channel is back --
      // a daemon that died mid-incident otherwise leaves no trace here.
      if (root._sawDaemonExit)
        root.note("daemon_restarted",
                  { exitCode: root._lastExitCode,
                    attempt: restartTimer.attempts })
      root._sawDaemonExit = false
    }

    onExited: function (exitCode) {
      root.everLoaded = true
      root.pending = false
      root._sawDaemonExit = true
      root._lastExitCode = exitCode
      healthyTimer.stop()
      if (root.recording) {
        // The helper finalizes on shutdown; we just can't see the result.
        root.recording = false
        root.level = 0
        root.lastError = "Otter helper stopped while recording"
      }
      restartTimer.restart()
    }
  }

  // Start the daemon as soon as we know where the helper lives.
  onHelperPathChanged: if (helperPath !== "" && !daemon.running) daemon.running = true
  Component.onCompleted: if (helperPath !== "") daemon.running = true
}
