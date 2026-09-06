# Roadmap

Simple feature tracking. The first major release (**1.0.0**) is out: everything
under *Shipped* is built and verified end to end on real hardware.

## Shipped — 1.0.0

**The bar**
- One minimal otter glyph; while recording it grows an elapsed timer and takes
  the theme's active colour. No dot, no badge.
- Left-click opens the panel, right-click refreshes, middle-click toggles
  recording.

**Recording**
- Start/stop from the panel, a keybinding (`SUPER+ALT+R` toggles recording,
  `SUPER+ALT+O` the panel), or the bar glyph.
- Captures the microphone **and** system audio by default, so both sides of a
  meeting are transcribed. Mic-only is one toggle away.
- Live transcript streams into the panel while you talk.
- Optional input-level meter; upload progress shown only when actually behind.
- Crash-safe upload: audio is spooled to disk and resumed from the last
  acknowledged offset if the connection drops mid-recording.

**Conversations**
- Recent list in the panel (3 by default, up to 6), each opening to its full
  transcript in-place — selectable text for quoting.
- AI summaries through Otter's own LLM proxy.
- Search (`/`) across your recordings; an email address in the query finds
  that participant's meetings.
- Open any conversation at otter.ai in one click.

**Meetings**
- Detects meetings (Meet, Zoom, Teams, Whereby, Jitsi, Slack huddles,
  Discord, plus your own patterns) from the call window and, for always-open
  apps like Teams, its microphone capture — so every join is caught, not
  just the first launch. By default it records and stops on its own after a
  5-second countdown you can cancel from the toast, and stops the same way
  when the meeting ends; one setting turns the automation off for
  click-to-act toasts only.

**Account**
- Sign in by reusing the browser's Otter session (Chrome, Chromium, Brave, or
  Edge) — no separate login. Sign out from the panel, with a confirm step.

**Reliability and observability**
- A microphone that delivers no audio no longer kills the recording: after a
  6-second grace the helper probes every other input and switches to a live
  one (system default preferred), keeping the same conversation -- up to three
  switches. Only a system where nothing delivers audio stops, loudly.
- Follows the system default microphone when it changes mid-meeting (after it
  settles), so fixing audio in the meeting app moves the recording.
- A leave must persist three seconds, so switching devices inside a call app
  never reads as the meeting ending.
- Every decision point -- capture changes, probes, mic switches, per-minute
  health while recording, and all shell-side meeting edges -- lands in a
  rotated `events.log`, so incidents are read off a log instead of
  reconstructed from toasts.
- Recording starts and stops are confirmed by the helper before the UI
  announces them; errors arrive classified (auth, device, network, Otter,
  internal) with actionable text.
- A recording that is silent in loudness for five minutes gets one
  ask-before-stopping toast; it never stops on its own.

## Backlog

- **Annotations** — highlight/comment on transcript text
  (`update_annotation` / `remove_annotation` exist in the API,
  [PROTOCOL.md](PROTOCOL.md)). Nothing else is queued; the plugin does what
  it set out to.

## Deliberately not doing

- **Virtual-assistant / calendar auto-join** — rejected, not deferred. A
  Wayland window gives no meeting URL to dispatch a bot to, and sending a bot
  into a call is a consequence for everyone in the room. The native recorder
  covers the same need without one.
- **Silent auto-record** — recording never begins or ends quietly. Even in
  `auto` mode every action is announced, and the default (`countdown`)
  always leaves a visible five-second window to say no before the mic goes
  to a third party.
