import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Otter in the Omarchy bar: one minimal glyph, and a dropdown with the recent
// conversations. Every color, font, radius and gap comes from the theme
// singletons -- nothing is hard-coded, so this re-themes with the desktop.
Panel {
  id: root
  moduleName: "io.github.dharmapurikar.otter"
  ipcTarget: "io.github.dharmapurikar.otter"
  manageIpc: false

  // "header" is the start-meeting action; "recent" is the conversation list;
  // "settings" is the mic picker and toggles, when revealed.
  property string focusSection: "recent"
  property int recentIndex: 0
  property int settingsIndex: 0
  property bool cursorActive: false

  // "main" is the list; "detail" is one conversation's transcript; "search"
  // is the query view; "settings" is audio and account. Only one is on
  // screen, so the panel never becomes a wall of everything at once.
  property string view: "main"
  readonly property bool settingsOpen: view === "settings"
  property int searchIndex: 0

  // Inputs for the silent-recording watchdog below.
  property int silentSec: 0
  property bool silenceWarned: false

  function showDetail(item) {
    if (!item) return
    otter.loadTranscript(item)
    view = "detail"
    if (panelFlick) panelFlick.contentY = 0
  }

  function showSearch() {
    view = "search"
    searchIndex = 0
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function () { if (searchField) searchField.forceActiveFocus() })
  }

  function backToMain() {
    view = "main"
    confirmSignOut = false
    if (focusSection === "settings")
      focusSection = otter.recent.length > 0 ? "recent" : "header"
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }

  function selectedSearchItem() {
    if (otter.searchResults.length === 0) return null
    return otter.searchResults[Math.max(0, Math.min(searchIndex, otter.searchResults.length - 1))]
  }

  // The settings list, flattened so one index can walk it: the system-default
  // row, then each device, then the three toggles.
  readonly property int settingsCount: otter.devices.length + 5
  property bool confirmSignOut: false

  function settingsRowCount() { return settingsCount }

  function activateSettingsRow(i) {
    if (i === 0) { otter.selectMic(""); return }
    if (i <= otter.devices.length) {
      var dev = otter.devices[i - 1]
      if (dev) otter.selectMic(dev.name)
      return
    }
    if (i === otter.devices.length + 1) {
      otter.setCaptureSystemAudio(!otter.captureSystemAudio)
      return
    }
    if (i === otter.devices.length + 2) {
      otter.setShowMeter(!otter.showMeter)
      return
    }
    if (i === otter.devices.length + 3) {
      otter.setMeetingAutoRecord(!otter.setting("meetingAutoRecord", true))
      return
    }
    signOut()
  }

  // Two steps, because this ends the session the browser shares -- there is
  // no plugin-only session to drop, so a stray click would sign you out of
  // otter.ai too.
  function signOut() {
    if (!confirmSignOut) { confirmSignOut = true; return }
    confirmSignOut = false
    otter.logout()
    backToMain()
  }

  function showSettings() {
    otter.listDevices()
    confirmSignOut = false
    view = "settings"
    focusSection = "settings"
    settingsIndex = 0
    if (panelFlick) panelFlick.contentY = 0
  }

  function toggleSettings() {
    if (view === "settings") backToMain()
    else showSettings()
  }

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool barVertical: bar ? bar.vertical : false

  // One source of truth for the mark, shared by the bar label and the hero.
  // See OtterIcon.qml for why this glyph.
  readonly property string otterGlyph: "󰗋"

  readonly property bool recording: otter.recording
  // bar.urgent is the theme's bar-active role; barForeground is the animated
  // idle color the bar already manages.
  readonly property color barIconColor: recording ? root.urgent : barForeground

  function elapsedText(seconds) {
    var s = Math.max(0, Math.floor(seconds))
    var m = Math.floor(s / 60)
    var h = Math.floor(m / 60)
    var two = function (n) { return n < 10 ? "0" + n : String(n) }
    if (h > 0) return h + ":" + two(m % 60) + ":" + two(s % 60)
    return m + ":" + two(s % 60)
  }

  // The flickable and the panel size to whichever view is on screen.
  readonly property real activeContentHeight: {
    if (view === "settings") return settingsColumn ? settingsColumn.implicitHeight : 0
    if (view === "detail") return detailColumn ? detailColumn.implicitHeight : 0
    if (view === "search") return searchColumn ? searchColumn.implicitHeight : 0
    return column ? column.implicitHeight : 0
  }

  readonly property bool signedOut: otter.everLoaded && !otter.authenticated
  readonly property bool headerHasCursor: cursorActive && focusSection === "header"

  function ensureCursor() {
    if (signedOut) {
      focusSection = "header"
      recentIndex = 0
      return
    }
    if (view === "settings") {
      focusSection = "settings"
      if (settingsIndex >= settingsRowCount())
        settingsIndex = Math.max(0, settingsRowCount() - 1)
      return
    }
    if (focusSection === "settings") focusSection = "recent"
    if (otter.recent.length === 0 && focusSection === "recent") {
      focusSection = "header"
      recentIndex = 0
      return
    }
    if (focusSection !== "recent" && focusSection !== "header") focusSection = "recent"
    recentIndex = Math.max(0, Math.min(recentIndex, otter.recent.length - 1))
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    ensureCursor()
    if (dy === 0) return

    // Settings is its own view, so its cursor stays inside it.
    if (view === "settings") {
      settingsIndex = Math.max(0, Math.min(settingsRowCount() - 1, settingsIndex + dy))
      return
    }
    if (focusSection === "header") {
      if (dy > 0 && otter.recent.length > 0) {
        focusSection = "recent"
        recentIndex = 0
        scrollCursorIntoView()
      }
      return
    }
    if (view === "search") {
      if (otter.searchResults.length === 0) return
      searchIndex = Math.max(0, Math.min(otter.searchResults.length - 1, searchIndex + dy))
      return
    }
    if (focusSection === "recent") {
      if (dy < 0 && recentIndex === 0) { setHeaderCursor(); return }
      recentIndex = Math.max(0, Math.min(otter.recent.length - 1, recentIndex + dy))
      scrollCursorIntoView()
    }
  }

  function setHeaderCursor() {
    cursorActive = true
    focusSection = "header"
    if (panelFlick) panelFlick.contentY = 0
  }

  function setRecentCursor(index) {
    cursorActive = true
    focusSection = "recent"
    recentIndex = index
    scrollCursorIntoView()
  }

  function selectedItem() {
    if (otter.recent.length === 0) return null
    return otter.recent[Math.max(0, Math.min(recentIndex, otter.recent.length - 1))]
  }

  function activateCursor() {
    if (view === "settings") { activateSettingsRow(settingsIndex); return }
    if (view === "search") { root.showDetail(selectedSearchItem()); return }
    if (view === "detail") return
    ensureCursor()
    if (signedOut) { otter.openSignIn(); return }
    if (focusSection === "header") otter.toggleRecording()
    else if (focusSection === "recent") root.showDetail(selectedItem())
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item) return
    Qt.callLater(function () {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var viewTop = panelFlick.contentY
      var viewBottom = viewTop + panelFlick.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < viewTop + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > viewBottom - margin) panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function scrollCursorIntoView() {
    if (focusSection === "recent" && recentColumn
        && recentIndex >= 0 && recentIndex < recentColumn.children.length) {
      scrollItemIntoView(recentColumn.children[recentIndex])
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    if (panelFlick) panelFlick.contentY = 0
    otter.refresh()
    otter.listDevices()
    if (view !== "main") backToMain()
    Qt.callLater(function () { keyCatcher.forceActiveFocus() })
  }
  onRecentIndexChanged: scrollCursorIntoView()

  Service {
    id: otter
    settings: root.settings
    moduleName: root.moduleName
    shell: root.bar ? root.bar.shell : null
    // Resolve this plugin's own directory so the helper can be found wherever
    // the plugin was installed.
    pluginDir: {
      var u = String(Qt.resolvedUrl("."))
      return u.replace(/^file:\/\//, "").replace(/\/+$/, "")
    }
  }

  Connections {
    target: otter
    function onRefreshed() { root.ensureCursor() }
  }

  MeetingWatch {
    id: meetingWatch
    mode: String(root.setting("meetingDetection", "on")) === "off"
      ? "off"
      : (root.setting("meetingAutoRecord", true) === false ? "notify" : "countdown")
    extraPatterns: String(root.setting("meetingPatterns", ""))
    recording: otter.recording || otter.finishing
    service: otter
    onStartRequested: otter.startRecording()
    onStopRequested: otter.stopRecording()
  }

  // The last-ditch guard for a leave signal that never arrives -- a call
  // app holding its microphone capture open after the meeting ended. If a
  // recording produces no mic *or* system audio for five minutes, say so
  // exactly once. It never stops by itself: a genuinely quiet meeting is
  // legal, so the choice stays with the person at the keyboard.
  Timer {
    id: silenceWatchdog
    interval: 1000
    repeat: true
    running: otter.recording && !otter.finishing
    onTriggered: {
      if (otter.lastRms < 0.004) {
        root.silentSec += 1
        if (root.silentSec >= 300 && !root.silenceWarned) {
          root.silenceWarned = true
          otter.note("silent_warning", { silentSec: root.silentSec,
                                         lastRms: otter.lastRms })
          Quickshell.execDetached([
            "omarchy-notification-send", "--app-name", "Otter",
            "-g", "󰗋", "-u", "normal",
            "Still recording, but silent",
            "No microphone or meeting audio for 5 minutes — click to stop.",
            "--exec", "omarchy-shell", "io.github.dharmapurikar.otter", "stop"
          ])
        }
      } else {
        root.silentSec = 0
      }
    }
    onRunningChanged: if (!running) {
      root.silentSec = 0
      root.silenceWarned = false
    }
  }



  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { otter.refresh(); return "ok" }
    // Bind these for push-to-record without touching the mouse, e.g.
    //   bindd = SUPER ALT, R, Otter record, exec, omarchy-shell io.github.dharmapurikar.otter record
    function record(): string { otter.startRecording(); return "ok" }
    function stop(): string { otter.stopRecording(); return "ok" }
    function recordToggle(): string { otter.toggleRecording(); return "ok" }
    // The countdown veto: clicking a countdown toast calls this.
    function cancelCountdown(): string {
      var was = meetingWatch.cancelCountdown(true)
      return was === "none" ? "no countdown running" : "cancelled " + was
    }
    function status(): string {
      if (otter.recording)
        return "recording " + root.elapsedText(otter.elapsedSec)
          + (otter.sourceLabel !== "" ? " (" + otter.sourceLabel + ")" : "")
      return otter.authenticated
        ? "signed in, " + otter.recent.length + " recent"
        : (otter.lastError !== "" ? otter.lastError : "signed out")
    }
    function settings(): string { root.toggleSettings(); return root.settingsOpen ? "open" : "closed" }
    // Scriptable equivalents of the picker, handy for keybindings and for
    // pinning a device without opening the panel. An empty name means
    // "follow the system default".
    function setMic(name: string): string {
      otter.selectMic(name)
      return otter.micLabel
    }
    function devices(): string {
      otter.listDevices()
      return JSON.stringify(otter.devices)
    }
    // Everything the helper knows about itself, for debugging a dead daemon.
    function diagnostics(): string {
      return JSON.stringify({
        helper: otter.helperPath,
        daemonAlive: otter.daemonRunning,
        stdinMode: otter.stdinMode,
        commandable: otter.commandable,
        authenticated: otter.authenticated,
        recording: otter.recording,
        finishing: otter.finishing,
        recent: otter.recent.length,
        devices: otter.devices.length,
        micDevice: otter.micDevice === "" ? "(system default)" : otter.micDevice,
        micLabel: otter.micLabel,
        captureSystemAudio: otter.captureSystemAudio,
        showMeter: otter.showMeter,
        error: otter.lastError
      })
    }
  }

  // WidgetButton rather than BarIconButton: the latter pins a fixed icon slot,
  // which would clip the elapsed timer. This auto-sizes to its label.
  //
  // Minimal by design: idle is one glyph. Recording adds only the timer, and
  // the glyph switches to the theme's bar-active color -- no dot, no badge.
  // A running clock in the urgent color is already unmistakable, and an
  // accidentally-live recording must never be easy to miss.
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    active: root.recording
    dimmed: !otter.authenticated && otter.everLoaded
    text: (root.recording && !barVertical)
      ? root.otterGlyph + "  " + root.elapsedText(otter.elapsedSec)
      : root.otterGlyph
    tooltipText: {
      if (root.recording) {
        var label = otter.sourceLabel !== "" ? " · " + otter.sourceLabel : ""
        return "Recording " + root.elapsedText(otter.elapsedSec) + label
      }
      if (!otter.authenticated && otter.everLoaded) return "Otter · not signed in"
      return "Otter"
    }
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.RightButton) otter.refresh()
      else if (buttonCode === Qt.MiddleButton) otter.toggleRecording()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(root.activeContentHeight, Style.space(460))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function (dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: {
        // Esc unwinds one level: any sub-view back to main, then close.
        if (root.view !== "main") root.backToMain()
        else root.close()
      }
      onTabRequested: function (direction) { root.switchPanel(direction) }
      onTextKey: function (t) {
        if (root.view === "detail") {
          if (t === "m" || t === "M") otter.summarize()
          else if (t === "o" || t === "O") otter.openUrl("https://otter.ai/u/" + otter.detailOtid)
          return
        }
        if (root.view === "settings") return
        if (root.view !== "main") return
        if (t === "r" || t === "R") otter.refresh()
        else if (t === "s" || t === "S") root.showSettings()
        else if (t === "/") root.showSearch()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: root.activeContentHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          visible: root.view === "main"
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            title: "Otter"
            meta: {
              if (root.signedOut) return "Waiting for sign-in"
              if (otter.recording) {
                var label = otter.sourceLabel !== "" ? otter.sourceLabel : "recording"
                return root.elapsedText(otter.elapsedSec) + " · " + label
              }
              if (!otter.everLoaded) return "Loading…"
              return "Ready"
            }
            foreground: otter.recording ? root.urgent : root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              OtterIcon {
                iconSize: Style.font.display
                glyph: root.otterGlyph
                color: otter.recording ? root.urgent : root.foreground
              }
            }

            // Settings live behind this rather than always on screen: the
            // default view stays the minimal one the brief asked for.
            trailingControl: Component {
              Row {
                spacing: Style.space(2)

                PanelActionButton {
                  iconText: "󰍉"
                  tooltipText: "Search conversations  (/)"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: root.showSearch()
                }

                PanelActionButton {
                  iconText: "󰒓"
                  tooltipText: root.settingsOpen ? "Hide settings" : "Audio settings"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: root.toggleSettings()
                }
              }
            }
          }

          // Level meter. Only while recording, only if enabled, and it simply
          // isn't rendered when no level frames are arriving -- no
          // placeholder, no error.
          Item {
            visible: otter.recording && otter.showMeter
            width: parent.width
            implicitHeight: Style.space(4)

            Rectangle {
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              height: Style.space(3)
              radius: height / 2
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)

              Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: parent.width * Math.max(0, Math.min(1, otter.level))
                radius: parent.radius
                color: root.urgent
                Behavior on width { NumberAnimation { duration: 90 } }
              }
            }
          }

          // ------------------------------------------------- live transcript
          Column {
            visible: otter.recording && (otter.liveText !== "" || otter.liveError !== "")
            width: parent.width
            spacing: Style.space(4)

            PanelSectionHeader {
              text: otter.liveConnected ? "LIVE" : "LIVE (reconnecting)"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              visible: otter.liveError !== "" && otter.liveText === ""
              text: otter.liveError
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            // Only the tail: this is a glance while you talk, not a reading
            // view. The full text is one click away in the detail view.
            Text {
              textFormat: Text.PlainText
              width: parent.width
              visible: otter.liveText !== ""
              text: otter.liveText.length > 400
                    ? "…" + otter.liveText.substring(otter.liveText.length - 400)
                    : otter.liveText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }
          }

          // Upload backlog. Silent unless we're actually behind, which is the
          // only time it tells you anything: the recording is safe on disk and
          // still catching up.
          Text {
            textFormat: Text.PlainText
            visible: otter.recording && otter.backlogSec > 5
            width: parent.width
            text: "Uploading… " + Math.round(otter.backlogSec) + "s behind"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // Errors and the signed-out hint share one line so the panel never
          // grows a permanently empty slot.
          // Errors only. Being signed out is not one -- the sign-in row below
          // already says so, and repeating it in red made a normal state look
          // like a failure. A real fault (locked keyring, network) still shows.
          Text {
            textFormat: Text.PlainText
            visible: otter.lastError !== "" && !otter.needsLogin
            width: parent.width
            text: otter.lastError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          SignInRow {
            visible: root.signedOut
            width: parent.width
          }

          StartMeetingRow {
            visible: !root.signedOut
            width: parent.width
          }

          PanelSeparator {
            visible: !root.signedOut
            foreground: root.foreground
          }

          Column {
            visible: !root.signedOut
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "RECENT"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              textFormat: Text.PlainText
              visible: otter.recent.length === 0
              width: parent.width
              text: otter.everLoaded ? "No conversations yet." : "Loading…"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              horizontalAlignment: Text.AlignHCenter
            }

            Column {
              id: recentColumn
              visible: otter.recent.length > 0
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: otter.recent
                MeetingRow {
                  required property var modelData
                  required property int index
                  width: recentColumn.width
                  item: modelData
                  rowIndex: index
                }
              }
            }
          }
        }

        // ------------------------------------------------ settings view
        Column {
          id: settingsColumn
          visible: root.view === "settings"
          width: panelFlick.width
          spacing: Style.space(10)

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            PanelActionButton {
              iconText: "󰅁"
              tooltipText: "Back"
              foreground: root.foreground
              fontFamily: root.fontFamily
              Layout.alignment: Qt.AlignVCenter
              onClicked: root.backToMain()
            }

            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              text: "Settings"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }
          }

          Column {
            id: settingsBody
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: "MICROPHONE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            MicRow {
              width: parent.width
              rowIndex: 0
              label: "System default"
              detail: {
                for (var i = 0; i < otter.devices.length; i++) {
                  if (otter.devices[i] && otter.devices[i].isDefault === true)
                    return String(otter.devices[i].label || "")
                }
                return "Follows Omarchy's audio setting"
              }
              selected: otter.micDevice === ""
            }

            Repeater {
              model: otter.devices
              MicRow {
                required property var modelData
                required property int index
                width: settingsBody.width
                rowIndex: index + 1
                label: String(modelData.label || modelData.name || "")
                detail: modelData.isDefault === true ? "currently the system default" : ""
                selected: otter.micDevice === String(modelData.name || "")
              }
            }

            Text {
              textFormat: Text.PlainText
              visible: otter.devices.length === 0
              width: parent.width
              text: "No input devices found."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Toggle {
              width: parent.width
              label: "Capture system audio"
              description: "Transcribe the other side of a meeting too"
              checked: otter.captureSystemAudio
              foreground: root.foreground
              fontFamily: root.fontFamily
              hasCursor: root.cursorActive && root.focusSection === "settings"
                         && root.settingsIndex === otter.devices.length + 1
              onHovered: function (on) {
                if (on) { root.cursorActive = true; root.focusSection = "settings"
                          root.settingsIndex = otter.devices.length + 1 }
              }
              onClicked: otter.setCaptureSystemAudio(!otter.captureSystemAudio)
            }

            Toggle {
              width: parent.width
              label: "Show level meter"
              description: "Input level while recording"
              checked: otter.showMeter
              foreground: root.foreground
              fontFamily: root.fontFamily
              hasCursor: root.cursorActive && root.focusSection === "settings"
                         && root.settingsIndex === otter.devices.length + 2
              onHovered: function (on) {
                if (on) { root.cursorActive = true; root.focusSection = "settings"
                          root.settingsIndex = otter.devices.length + 2 }
              }
              onClicked: otter.setShowMeter(!otter.showMeter)
            }
          }

          PanelSeparator { foreground: root.foreground }

          PanelSectionHeader {
            text: "MEETINGS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Toggle {
            width: parent.width
            label: "Start and stop recording automatically"
            description: "5-second countdown you can cancel; off means click-to-act"
            checked: otter.setting("meetingAutoRecord", true) !== false
            foreground: root.foreground
            fontFamily: root.fontFamily
            hasCursor: root.cursorActive && root.focusSection === "settings"
                       && root.settingsIndex === otter.devices.length + 3
            onHovered: function (on) {
              if (on) { root.cursorActive = true; root.focusSection = "settings"
                        root.settingsIndex = otter.devices.length + 3 }
            }
            onClicked: otter.setMeetingAutoRecord(
                         otter.setting("meetingAutoRecord", true) === false)
          }

          PanelSeparator { foreground: root.foreground }

          PanelSectionHeader {
            text: "ACCOUNT"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          SignOutRow { width: parent.width }
        }

        // ------------------------------------------------ detail view
        Column {
          id: detailColumn
          visible: root.view === "detail"
          width: panelFlick.width
          spacing: Style.space(10)

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            PanelActionButton {
              iconText: "󰅁"
              tooltipText: "Back"
              foreground: root.foreground
              fontFamily: root.fontFamily
              Layout.alignment: Qt.AlignVCenter
              onClicked: root.backToMain()
            }

            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              text: otter.detailTitle
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }

            PanelActionButton {
              iconText: "󰊪"
              tooltipText: "Summarize with Otter's AI  (m)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !otter.summaryLoading && otter.transcriptText !== ""
              Layout.alignment: Qt.AlignVCenter
              onClicked: otter.summarize()
            }

            PanelActionButton {
              iconText: "󰈈"
              tooltipText: "Open in Otter  (o)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              Layout.alignment: Qt.AlignVCenter
              onClicked: otter.openUrl("https://otter.ai/u/" + otter.detailOtid)
            }
          }

          // Summary sits above the transcript: it is the thing you came for,
          // and burying it under a wall of text would defeat the point.
          Column {
            visible: otter.summaryLoading || otter.summaryText !== "" || otter.summaryError !== ""
            width: parent.width
            spacing: Style.space(4)

            PanelSectionHeader {
              text: "SUMMARY"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              visible: otter.summaryLoading || otter.summaryError !== ""
              text: otter.summaryLoading ? "Asking Otter's AI…" : otter.summaryError
              color: otter.summaryError !== "" ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              visible: otter.summaryText !== ""
              text: otter.summaryText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }
          }

          PanelSeparator {
            visible: otter.summaryText !== ""
            foreground: root.foreground
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: otter.transcriptLoading || otter.transcriptError !== ""
                     || otter.transcriptText.trim() === ""
            text: {
              if (otter.transcriptLoading) return "Loading transcript…"
              if (otter.transcriptError !== "") return otter.transcriptError
              return "No transcript yet — Otter may still be processing this one."
            }
            color: otter.transcriptError !== "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          // A read-only TextEdit rather than a Text: transcripts exist to be
          // quoted, and Text cannot be selected.
          TextEdit {
            width: parent.width
            visible: otter.transcriptText.trim() !== ""
            text: otter.transcriptText
            readOnly: true
            selectByMouse: true
            textFormat: TextEdit.PlainText
            wrapMode: TextEdit.WordWrap
            color: root.foreground
            selectionColor: Style.selectionFillFor(root.foreground, Color.accent)
            selectedTextColor: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }

        // ------------------------------------------------ search view
        Column {
          id: searchColumn
          visible: root.view === "search"
          width: panelFlick.width
          spacing: Style.space(10)

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            PanelActionButton {
              iconText: "󰅁"
              tooltipText: "Back"
              foreground: root.foreground
              fontFamily: root.fontFamily
              Layout.alignment: Qt.AlignVCenter
              onClicked: { otter.clearSearch(); root.backToMain() }
            }

            TextField {
              id: searchField
              Layout.fillWidth: true
              placeholderText: "Search your conversations…"
              foreground: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              onAccepted: otter.runSearch(text)
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: otter.searchLoading || otter.searchError !== ""
            text: otter.searchLoading ? "Searching…" : otter.searchError
            color: otter.searchError !== "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: otter.searched && !otter.searchLoading
                     && otter.searchResults.length === 0 && otter.searchError === ""
            text: "Nothing matched."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: !otter.searched && !otter.searchLoading
            text: "Type a phrase, or an email address to find a participant's meetings."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Column {
            id: searchResultColumn
            width: parent.width
            spacing: Style.space(6)

            Repeater {
              model: otter.searchResults
              MeetingRow {
                required property var modelData
                required property int index
                width: searchResultColumn.width
                item: modelData
                rowIndex: index
                inSearch: true
              }
            }
          }
        }
      }
    }
  }

  // Clicking sign-in means a login is about to happen elsewhere, so check
  // back for a few seconds rather than waiting on the helper's own cadence.
  Timer {
    id: signInPoll
    interval: 2000
    repeat: true
    running: false
    property int ticks: 0
    onTriggered: {
      ticks += 1
      if (otter.authenticated || ticks > 30) { ticks = 0; running = false; return }
      otter.refresh()
    }
    onRunningChanged: if (running) ticks = 0
  }

  // ----------------------------------------------------------- components

  component SignInRow: CursorSurface {
    id: signInRow
    hasCursor: root.cursorActive && root.focusSection === "header"
    foreground: root.foreground
    implicitHeight: signInContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setHeaderCursor()
      onClicked: { otter.openSignIn(); signInPoll.start() }
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      ColumnLayout {
        id: signInContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: "Sign in to Otter"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: "Opens otter.ai — the panel picks the session up by itself"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      PanelActionButton {
        iconText: "󰍂"
        foreground: root.foreground
        fontFamily: root.fontFamily
        Layout.alignment: Qt.AlignVCenter
        onClicked: otter.openSignIn()
      }
    }
  }

  // The start/stop control: one row that swaps meaning with the recording
  // state, so there is never both a start and a stop to choose between.
  component StartMeetingRow: CursorSurface {
    id: startRow
    readonly property bool enabled: !otter.pending

    hasCursor: root.cursorActive && root.focusSection === "header"
    foreground: otter.recording ? root.urgent : root.foreground
    opacity: startRow.enabled ? 1.0 : 0.55
    implicitHeight: startContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: startRow.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      enabled: startRow.enabled
      onEntered: root.setHeaderCursor()
      onClicked: otter.toggleRecording()
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: otter.recording ? "󰙦" : "󰑊"
        color: otter.recording ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: startContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: {
            if (otter.finishing) return "Finishing…"
            if (otter.pending) return otter.recording ? "Stopping…" : "Starting…"
            return otter.recording ? "Stop recording" : "Start meeting"
          }
          color: otter.recording ? root.urgent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          visible: text !== ""
          text: {
            if (otter.finishing) {
              return otter.finishingLeftSec > 0.3
                ? "Uploading the last " + otter.finishingLeftSec.toFixed(1) + "s to Otter"
                : "Finishing up with Otter"
            }
            if (otter.recording) return root.elapsedText(otter.elapsedSec)
            var mic = otter.micLabel
            return otter.captureSystemAudio ? mic + " + system audio" : mic
          }
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      // Jump to the live conversation in Otter while it records.
      PanelActionButton {
        visible: otter.recording && otter.recordingOtid !== ""
        iconText: "󰈈"
        tooltipText: "Open in Otter"
        foreground: root.foreground
        fontFamily: root.fontFamily
        Layout.alignment: Qt.AlignVCenter
        onClicked: otter.openCurrent()
      }
    }
  }

  // A selectable microphone. Radio-style: exactly one is in effect, and the
  // marker shows which, so "System default" reads as a real choice rather
  // than an absence of one.
  component SignOutRow: CursorSurface {
    id: signOutRow
    readonly property int rowIndex: otter.devices.length + 4

    hasCursor: root.view === "settings" && root.settingsIndex === signOutRow.rowIndex
    foreground: root.confirmSignOut ? root.urgent : root.foreground
    implicitHeight: signOutContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: { root.settingsIndex = signOutRow.rowIndex; root.cursorActive = true }
      onClicked: root.signOut()
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: "󰍃"
        color: root.confirmSignOut ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: signOutContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: root.confirmSignOut ? "Sign out — click again to confirm" : "Sign out of Otter"
          color: root.confirmSignOut ? root.urgent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          // Not a detail to bury: there is no plugin-only session, so this
          // ends the one the browser is using as well.
          text: "Also signs out otter.ai in your browser"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component MicRow: CursorSurface {
    id: micRow
    property int rowIndex: 0
    property string label: ""
    property string detail: ""
    property bool selected: false

    hasCursor: root.cursorActive && root.focusSection === "settings"
               && root.settingsIndex === micRow.rowIndex
    foreground: root.foreground
    implicitHeight: micContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: {
        root.cursorActive = true
        root.focusSection = "settings"
        root.settingsIndex = micRow.rowIndex
      }
      onClicked: root.activateSettingsRow(micRow.rowIndex)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: micRow.selected ? "󰄬" : ""
        color: root.foreground
        opacity: micRow.selected ? 1.0 : 0.0
        font.family: root.fontFamily
        font.pixelSize: Style.font.iconSmall
        Layout.alignment: Qt.AlignVCenter
        Layout.preferredWidth: Style.space(12)
      }

      ColumnLayout {
        id: micContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: micRow.label
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: micRow.selected
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          visible: micRow.detail !== ""
          text: micRow.detail
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }

  component MeetingRow: CursorSurface {
    id: meetingRow
    property var item: null
    property int rowIndex: 0
    property bool inSearch: false

    hasCursor: meetingRow.inSearch
      ? (root.view === "search" && root.searchIndex === rowIndex)
      : (root.cursorActive && root.focusSection === "recent" && root.recentIndex === rowIndex)
    foreground: root.foreground
    implicitHeight: meetingContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: {
        if (meetingRow.inSearch) root.searchIndex = meetingRow.rowIndex
        else root.setRecentCursor(meetingRow.rowIndex)
      }
      onClicked: root.showDetail(meetingRow.item)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      ColumnLayout {
        id: meetingContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.titleText(meetingRow.item ? meetingRow.item.title : "")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          visible: text !== ""
          text: {
            if (!meetingRow.item) return ""
            var d = Model.durationText(meetingRow.item.duration)
            var day = Model.dayText(meetingRow.item.startTime)
            if (d !== "" && day !== "") return d + " · " + day
            return d !== "" ? d : day
          }
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Text {
        textFormat: Text.PlainText
        text: "󰁔"
        color: root.dim
        opacity: meetingRow.hasCursor ? 1.0 : 0.0
        font.family: root.fontFamily
        font.pixelSize: Style.font.iconSmall
        Layout.alignment: Qt.AlignVCenter
      }
    }
  }
}
