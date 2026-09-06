# UI design

The whole point is that this looks like it shipped with Omarchy. No custom
palette, no custom fonts, no rounded-corner value of our own — every visual
token comes from the shell's `Color` and `Style` singletons, so it re-themes
instantly when you switch Omarchy themes.

## Principles

1. **Native Omarchy aesthetic.** Reuse the shell's `Ui/` kit — `Panel`,
   `PanelActionButton`, `PanelSectionHeader`, `PanelSeparator` — exactly as the
   built-in panels do. Same margins, same hover behavior, same keyboard cursor.
2. **Minimal indicator.** One glyph. It only grows visual weight (a timer, and
   the theme's active colour) while actually recording.
3. **Fully themable.** Zero hard-coded colors. Everything is `Color.*` /
   `bar.*` / `Style.*`. Verified against the theming API in
   `shell/Commons/Color.qml` and `Style.qml`.
4. **Just the essentials.** Start a meeting, see it recording, stop it; see the
   latest three and open one. Optional VU meter, nothing more.

## The bar indicator

```
idle          recording
────          ─────────
 󰗋             󰗋  12:34        ← glyph in the theme's active colour
```

The mark is U+F05CB, a person with sound waves: it reads as "someone
speaking". Chosen over the speech bubble this started as (which just said
"chat") and over a mic or record dot, both of which are already taken in the
bar by Omarchy's own Microphone widget and screen recorder. Verified present
in the theme's Nerd Font and legible at the bar's 13px.

- **Idle:** the otter mark in `bar.text` (theme foreground). Nothing else.
- **Recording:** the mark tints to `bar.active` (theme accent/urgent) and an
  `M:SS` elapsed timer counts up beside it. The timer is driven by a local QML
  timer off `Service.elapsedSec`, resynced by the helper each second, so it
  moves smoothly without waiting on IO.

  As built there is **no dot** — the design originally called for one, but a
  running clock in the urgent color is already unmistakable, and "keep the
  indicators to a minimum" won. The colour change is what must never be
  missable: an accidentally-live recording is the failure that matters.
- The widget uses `WidgetButton`, not `BarIconButton`: the latter pins a fixed
  icon slot and would clip the timer.
- **Interaction:** left-click toggles the panel, right-click refreshes the
  conversation list, middle-click starts/stops recording. No menu. (A
  keybinding can start/stop directly via IPC; see below.)
- **Vertical bars:** the glyph alone carries it — the timer is dropped
  entirely (`barVertical` pins the label to the glyph), since a vertical bar
  has no room beside it.

Colors, all from the theme:

| Element | Token |
|---|---|
| idle glyph | `bar.text` (the bar's animated `barForeground`) |
| recording glyph | `bar.active` (via `bar.urgent`) |
| timer text | `bar.text` |
| signed out | dimmed via `WidgetButton.dimmed` |

## The panel (dropdown)

Built on `Ui/Panel`. Width follows the shell's panel sizing; corner radius and
gap come from `Style.cornerRadius` / `Style.gapsOut`.

### Idle

```
┌────────────────────────────────────┐
│  󰗋  Otter                     󰍉  󰒓 │  ← hero: mark + state, search + gear
│                                    │
│  󰑊  Start meeting                  │  ← one row that swaps to Stop when live
│     System default + system audio  │  names the mic it will use
│                                    │
│  RECENT                            │  ← PanelSectionHeader
│  Design review                     │
│    42m · Tue                       │  ← latest few; click / Enter → detail
│  1:1 w/ Priya                      │
│    28m · Mon                       │
└────────────────────────────────────┘
```


### Recording

```
┌────────────────────────────────────┐
│  󰗋  Otter                     󰍉  󰒓 │
│                                    │
│  󰙦  Stop recording               󰈈 │  ← same row, now urgent; opens the live one
│     12:34 · mic+system             │  the elapsed timer is the clock
│  ▁▃▅▇▅▃▁▃▅▇▅▃▁                     │  ← optional level meter (urgent fill)
│                                    │
│  LIVE                              │  ← tail of the transcript while you talk;
│  …the last few hundred chars       │  absent if it never came up
│  Uploading… 7s behind              │  ← only when actually behind
│                                    │
│  RECENT …                          │
└────────────────────────────────────┘
```


### Signed out

```
┌────────────────────────────────────┐
│  󰗋  Otter      Waiting for sign-in │
│                                    │
│  Sign in to Otter                  │  ← a state, not an error
│  Opens otter.ai — the panel        │
│  picks the session up by itself    │
└────────────────────────────────────┘
```

Being signed out is a **state, not an error**, so it is not drawn in the
urgent colour: the row already says what to do, and repeating it in red made
a perfectly normal condition look like a failure. A real fault — a locked
keyring, no network — still shows as an error above the row. The helper
distinguishes the two with a `needsLogin` flag rather than leaving the panel
to parse error strings.

Signing back in is picked up on its own. While signed out the helper polls
every couple of seconds instead of the idle 300s (backing off to 20s so an
unattended machine is not hammered), any explicit refresh resets it to brisk,
and clicking the sign-in row starts a 2-second poll for a minute — because
clicking it means a login is about to happen. Waiting out the idle interval
used to make signing in look like it had not worked.

## The recent list

- Source: `available_speeches` — the helper asks with headroom above the
  `recentCount` setting (default 3, up to 6), because `page_size` is an upper
  bound, not a promise.
- Each row is two lines: the **title** (elided; "Untitled" when the API sends
  `null`) over a muted `42m · Tue` caption. Sub-minute durations keep their
  seconds — Otter is full of 6-second recordings and "0m" reads like a bug.
  A chevron marks the keyboard cursor's row.
- Click or Enter → the **detail view** with the transcript in place;
  open-in-Otter is the button there (or `o`), which opens it as an app window
  in your default browser.
- Empty state: "No conversations yet."

## Source selection

Pick the microphone you want, with the system default always one tap away.
Settings are their **own view**, reached from the gear on the hero's trailing
edge or by pressing `s`, and left with Back or Esc. They were originally
inline in the main panel, which pushed the conversation list down and made
the ordinary view busier the more options existed; as a separate view the
main panel stays the minimal one no matter how much configuration grows.

```
  MICROPHONE
   󰄬 System default          ← radio-style; exactly one is in effect
     Blue Microphones Analog Stereo
     Core Ultra Digital Microphone
     Webcam C930e Analog Stereo
   ☑ Capture system audio
     Transcribe the other side of a meeting too
   ☑ Show level meter

  MEETINGS
   ☑ Start and stop recording automatically
     5-second countdown you can cancel; off means click-to-act
```

- **Microphone** — the live PipeWire input list, labelled with Pulse's own
  descriptions (`pactl -f json`), which beat anything recoverable from a
  device id. **System default** is the top row and the out-of-box choice
  (an empty `micDevice`), so recording needs no setup and follows whatever
  Omarchy's audio panel has set — no re-picking when you swap headsets.
  Its label comes from `Pipewire.defaultAudioSource` rather than the helper's
  cached list, so changing the system default is reflected immediately;
  reading it from the cached `isDefault` flag left the panel naming the old
  device until the daemon restarted.
  Choosing a named device pins it. The tick marks which is in effect, so
  "System default" reads as a real choice rather than an absence of one.
- **System audio** — a toggle (`captureSystemAudio`, default on) that mixes in
  the output monitor, so the other side of a meeting is transcribed too. Turn
  it off for a mic-only memo.
- Choices persist in the shell's own settings store (`shell.json`), the same
  place Omarchy's built-in widgets keep theirs — so they survive restarts and
  re-themes.
- Scriptable too: `omarchy-shell io.github.dharmapurikar.otter setMic <name|"">` and
  `... devices`.
- The live block's source label reflects the actual mix: `mic`, `system`, or
  `mic+system`.
- If a pinned device disappears (unplugged), the helper falls back to the system
  default and notes it in the source label, rather than failing the recording.

Enumeration runs once per daemon lifetime and when the picker opens — enough
for the idle summary to name the mic in words. Nothing polls audio devices in
the background.

## The VU meter (optional, easy path only)

Included because PipeWire hands us an RMS level almost for free while
capturing; it is **not** worth a heavy audio-analysis dependency.

- A single slim rounded bar: a foreground-coloured track at low alpha with
  the fill in the urgent colour, tracking the `{"type":"level","rms":…}`
  frames (~10 Hz, 90 ms width animation).
- If `level` frames aren't arriving (helper built without it, or disabled in
  settings), the row simply isn't rendered — no placeholder, no error.
- Setting `showMeter` (default `true`) hides it entirely.

Deliberately **not** doing: waveforms, spectrograms, per-speaker meters. A
level bar is as far as audio graphics go; anything richer would cost more
than it tells.

## Stopping

Stopping is not instantaneous — capture ends at once, but the last buffered
audio still has to reach Otter. The panel says so rather than pretending:

```
  ⏹  Finishing…
     Uploading the last 0.4s to Otter
```

The bar indicator drops back to its idle glyph immediately and the clock
stops, because by then nothing is being captured. Typically under a second.

## The four views

Only one is on screen at a time, so the panel never becomes a wall of
everything. Esc unwinds one level rather than dumping you out: detail/search →
main, settings → hidden, then close.

| View | What it holds |
|---|---|
| **main** | hero, start/stop, live transcript while recording, RECENT |
| **settings** | microphone picker, audio toggles, and the account section |
| **detail** | one conversation: back, title, summarize, open-in-Otter, the SUMMARY block, then the transcript (a read-only `TextEdit`, so it can be selected and quoted) |
| **search** | a text field and results; email addresses match participants rather than text |

While recording, the main view shows a **LIVE** block with the tail of the
transcript (last ~400 chars). It is a glance while you talk, not a reading
view — the full text is one click away in detail. The header reads
"LIVE (reconnecting)" if the subscription drops, and the whole block is absent
if it never came up, because recording does not depend on it.

## Signing out

The account section at the bottom of settings. It takes **two clicks** — the
row changes to "click again to confirm" — because there is no plugin-only
session to end: the cookies are the browser's, so signing out here signs you
out of otter.ai in Chrome as well. The row says so rather than leaving it to
be discovered.

The local cached session is dropped either way, including when the API call
fails: if you asked to be signed out, continuing to use those cookies would
be the wrong answer.

## Closing the panel

Esc, clicking the bar glyph again, or clicking anywhere outside it. Esc
unwinds one level at a time — a sub-view returns to main first, so it takes
two presses from settings, detail or search.

## Keyboard & IPC

- In-panel: `j`/`k` move the cursor, Enter activates, Esc goes back one level.
  `r` refreshes, `s` toggles settings, `/` searches. In the detail view, `m`
  summarizes and `o` opens the conversation in Otter.
- Global: `omarchy-shell io.github.dharmapurikar.otter toggle` opens/closes the panel;
  `record` / `stop` / `recordToggle` control recording, `cancelCountdown`
  vetoes a running countdown, and `diagnostics` dumps helper state for
  debugging. Bind these in
  Hyprland for push-to-record without touching the mouse, e.g.

  Omarchy 4 configures Hyprland in **Lua**, so bindings go in
  `~/.config/hypr/bindings.lua` (not a `.conf`):

  ```lua
  o.bind("SUPER + ALT + R", "Otter record toggle", "omarchy-shell io.github.dharmapurikar.otter recordToggle")
  o.bind("SUPER + ALT + O", "Otter panel", "omarchy-shell io.github.dharmapurikar.otter toggle")
  ```

  Both keys were free here. Re-binding an occupied key needs `hl.unbind(...)`
  first. Validate with `hyprctl reload` then `hyprctl configerrors`.

## Meeting detection

A meeting is detected from two signals together:

- **A window that looks like a meeting** (Meet, Zoom, Teams, Whereby, Jitsi,
  Slack huddles, Discord — by app id, or by title for a browser tab). For
  genuinely call-scoped clients (the Meet, Whereby and Jitsi PWAs open a
  window per call) the window alone *is* the call. Zoom, Teams, Slack and
  Discord keep a window open all day, so their window can only *name* the
  meeting.
- **A microphone capture by that same app.** Call clients open a capture
  when you join and close it when you leave — the signal the window alone
  can never give. The capture is *correlated*: its process must belong to
  the matched window's app (a chrome-family window wants the chrome
  binary), so Omarchy's own audio panel — which peeks input levels through
  a "Quickshell Peak Detect" stream — can never read as joining a call.
  Otter's own ffmpeg captures are excluded too, so recording never looks
  like a meeting that never ends. Windows of unrecognized apps (your own
  `meetingPatterns`) accept any capture rather than miss a meeting.

Joining and leaving each act once, as an edge. Two settings decide what an
edge does:

- **`meetingDetection`** (`on`, default) — watch for meetings at all.
- **`meetingAutoRecord`** (`true`, default) — act automatically: join shows
  a toast "*Recording starts in 5 s — click to cancel*" and records if you
  don't; leave shows "*Stopping in 5 s — click to keep recording*" and
  stops if you don't. Turn it off and the same toasts become click-to-act
  only: you press the button, nothing happens by itself.

The countdown toast **ticks live** — 5, 4, 3… — by replacing itself in
place through its notification id (`omarchy-notification-send -p`/`-r`), and
its one click is the cancel (`cancelCountdown` IPC), so vetoing costs
nothing else. When the countdown resolves, the same toast becomes the
outcome ("Recording started", "Recording stopped", "Cancelled"): one toast
per meeting, never a stack. The countdowns cross-cancel correctly: a meeting
that ends before the auto-start fires cancels the start rather than
recording an empty room, and rejoining within the stop countdown keeps the
recording going. Five seconds is long enough to read and click, short
enough that the recording still catches the start of the meeting.

Only one recording can ever be in progress — the daemon refuses a second
start while one is live, and the panel's start button and the countdown
both check before firing.

Uploading room audio is never silent: every automatic action is announced,
and the default always leaves a visible window to say no.

## Manifest

The real thing is [`manifest.json`](../manifest.json) at the repo root —
validated by `omarchy plugin validate` and the source of truth; it is not
reproduced here so it cannot drift. Shape: `bar-widget` kind, entry point
`BarWidget.qml`, `category: AI`, aliases `otter` / `otter.ai`, one instance,
default section `right`.

Settings declared in `barWidget.schema`, all persisted inline in
`shell.json`:

| Key | Type | Default | Changed from |
|---|---|---|---|
| `micDevice` | string | `""` = system default | the panel's picker, or IPC `setMic` |
| `captureSystemAudio` | boolean | `true` | the settings view toggle |
| `showMeter` | boolean | `true` | the settings view toggle |
| `meetingDetection` | enum `on`/`off` | `on` | Omarchy's settings UI |
| `meetingAutoRecord` | boolean | `true` | Omarchy's settings UI |
| `meetingPatterns` | string | `""` | Omarchy's settings UI |
| `recentCount` | integer 1–6 | `3` | Omarchy's settings UI |
| `refreshIntervalSec` | integer 30–3600 | `300` | Omarchy's settings UI |
| `browser` | enum `chrome`/`chromium`/`brave`/`edge` | `chrome` | Omarchy's settings UI |
| `chromeProfile` | string | `Default` | Omarchy's settings UI |

The three the panel owns are written back through the shell's settings API.

The id is `io.github.dharmapurikar.otter`, so it installs into
`~/.config/omarchy/plugins/io.github.dharmapurikar.otter/` and that is also its IPC target. The
`omarchy.*` namespace is reserved for first-party plugins and
`omarchy plugin validate` refuses it.

## Theming checklist (no hard-coded values)

- Colors → `Color.foreground/accent/urgent/muted/background`, `bar.text/active`,
  `popups.*`.
- Radius/spacing/gaps → `Style.cornerRadius`, `Style.gapsOut`, `Style` spacing
  tokens.
- Font → `bar.fontFamily` / `Style.font.*`.
- State affordances (hover/selected/focus) → reuse `Ui/` components, which
  already read `Style.styleOverrides`.

The mark is not a bundled asset — there is no `assets/` directory.
`OtterIcon.qml` renders U+F05CB as a Nerd Font glyph: it inherits the theme
colour exactly, scales with the font tokens, and needs no colorization pass.
Alternative glyphs that are covered by the font are listed in that file's
comment.
