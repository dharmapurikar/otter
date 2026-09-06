# Working on the Otter plugin (agent orientation)

Read this first. It tells you what this repo is, what to read in what order,
where the reference material on this machine lives, and what state the build
is in. Everything needed to build the plugin is either in `docs/` or pointed
to from here.

## What you're building

A native Omarchy (Quickshell/QML) bar plugin for Otter.ai. Otter ships only a
web app and a Chrome extension; this plugin talks to the same private API the
extension uses and presents it as a themed Omarchy bar widget + panel.

Design intent, non-negotiable:

- **Native Omarchy aesthetic** — reuse the shell's own `Ui/` kit; it must look
  like a first-party module.
- **Minimal indicator** — one glyph; it only grows a timer and the theme's
  active colour while recording. No dot: a running clock in the urgent colour
  is already unmistakable.
- **Fully themable** — zero hard-coded colors, fonts, radii, or spacing.
  Everything from `Color.*`, `Style.*`, `bar.*`.
- **Small surface, even as features grow** — the default view stays the
  minimal one. Settings hide behind a gear, transcripts and search are
  separate views, and only one view is ever on screen. Adding a capability
  must not add clutter to the idle panel.
- **Nothing surprising with audio** — recording uploads to a third party, so
  it never starts or stops silently: by default it records after a visible
  5-second countdown you can cancel (one setting makes it fully manual), a
  silent microphone aborts loudly rather than recording nothing,
  and audio Otter has not confirmed is never deleted.

## Read in this order

| # | Doc | Why |
|---|---|---|
| 1 | [docs/README.md](docs/README.md) | Overview and how the pieces fit |
| 2 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, process model, the stdio JSON protocol between QML and the helper |
| 3 | [docs/UI.md](docs/UI.md) | Exact layout, theming rules, manifest + settings schema |
| 4 | [docs/AUTH.md](docs/AUTH.md) | How the Otter session is obtained from Chrome |
| 5 | [docs/PROTOCOL.md](docs/PROTOCOL.md) | The private Otter API — endpoints, recording upload socket, live-transcript socket |

`docs/PROTOCOL.md` is the crown jewels: it was derived by reading the shipped
Chrome extension and records the full recording and transcript protocols. You
should not need to re-derive any of it. If you do want to check something
against the source, the extension is unpacked at:

```
~/.config/google-chrome/Default/Extensions/bnmojkbbkkonlmlfgejehefjldooiedp/3.10.1_0/
    src/background-script/background.js   auth, REST helpers, endpoint enum
    src/content-script/contentscript.js   live-transcript socket, speech_start call
    src/recordingTab/index.js             the audio upload loop (PCM framing, acks)
```

Those bundles are minified; grep for endpoint strings rather than reading
linearly.

## Omarchy reference material on this machine

The plugin system is documented by Omarchy itself. Read these before writing
QML — they are the API contract:

```
/usr/share/omarchy/shell/README.md              plugin manifest, kinds, IPC, shell.json
/usr/share/omarchy/shell/plugins/README.md      plugin discovery
/usr/share/omarchy/shell/Commons/Color.qml      the theme color singleton
/usr/share/omarchy/shell/Commons/Style.qml      radius/spacing/typography tokens
/usr/share/omarchy/shell/Ui/                    the component kit (Panel, PanelActionButton, …)
/usr/share/omarchy/shell/Ui/BarWidget.qml       base item every bar widget extends
```

**Canonical template — copy this plugin's shape:**

```
/usr/share/omarchy/shell/plugins/panels/dropbox/
    manifest.json     bar-widget manifest with a settings schema
    Panel.qml         bar button + dropdown panel
    Service.qml       state + spawns a helper process, parses its JSON
    Model.js          pure data helpers
    status.py         the out-of-process helper
```

Dropbox is the closest analogue to what we need (network-facing, spawns a
Python helper, has auth state). Read all five files. `plugins/agents/` is a
good second reference for a richer panel and for asset/icon conventions.

Validate before installing: `omarchy plugin validate .` — it enforces the
manifest schema, refuses symlinks, and rejects the reserved `omarchy.*` id
namespace (use `io.github.dharmapurikar.otter`).

Install/iterate loop:

```
omarchy plugin add <repo-url> --enable --yes    # or symlink-free copy into
                                                # ~/.config/omarchy/plugins/otter/
omarchy-shell shell rescanPlugins               # force reload
```

**Reloading is not as advertised — this will waste your time otherwise.**
`omarchy-shell shell rescanPlugins` did *not* pick up edits to a live
widget's `.qml`: the previously-instantiated widget kept running, so new code
and new `IpcHandler` functions simply weren't there. Use

```bash
omarchy-restart-shell     # required after any .qml change
```

It is safe and well-guarded (it refuses while the session is locked and polls
for a healthy ping before returning), but it does restart the whole bar.
Helper `.py` changes need no restart — they're re-executed on the next daemon
spawn.

Symptom to recognise: `omarchy-shell io.github.dharmapurikar.otter <newFunction>` returns
"Function not found" while older functions still answer. That means old QML
is live, not that your code is wrong.

Useful probe, once the widget is loaded:

```bash
omarchy-shell io.github.dharmapurikar.otter diagnostics   # helper path, daemon liveness,
                                         # stdin mode, auth, recording, errors
```

## Layout of this repo

```
manifest.json            bar-widget manifest (id io.github.dharmapurikar.otter)
BarWidget.qml            entry point: bar glyph + dropdown panel
Service.qml              state; runs the helper daemon, parses its JSON
Model.js                 pure formatting/parsing (unit-tested)
OtterIcon.qml            the themable mark (a Nerd Font glyph)
helper/otter.py  CLI entry: auth, REST, subcommand dispatch
helper/serve.py          daemon mode: one select loop over stdin + audio + ws
helper/record.py         a recording: spool, resumable upload, speech_finish
helper/audio.py          PipeWire/ffmpeg capture, device list, RMS
helper/ws.py             minimal RFC 6455 client (no stdlib websocket exists)
helper/live.py           live transcript: speech_update socket + patch algorithm
MeetingWatch.qml         notices meetings (Wayland windows + mic captures); countdown record
scripts/otter_probe.py   standalone auth/API diagnostic, kept from before the helper existed
test/test_helper.py      python tests for ws/audio/spool/daemon
docs/                    the documentation (user-facing first, then technical)
```

`helper/otter.py` supersedes `scripts/otter_probe.py` — the probe stays
because it's a useful zero-dependency "is my session alive?" check, but new
work goes in the helper.

## Checks to run before claiming something works

```bash
omarchy plugin validate .                                  # manifest schema
/usr/lib/qt6/bin/qmlformat *.qml >/dev/null                 # QML PARSES (exit 0)
/usr/lib/qt6/bin/qmllint -I /usr/share/omarchy/shell *.qml # QML lint
node test/model_test.mjs                                   # Model.js units
python3 test/test_helper.py                                # ws/audio/spool/daemon/finalize
python3 helper/otter.py devices                    # PipeWire enumeration
python3 helper/otter.py selftest --seconds 3       # capture only: no network,
                                                           # audio counted and discarded
python3 helper/otter.py status --n 3               # live API (needs session)
```

`qmllint` cannot resolve `qs.Commons` / `qs.Ui` without Quickshell's module
registry, so warnings about `BarIconButton`, `PanelHero`, `CursorSurface`,
`Style`, `Color` etc. being "not found" are expected noise. Only `^Error:`
lines matter.

**`qmllint` alone is not enough — run `qmlformat` too.** A file with one
surplus closing brace passed `qmllint` with zero errors and then failed to
load at runtime, showing only "Target not found" from the IPC probe.
`qmlformat` exits non-zero on it. A quick
`python3 -c "s=open(f).read(); print(s.count(chr(123)), s.count(chr(125)))"`
brace count is a decent smell test after any scripted edit, and it was right
when qmllint was wrong.

None of the above proves the widget *looks* right. Rendering must be checked by
a human with the plugin installed — say so rather than implying otherwise.

## Build status

Everything the plugin set out to do is built and verified on real hardware:
read-only widget, PipeWire recording into Otter's ASR, settings, transcripts,
search, summaries, live transcript, and meeting detection.
Virtual-assistant auto-join was considered and deliberately rejected (no
reliable meeting URL from a Wayland window, and dispatching a bot into a call
is a consequence for everyone present). `docs/` is written as user
documentation first; ARCHITECTURE.md and PROTOCOL.md go deeper for
contributors. docs/ROADMAP.md tracks the feature list: shipped, backlog, and
what was deliberately rejected.

### The session probe

`scripts/otter_probe.py` is committed and runnable. It decrypts the Otter
cookies from Chrome and hits `login_csrf` + `available_speeches`.

The 32-byte SHA-256 domain-hash question it was written to answer is settled:
**the strip is required** on this machine's Chrome (see docs/AUTH.md,
"Verified end to end").

**Never print or commit cookie values.** Report lengths and booleans only.
`session.json` is mode `0600` and lives outside the repo, under

~/.local/state/omarchy/otter/`.

## Environment notes

- This is Omarchy 4.0.2 (Arch-based), Hyprland, `omarchy-shell` = Quickshell.
- Use `pkexec`, not `sudo` — there's no TTY for a password prompt. Add
  `--noconfirm`/`-y` since the elevated command has no TTY either.
- No `cryptography` or `pycryptodome` installed; `openssl` is present. Keep the
  helper working with stdlib + openssl so the plugin has no install step.
- gnome-keyring is running and holds Chrome's Safe Storage key.
- Some agent harnesses (including Claude Code's auto mode) refuse to run
  cookie-decryption commands. If you're blocked, ask the user to run
  `scripts/otter_probe.py` themselves rather than working around the refusal.

## Repo conventions

- `origin` = `github.com/dharmapurikar/otter`.
- Never commit: cookies, session state, captured audio, transcripts.
- This is an **unofficial** API. Where behavior is inferred rather than
  observed, say so in the docs instead of asserting it.
