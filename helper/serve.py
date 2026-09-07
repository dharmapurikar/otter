"""
Daemon mode: the long-lived side of the helper.

One select loop owns everything -- stdin commands, ffmpeg's PCM, and the
websocket's acks. Single-threaded on purpose: recording state is touched from
exactly one place, so there are no locks and no races to reason about.

Protocol (newline-delimited JSON both ways, see docs/ARCHITECTURE.md):

  in   {"cmd":"refresh"}
       {"cmd":"start","micDevice":"","captureSystem":true,"auto":false,"label":""}
       {"cmd":"stop"}
       {"cmd":"devices"}
       {"cmd":"transcript","otid":"..."}
       {"cmd":"search","query":"...","size":10}
       {"cmd":"summarize","otid":"..."}
       {"cmd":"note","event":"join","label":"Microsoft Teams"}  # shell decisions -> events log
       {"cmd":"logout"}
       {"cmd":"quit"}

  out  {"type":"state","authenticated":true,"recording":false,"recent":[...],
            "stdinMode":"pipe","commandable":true,"needsLogin":false}
       {"type":"recording","otid":"...","elapsed":47,"backlog":0.4}
       {"type":"level","rms":0.42}
       {"type":"call","captures":[{"name":"Microsoft Teams (PWA)","binary":"chrome"}]}  # mic captures; meeting join/leave
       {"type":"stopping","otid":"...","elapsed":312}
       {"type":"finishing","otid":"...","remaining":0.4}
       {"type":"live","otid":"...","text":"...","connected":true}
       {"type":"stopped","otid":"...","url":"...","duration":312}
       {"type":"reconciled","otid":"...","stopped":true,"keptSpool":""}  # a stray live speech was closed
       {"type":"devices","devices":[...]}
       {"type":"transcript","otid":"...","text":"...","error":""}
       {"type":"summary","otid":"...","text":"...","error":""}
       {"type":"results","query":"...","results":[...],"error":""}
       {"type":"error","message":"..."}
"""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import stat
import sys
import time
import urllib.error

import audio
import live
import record
# How often to re-check which apps hold a microphone capture (the meeting
# join/leave signal). Every 2s: fast enough that "stop when I leave" feels
# immediate, cheap enough (one pactl exec) to run forever.
MIC_CHECK_SEC = 2.0

# The events log: one JSON line per transition worth reconstructing an
# incident from -- capture changes, mic switches, aborts, finishes. It
# exists because a real mis-detection had to be investigated from
# notification toasts alone; the truth was in frames nobody recorded.
# Capped by rotation so a daemon that runs forever cannot fill the disk.
EVENTS_LOG_NAME = "events.log"
EVENTS_LOG_MAX_BYTES = 512 * 1024
# While recording, one health line per minute: elapsed, upload backlog
# and the peak RMS seen -- the witness for "was the room really silent".
HEALTH_LOG_SEC = 60.0

# Reconciling is synchronous -- a socket per stray -- so it is bounded. Past
# this many live speeches something is wrong that closing sockets will not
# fix, and the panel should say so rather than hang on startup.
MAX_REAP = 4
# How long after an automatic start the same meeting may not auto-start
# again, across daemon and shell restarts.
#
# This is the guard for the incident that produced all of this: a shell
# restart hands MeetingWatch a fresh reducer, an in-progress call reads as a
# brand-new join, and a new recording begins. It happened five times in
# ninety seconds, once per restart, and four of the five recordings were
# still live server-side when it was over.
AUTO_START_COOLDOWN_SEC = 90.0
AUTOSTART_NAME = "autostart.json"

# How often to re-poll the conversation list while idle.
DEFAULT_REFRESH_SEC = 300
# While signed out, poll briskly instead: the moment you finish signing in in
# the browser the panel should notice, rather than waiting out the idle
# interval. Backs off so a genuinely absent session is not hammered.
AUTH_RETRY_START_SEC = 2.0
AUTH_RETRY_MAX_SEC = 20.0


def stdin_kind() -> str:
    """What fd 0 actually is.

    This matters more than it looks. If stdin is a character device
    (/dev/null, a tty) rather than a pipe, select() reports it readable
    forever and every read returns empty -- which, if empty is taken to mean
    "shut down", kills the daemon the instant it starts. So only watch stdin
    when it is something a parent can actually write to.
    """
    try:
        mode = os.fstat(0).st_mode
    except OSError:
        return "none"
    if stat.S_ISFIFO(mode):
        return "pipe"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "chr"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def classify_error(err: Exception | str) -> tuple[str, str]:
    """Classify an exception or error message into (category, clean_message).

    Categories:
      - 'auth': cookie/session expired, keyring locked, missing userid, 401/403
      - 'device': mic missing, audio capture exited, ffmpeg failure
      - 'network': connection drop, dns failure, timeout, websocket disconnect
      - 'otter-api': 4xx/5xx responses from Otter endpoints, speech limit
      - 'internal': bugs, unexpected Attribute/Type/Key/Value errors
    """
    msg = str(err).strip()
    if isinstance(err, audio.AudioError):
        return ("device", msg)
    if isinstance(err, (urllib.error.URLError, TimeoutError, socket.error)):
        return ("network", msg)
    if isinstance(err, (AttributeError, TypeError, KeyError, IndexError)):
        return ("internal", msg)

    cls_name = err.__class__.__name__ if isinstance(err, Exception) else ""
    if "NetworkError" in cls_name:
        return ("network", msg)
    if "AuthError" in cls_name:
        return ("auth", msg)

    low = msg.lower()
    if any(k in low for k in ("keyring", "secret-tool", "session", "sign in", "user id", "userid", "cookie", "csrf", "unauthorized", "401")):
        return ("auth", msg)
    if any(k in low for k in ("microphone", "audio capture", "ffmpeg", "pactl", "pulse", "pipewire", "device", "no input")):
        return ("device", msg)
    if any(k in low for k in ("network", "timed out", "timeout", "connection", "socket", "dns", "refused", "reset by peer")):
        return ("network", msg)
    if any(k in low for k in ("returned 4", "returned 5", "limit", "quota", "speech_start", "speech_finish", "available_speeches")):
        return ("otter-api", msg)

    return ("internal", msg)


class Server:
    def __init__(self, core, args):
        self.core = core
        self.args = args
        self.recorder: record.Recorder | None = None
        # A recording whose capture has ended but whose last bytes are
        # still going out. Kept separate from `recorder` so state reports
        # "not recording" while the upload finishes.
        self.finalizing: record.Recorder | None = None
        # Live transcript for the recording in progress, if the
        # subscription came up. Entirely optional: recording must not
        # depend on it.
        self.live: live.LiveTranscript | None = None
        self.cookies: dict | None = None
        self.userid = ""
        self.running = True
        self.recent: list = []
        self.authenticated = False
        self.last_error = ""
        self._next_refresh = 0.0
        self._next_tick = 0.0
        self._stdin_buf = b""
        self.spool_dir = os.path.join(core.STATE_DIR, "recordings")
        self.stdin_kind = "unknown"
        self._watch_stdin = False
        self._parent = os.getppid()
        self._live_retries = 0
        self._features_read = False
        self._auth_retry = AUTH_RETRY_START_SEC
        self.needs_login = False
        # Next periodic health line while recording (see tick).
        self._next_health = 0.0
        # Mic-capture watch (meeting join/leave signal for MeetingWatch).
        self.mic_captures: list = []
        self._next_mic_check = 0.0
        self._events_path = os.path.join(core.STATE_DIR, EVENTS_LOG_NAME)
        self._autostart_path = os.path.join(core.STATE_DIR, AUTOSTART_NAME)
        # Whether the "is anything already recording?" sweep has run. Nothing
        # may auto-start before it has: a fresh process has no idea what the
        # process it replaced left behind.
        self.reconciled = False
        self._reconcile_pending = True
        # Pid of another helper that holds the recording, if there is one.
        self.blocked_by_pid = 0
        # Live speeches that are not ours to touch (the web app, a phone,
        # OtterPilot). Reported, never reaped.
        self.foreign_live: list = []

    # ------------------------------------------------------------- output

    def emit(self, **payload) -> None:
        try:
            sys.stdout.write(json.dumps(payload) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            # The shell went away; nothing left to talk to.
            self.running = False

    def emit_state(self, **extra) -> None:
        payload = {
            "type": "state",
            "authenticated": self.authenticated,
            "recording": self.recorder is not None,
            "recent": self.recent,
            "error": self.last_error,
            # Surfaced so a broken command channel is diagnosable from the
            # shell instead of looking like "start does nothing".
            "stdinMode": self.stdin_kind,
            "commandable": self._watch_stdin,
            # Being signed out is a state, not a fault. The panel already
            # shows a sign-in row, so it should not also show red error text.
            "needsLogin": self.needs_login,
            # Whether a start is allowed to happen yet, and why not.
            "reconciled": self.reconciled,
            "otherDaemonPid": self.blocked_by_pid,
            "foreignLive": self.foreign_live,
        }
        if self.recorder:
            payload["otid"] = self.recorder.otid
            payload["elapsed"] = self.recorder.elapsed()
            payload["sourceLabel"] = self.recorder.source_label
        payload.update(extra)
        self.emit(**payload)

    def log_event(self, event: str, **fields) -> None:
        """Append one JSON line to the events log, best effort.

        Diagnostics only: a logging failure must never take the daemon
        down, and the log must never hold the loop -- one write, no fsync,
        and rotate past half a megabyte.
        """
        try:
            os.makedirs(self.core.STATE_DIR, mode=0o700, exist_ok=True)
            try:
                if os.path.getsize(self._events_path) > EVENTS_LOG_MAX_BYTES:
                    os.replace(self._events_path, self._events_path + ".1")
            except OSError:
                pass
            import datetime
            line = json.dumps({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "event": event, **fields,
            })
            with open(self._events_path, "a", encoding="utf8") as fh:
                fh.write(line + "\n")
        except (OSError, ValueError):
            pass

    # --------------------------------------------------------------- auth

    def ensure_auth(self) -> dict:
        if self.cookies is not None:
            return self.cookies
        cookies = self.core.get_cookies(self.args)
        self.cookies = cookies
        self.authenticated = True
        if not self.userid:
            self.userid = self.core.user_id(cookies)
        if not self._features_read:
            self._features_read = True
            try:
                feats = self.core.user_features(cookies)
                cap = int(feats.get("max_recording_duration") or 0)
                if cap > 0:
                    record.MAX_DURATION_SEC = cap
            except Exception:
                pass  # the built-in default stands
        return cookies

    def refresh(self) -> None:
        try:
            cookies = self.ensure_auth()
            self.recent = self.core.fetch_recent(cookies, self.args.n)
            self.last_error = ""
            self.needs_login = False
            self._auth_retry = AUTH_RETRY_START_SEC
        except self.core.OtterNetworkError as e:
            # The network failed, which says nothing about the session. Keep
            # the cookies and the signed-in state; a flaky connection should
            # not look like being logged out or blank the panel.
            self.last_error = str(e)
        except Exception as e:
            # A real rejection: drop the session so the next attempt re-reads
            # it from the browser, and forget the cached file so that attempt
            # does not waste a round trip re-probing dead cookies.
            self.cookies = None
            self.authenticated = False
            self.needs_login = True
            self.last_error = str(e)
            try:
                self.core.clear_session()
            except Exception:
                pass
            self._auth_retry = min(AUTH_RETRY_MAX_SEC, self._auth_retry * 1.6)
        self._next_refresh = time.monotonic() + self.refresh_interval()
        # A reconcile that could not run for want of a session gets its
        # chance the moment one appears. Until then nothing may auto-start.
        if self._reconcile_pending and self.authenticated and not self.recorder:
            self.reconcile()
            return
        self.emit_state()

    def refresh_interval(self) -> float:
        if not self.authenticated:
            return self._auth_retry
        return float(getattr(self.args, "refresh", DEFAULT_REFRESH_SEC) or DEFAULT_REFRESH_SEC)

    # ----------------------------------------------------------- commands

    def handle(self, cmd: dict) -> None:
        name = str(cmd.get("cmd") or "")
        if name == "refresh":
            # An explicit refresh means someone is looking at the panel, so
            # restore the brisk cadence rather than staying on a long backoff.
            self._auth_retry = AUTH_RETRY_START_SEC
            self.refresh()
        elif name == "start":
            self.start(cmd)
        elif name == "stop":
            self.stop()
        elif name == "devices":
            self.devices()
        elif name == "transcript":
            self.transcript(cmd)
        elif name == "search":
            self.search(cmd)
        elif name == "summarize":
            self.summarize(cmd)
        elif name == "logout":
            self.logout()
        elif name == "note":
            # A shell-side decision point (meeting edges, countdowns),
            # recorded through the same log so one file reconstructs an
            # incident end to end. Best effort; nothing is answered.
            self.log_event("ui:" + str(cmd.get("event") or "note"),
                           **{k: v for k, v in cmd.items()
                              if k not in ("cmd", "event")})
        elif name == "quit":
            self.running = False
        elif name:
            self.emit(type="error", message=f"unknown command {name!r}")

    def logout(self) -> None:
        try:
            if self.cookies:
                self.core.logout(self.cookies)
            else:
                self.core.clear_session()
        except Exception as e:
            self.emit(type="error", message=f"signing out: {e}")
        # Forget everything either way: the user asked to be signed out, so
        # continuing to use the cookies would be the wrong answer even if the
        # API call failed.
        self.cookies = None
        self.authenticated = False
        self.needs_login = True
        self.recent = []
        self.userid = ""
        self.last_error = ""
        self._auth_retry = AUTH_RETRY_START_SEC
        self._next_refresh = time.monotonic() + self._auth_retry
        self.emit_state()

    def devices(self) -> None:
        try:
            self.emit(type="devices", devices=audio.list_sources())
        except Exception as e:
            self.emit(type="error", message=str(e))

    # These three are one-shot request/response rather than state: each
    # answers with its own frame carrying the request's otid, so a slow reply
    # cannot be mistaken for the answer to a later request.
    def transcript(self, cmd: dict) -> None:
        otid = str(cmd.get("otid") or "")
        try:
            text = self.core.fetch_transcript(self.ensure_auth(), otid)
            self.emit(type="transcript", otid=otid, text=text, error="")
        except self.core.OtterNetworkError as e:
            # Keep the session: the network failed, not the login.
            self.emit(type="transcript", otid=otid, text="", error=str(e))
        except Exception as e:
            self.cookies = None
            self.emit(type="transcript", otid=otid, text="", error=str(e))

    def search(self, cmd: dict) -> None:
        query = str(cmd.get("query") or "")
        try:
            results = self.core.search(self.ensure_auth(), query,
                                       int(cmd.get("size") or 10))
            self.emit(type="results", query=query, results=results, error="")
        except self.core.OtterNetworkError as e:
            self.emit(type="results", query=query, results=[], error=str(e))
        except Exception as e:
            self.cookies = None
            self.emit(type="results", query=query, results=[], error=str(e))

    def summarize(self, cmd: dict) -> None:
        otid = str(cmd.get("otid") or "")
        try:
            cookies = self.ensure_auth()
            transcript = self.core.fetch_transcript(cookies, otid)
            if not transcript.strip():
                raise RuntimeError("no transcript yet -- Otter may still be processing")
            summary = self.core.ask_llm(cookies, transcript,
                                        str(cmd.get("prompt") or ""))
            self.emit(type="summary", otid=otid, text=summary, error="")
        except Exception as e:
            self.emit(type="summary", otid=otid, text="", error=str(e))

    # -------------------------------------------------------- single flight

    def owns(self, otid: str) -> bool:
        """Whether a live speech is one of ours to clean up.

        The spool file is the ownership receipt: finish() only deletes it
        once Otter has everything, so a recording we started and lost still
        has one. Anything else that is live -- the web app, a phone,
        OtterPilot sitting in a meeting -- belongs to somebody else and is
        never touched.
        """
        if not otid:
            return False
        return os.path.exists(os.path.join(self.spool_dir, f"{otid}.pcm"))

    def reconcile(self) -> None:
        """Ensure at most one recording exists, before one can start.

        Runs once per daemon, before the mic watch reports anything, because
        the first thing a fresh daemon does is tell MeetingWatch about an
        in-progress call -- and an in-progress call is what makes it start
        recording. Anything left live by the process this one replaced has
        to be closed before that.
        """
        marker = record.read_active(self.core.STATE_DIR)

        # Another helper is recording right now: two panels, or a restart
        # that outran its predecessor. Stand down rather than compete for
        # the microphone, and say why -- "start does nothing", unexplained,
        # is the worst version of this.
        marker_pid = int((marker or {}).get("pid") or 0)
        if marker and marker_pid != os.getpid() and record.pid_alive(marker_pid):
            self.blocked_by_pid = marker_pid
            self.reconciled = True
            self._reconcile_pending = False
            self.log_event("reconcile_busy", pid=marker_pid,
                           otid=marker.get("otid"))
            self.emit(type="error", errorClass="internal", message=(
                f"another Otter helper (pid {marker_pid}) is already "
                "recording; this panel will not start a second one"))
            self.emit_state()
            return

        try:
            live_list = self.core.live_speeches(self.ensure_auth())
        except Exception as e:
            # No sweep without a session. Try again after the next refresh
            # rather than concluding the world is clean -- concluding that
            # wrongly is the whole bug.
            self.log_event("reconcile_deferred", error=str(e))
            self._reconcile_pending = True
            self.emit_state()
            return

        live_otids = {s["otid"]: s for s in live_list}
        mine, foreign = [], []
        for otid, s in live_otids.items():
            (mine if self.owns(otid) else foreign).append(otid)

        # A marker whose speech is not in the feed's live set still has to be
        # asked about directly: the feed is a page, not the whole account.
        # And only ever reap something confirmed live -- re-opening a
        # *finished* speech would put it back into RECORDING, which is worse
        # than the orphan we came to fix.
        marker_otid = str((marker or {}).get("otid") or "")
        if marker_otid and marker_otid not in live_otids:
            try:
                state = self.core.speech_state(self.cookies, marker_otid)
                if self.core.is_live(state):
                    mine.append(marker_otid)
                    live_otids[marker_otid] = {
                        "otid": marker_otid,
                        "startTime": int(state.get("start_time") or 0),
                    }
            except Exception as e:
                self.log_event("reconcile_probe_failed",
                               otid=marker_otid, error=str(e))

        self.foreign_live = sorted(foreign)
        if foreign:
            self.log_event("reconcile_foreign", otids=self.foreign_live)

        for otid in sorted(set(mine))[:MAX_REAP]:
            info = live_otids.get(otid) or {}
            try:
                res = record.reap_orphan(
                    self.core.Api, self.cookies, self.userid, otid,
                    start_time=int(info.get("startTime") or 0),
                    log=self.log_event)
            except Exception as e:
                _cls, msg = classify_error(e)
                self.log_event("reap_failed", otid=otid, error=msg)
                self.emit(type="reconciled", otid=otid, stopped=False,
                          error=msg)
                self.emit(type="error", errorClass="otter-api", message=(
                    f"an interrupted recording ({otid[:8]}) is still live on "
                    f"Otter and could not be closed: {msg}"))
                continue
            kept = os.path.join(self.spool_dir, f"{otid}.pcm")
            kept = kept if os.path.exists(kept) else ""
            self.emit(type="reconciled", otid=otid,
                      stopped=bool(res.get("stopped")), keptSpool=kept,
                      url=res.get("url", ""))
            if not res.get("stopped"):
                self.emit(type="error", errorClass="network", message=(
                    f"an interrupted recording ({otid[:8]}) may still be live "
                    f"on Otter: {res.get('stopError') or 'unknown'}"))

        record.clear_active(self.core.STATE_DIR)
        self.reconciled = True
        self._reconcile_pending = False
        self.blocked_by_pid = 0
        self.log_event("reconciled", reaped=sorted(set(mine))[:MAX_REAP],
                       foreign=self.foreign_live)
        self.emit_state()

    def read_autostart(self) -> dict:
        try:
            with open(self._autostart_path) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def stamp_autostart(self, label: str) -> None:
        """Remember that a meeting was auto-started, on disk.

        On disk rather than in memory because the process that needs to know
        is the one that replaces this one.
        """
        try:
            os.makedirs(self.core.STATE_DIR, mode=0o700, exist_ok=True)
            with open(self._autostart_path, "w") as fh:
                json.dump({"label": label, "ts": time.time()}, fh)
        except OSError:
            pass

    def autostart_blocked(self, label: str) -> float:
        """Seconds of cooldown left for this meeting, 0 if it may start."""
        stamp = self.read_autostart()
        if str(stamp.get("label") or "") != label:
            return 0.0
        try:
            age = time.time() - float(stamp.get("ts") or 0)
        except (TypeError, ValueError):
            return 0.0
        # A clock that jumped backwards must not lock recording out.
        if age < 0 or age >= AUTO_START_COOLDOWN_SEC:
            return 0.0
        return AUTO_START_COOLDOWN_SEC - age

    def fail_start(self, err_class: str, msg: str, quiet: bool = False) -> None:
        """Answer a start that will not happen.

        `quiet` is for a start we deliberately suppressed: the terminal
        `start_failed` frame still has to go out or the widget's pending
        flag never clears, but a decision of ours is not a fault and has no
        business painting red error text in the panel.
        """
        self.emit(type="start_failed", errorClass=err_class, message=msg)
        if not quiet:
            self.last_error = msg
            self.emit(type="error", errorClass=err_class, message=msg)
        self.log_event("start_fail", error=msg, errorClass=err_class,
                       quiet=quiet)
        self.emit_state()

    def guard_start(self, cmd: dict) -> bool:
        """Every reason not to start. True means "go ahead".

        Ordered cheapest-first, and deliberately exhaustive: this is the one
        place that stands between a restart loop and a pile of concurrent
        live sessions.
        """
        auto = cmd.get("auto") is True
        label = str(cmd.get("label") or "")

        if self.blocked_by_pid and record.pid_alive(self.blocked_by_pid):
            self.fail_start("internal", (
                f"another Otter helper (pid {self.blocked_by_pid}) holds the "
                "recording"))
            return False
        self.blocked_by_pid = 0

        if auto and not self.reconciled:
            # Not yet allowed to know whether something is already recording.
            self.fail_start("suppressed", (
                "not starting automatically yet -- still checking for a "
                "recording already in progress"), quiet=True)
            return False

        if auto:
            left = self.autostart_blocked(label)
            if left > 0:
                self.fail_start("suppressed", (
                    f"{label or 'this meeting'} was auto-recorded "
                    f"{int(AUTO_START_COOLDOWN_SEC - left)}s ago; not "
                    "starting a second recording"), quiet=True)
                return False

        # The server is the last word. A flaky sweep must not block
        # recording -- losing a meeting is worse than a duplicate -- so a
        # failure here is logged and waved through.
        try:
            live_list = self.core.live_speeches(self.ensure_auth())
        except Exception as e:
            self.log_event("live_sweep_failed", error=str(e))
            return True

        ours = [s["otid"] for s in live_list if self.owns(s["otid"])]
        theirs = [s["otid"] for s in live_list if not self.owns(s["otid"])]
        self.foreign_live = sorted(theirs)
        if theirs:
            self.fail_start("otter-api", (
                "Otter already has a recording in progress "
                f"({theirs[0][:8]}); stop it first, or run "
                "`otter.py reap --all` if it is a leftover"))
            return False

        # Ours and stranded: close it rather than making it a sibling.
        for otid in ours[:MAX_REAP]:
            info = next((s for s in live_list if s["otid"] == otid), {})
            try:
                record.reap_orphan(self.core.Api, self.cookies, self.userid,
                                   otid, start_time=int(info.get("startTime") or 0),
                                   log=self.log_event)
                self.emit(type="reconciled", otid=otid, stopped=True)
            except Exception as e:
                _cls, msg = classify_error(e)
                self.log_event("reap_failed", otid=otid, error=msg)
                self.fail_start("otter-api", (
                    f"a previous recording ({otid[:8]}) is still live and "
                    f"could not be closed: {msg}"))
                return False
        return True

    def start(self, cmd: dict) -> None:
        if self.recorder:
            self.emit(type="start_failed", errorClass="internal", message="already recording")
            self.emit(type="error", errorClass="internal", message="already recording")
            return
        if self.finalizing:
            self.fail_start("internal",
                            "the previous recording is still finishing")
            return
        if not self.guard_start(cmd):
            return
        try:
            cookies = self.ensure_auth()
            if not self.userid:
                raise record.RecordError("could not determine your Otter user id")
            rec = record.Recorder(
                api=self.core.Api,
                cookies=cookies,
                userid=self.userid,
                spool_dir=self.spool_dir,
                mic_device=str(cmd.get("micDevice") or ""),
                capture_system=cmd.get("captureSystem", True) is not False,
                emit=self.emit,
                log=self.log_event,
                state_dir=self.core.STATE_DIR,
            )
            rec.start()
        except Exception as e:
            err_class, msg = classify_error(e)
            self.cookies = None
            self.last_error = msg
            self.emit(type="start_failed", errorClass=err_class, message=msg)
            self.emit(type="error", errorClass=err_class, message=msg)
            self.log_event("start_fail", error=msg, errorClass=err_class)
            self.emit_state()
            return
        self.recorder = rec
        self.last_error = ""
        self.emit(type="started", otid=rec.otid, sourceLabel=rec.source_label, elapsed=0)
        self.emit_state()
        self._live_retries = 0
        self.start_live(rec.otid)
        if cmd.get("auto") is True:
            self.stamp_autostart(str(cmd.get("label") or ""))
        self.log_event("start", otid=rec.otid, mic=rec.mic_source,
                       monitor=len(rec._aux) > 0,
                       auto=cmd.get("auto") is True,
                       label=str(cmd.get("label") or ""))

    def start_live(self, otid: str) -> None:
        """Attach the live transcript. Best effort, by design.

        Otter needs a moment before the speech exists, and the subscription is
        a nicety -- a recording that uploads fine but shows no live text is a
        far better outcome than one that refuses to start because a second
        socket would not open. So failures are reported and dropped.
        """
        self.stop_live()
        if not otid:
            return
        try:
            sub = live.LiveTranscript(self.core.Api, self.cookies or {},
                                      otid, self.userid, emit=self.emit)
            sub.start()
            self.live = sub
            self.emit(type="live", otid=otid, text=sub.text(), connected=True)
        except Exception as e:
            self.live = None
            self.emit(type="live", otid=otid, text="", connected=False,
                      error=f"live transcript unavailable: {e}")

    def stop_live(self) -> None:
        if self.live:
            try:
                self.live.close()
            except Exception:
                pass
        self.live = None

    def stop(self) -> None:
        """End capture now; finish the upload from the loop.

        Nothing slow happens here. Capture stops, the widget is told
        immediately, and the remaining bytes go out through the normal pump
        so the daemon keeps answering commands and emitting state throughout.
        """
        if not self.recorder:
            if self.finalizing:
                return  # already on its way out
            self.emit(type="stop_failed", errorClass="internal", message="not recording")
            self.emit(type="error", errorClass="internal", message="not recording")
            return
        rec, self.recorder = self.recorder, None
        self.stop_live()
        rec.begin_stop()
        self.finalizing = rec
        self.emit(type="stopping", otid=rec.otid, elapsed=rec.elapsed())

    def finish_finalizing(self) -> None:
        """Close out a recording whose audio has all been sent."""
        rec, self.finalizing = self.finalizing, None
        if not rec:
            return
        try:
            result = rec.finish()
        except Exception as e:
            err_class, msg = classify_error(e)
            self.emit(type="stop_failed", errorClass=err_class, message=f"finishing: {msg}")
            self.emit(type="error", errorClass=err_class, message=f"finishing: {msg}")
            self.log_event("stop_fail", error=msg, errorClass=err_class)
            self.emit_state()
            return

        # Refresh FIRST. A successful refresh clears last_error, so doing it
        # after the reports below silently swallowed them -- which is how an
        # empty recording managed to report no error at all.
        self.refresh()

        if not result.get("capturedBytes"):
            # A conversation with no audio is the worst outcome to report
            # quietly: it looks finished and contains nothing.
            detail = str(result.get("captureError") or "").strip()
            self.emit(type="error", errorClass="device", message=(
                "no audio was captured, so the conversation is empty"
                + (f" -- {detail}" if detail else "")
            ))
        if result.get("finishError"):
            self.emit(type="error", errorClass="otter-api",
                      message="Otter did not finalize the recording: "
                              + str(result["finishError"]))
        # Only complain when audio was actually kept back. A tail inside the
        # ack tolerance is a finished upload, and `keptSpool` is the single
        # source of truth for "there is unsent audio on disk".
        kept = str(result.get("keptSpool") or "")
        if kept:
            seconds = float(result.get("unsentBytes") or 0) / (
                audio.SAMPLE_RATE * audio.SAMPLE_WIDTH)
            amount = f"{seconds:.1f}s" if seconds >= 0.1 else "the tail"
            self.emit(type="error", errorClass="network", message=(
                f"{amount} of audio was not uploaded; kept at {kept}"
            ))
        self.emit(type="stopped", **{k: v for k, v in result.items()
                                     if k in ("otid", "url", "duration", "keptSpool")})
        self.log_event("stopped", otid=str(result.get("otid") or ""),
                       capturedBytes=result.get("capturedBytes"),
                       micSwitches=result.get("micSwitches"),
                       keptSpool=str(result.get("keptSpool") or ""))

    # --------------------------------------------------------------- loop

    def read_stdin(self) -> None:
        try:
            chunk = os.read(0, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._watch_stdin = False
            return
        if not chunk:
            # EOF: nobody can command us any more. Stop watching stdin rather
            # than exiting -- the panel still wants conversation refreshes, and
            # a live recording must not be abandoned. Orphan detection in
            # tick() is what actually ends the process.
            self._watch_stdin = False
            self.emit(type="error",
                      message="command channel closed; start/stop unavailable")
            return
        self._stdin_buf += chunk
        while b"\n" in self._stdin_buf:
            line, _, self._stdin_buf = self._stdin_buf.partition(b"\n")
            text = line.decode("utf8", "replace").strip()
            if not text:
                continue
            try:
                cmd = json.loads(text)
            except ValueError:
                self.emit(type="error", message="unreadable command")
                continue
            if isinstance(cmd, dict):
                self.handle(cmd)

    def tick(self) -> None:
        now = time.monotonic()
        if now < self._next_tick:
            return
        self._next_tick = now + 1.0

        # With stdin no longer the shutdown signal, being orphaned is. If the
        # shell died we would otherwise linger forever holding a microphone --
        # and, worse, keep a speech live while the shell's replacement starts
        # a second one. Shutting down runs the finalize path below, so the
        # recording is closed properly rather than abandoned.
        if self._parent > 1 and os.getppid() != self._parent:
            self.log_event("orphaned", parent=self._parent,
                           now=os.getppid())
            self.emit(type="error", message=(
                "the shell exited; finishing the recording and shutting down"))
            self.running = False
            return

        # Meeting join/leave signal: which apps hold a microphone capture.
        if now >= self._next_mic_check:
            self._next_mic_check = now + MIC_CHECK_SEC
            self.check_mic_apps()
            self.check_default_mic()

        if self.recorder:
            self.emit(type="recording",
                      otid=self.recorder.otid,
                      elapsed=self.recorder.elapsed(),
                      backlog=round(self.recorder.backlog_sec(), 1))
            if now >= self._next_health:
                self._next_health = now + HEALTH_LOG_SEC
                # Same cadence as the health line: refresh the marker so a
                # later process resumes from a recent offset rather than
                # from the start of the meeting.
                self.recorder.mark_active()
                self.log_event(
                    "health", elapsed=self.recorder.elapsed(),
                    backlog=round(self.recorder.backlog_sec(), 1),
                    rmsPeak=round(self.recorder.take_minute_peak(), 3))
        elif self.recorder and not self.live and self._live_retries < 3:
            # The speech can take a beat to exist server-side; retry a couple
            # of times rather than giving up on the first miss.
            self._live_retries += 1
            self.start_live(self.recorder.otid)
        elif self.finalizing:
            pending = self.finalizing.pending_bytes()
            self.emit(type="finishing", otid=self.finalizing.otid,
                      remaining=round(pending / (audio.SAMPLE_RATE
                                                 * audio.SAMPLE_WIDTH), 1))
        elif now >= self._next_refresh:
            self.refresh()

    def check_mic_apps(self, force: bool = False) -> None:
        """Emit a `call` frame when the set of mic-capturing apps changes.

        MeetingWatch correlates these with its window matching: a call app
        that opens a microphone capture has joined a meeting, and closing it
        is leaving one -- the signal the Teams window title never gives.
        Level peekers (the shell's own audio panel) are already filtered out
        by audio.py.
        """
        try:
            captures = audio.mic_captures()
        except Exception:
            captures = []
        if force or captures != self.mic_captures:
            self.mic_captures = captures
            self.emit(type="call", captures=captures)
            self.log_event("captures", captures=captures)

    def check_default_mic(self) -> None:
        """Tell the recorder when the system default microphone moves.

        The recorder decides what to do about it (settle, then switch) --
        see Recorder.note_default. A pinned device never follows.
        """
        if not self.recorder or self.recorder.stopping:
            return
        try:
            default = audio.default_source()
        except Exception:
            return
        if default:
            self.recorder.note_default(default)

    def install_signals(self) -> None:
        """Turn a polite kill into the graceful shutdown path.

        Without this, SIGTERM ended the process outright: the loop's
        finalize below never ran, so the socket died with no stop frame and
        the speech stayed live on Otter forever. A shell restart sends
        exactly that signal, and did -- repeatedly.

        SIGKILL cannot be caught, which is why the on-disk marker exists as
        well; this only removes the excuse for the cases that are catchable.
        """
        def bow_out(signum, _frame):
            self.log_event("signal", signal=int(signum))
            self.running = False

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, bow_out)
            except (ValueError, OSError):
                pass  # not the main thread, or the platform disagrees

    def run(self) -> int:
        self.stdin_kind = stdin_kind()
        self._watch_stdin = self.stdin_kind in ("pipe", "socket")
        if self._watch_stdin:
            os.set_blocking(0, False)
        self.install_signals()
        self.log_event("serve_start", pid=os.getpid(),
                       version=getattr(self.core, "__version__", "unknown"))
        self.refresh()
        # Before the mic watch says a word. Its first report is what makes
        # MeetingWatch start recording, and a fresh daemon must not do that
        # until it knows what its predecessor left behind.
        if self._reconcile_pending:
            self.reconcile()
        # Ground truth for MeetingWatch even if a call began before us.
        self.check_mic_apps(force=True)
        if not self._watch_stdin:
            self.emit(type="error", message=(
                f"no command channel (stdin is {self.stdin_kind}); "
                "recording cannot be started"
            ))

        while self.running:
            fds = [0] if self._watch_stdin else []
            if self.recorder:
                fds.extend(self.recorder.fds())
            if self.finalizing:
                fds.extend(self.finalizing.fds())
            if self.live:
                fds.extend(self.live.fds())
            try:
                readable, _, _ = select.select(fds, [], [], 0.1)
            except (OSError, ValueError):
                # A recorder fd closed underneath us; let pump() notice.
                readable = []
            except InterruptedError:
                continue

            if self._watch_stdin and 0 in readable:
                self.read_stdin()

            if self.recorder:
                try:
                    self.recorder.pump(readable)
                except Exception as e:
                    self.emit(type="error", message=f"recording: {e}")
                    self.stop()
                else:
                    # pump() stops itself when capture dies. Without noticing,
                    # the daemon kept reporting `recording: true` forever while
                    # nothing was being recorded.
                    if self.recorder and self.recorder.stopping:
                        self.stop()

            # Push the tail of a stopped recording without blocking the loop.
            if self.finalizing:
                try:
                    self.finalizing.pump(readable)
                    if self.finalizing.drain_done():
                        self.finish_finalizing()
                except Exception as e:
                    self.emit(type="error", message=f"finishing: {e}")
                    self.finish_finalizing()

            if self.live:
                try:
                    self.live.pump()
                    text = self.live.take_if_changed()
                    if text is not None:
                        self.emit(type="live", otid=self.live.otid, text=text,
                                  connected=self.live.connected)
                except Exception as e:
                    self.emit(type="live", otid=self.live.otid, text="",
                              connected=False, error=str(e))
                    self.stop_live()

            self.tick()

        # Never abandon a recording just because we're shutting down.
        # `stop()` is the synchronous form: it drains, sends the stop frame,
        # posts speech_finish and clears the marker. Skipping any of that is
        # what leaves a speech live for the next daemon to trip over.
        for rec in (self.recorder, self.finalizing):
            if not rec:
                continue
            try:
                res = rec.stop()
                self.log_event("shutdown_finalize", otid=res.get("otid"),
                               keptSpool=str(res.get("keptSpool") or ""),
                               finishError=str(res.get("finishError") or ""))
            except Exception as e:
                # The marker survives a failure here on purpose: the next
                # daemon's reconcile is the second chance.
                self.log_event("shutdown_finalize_failed", error=str(e))
        self.recorder = None
        self.finalizing = None
        self.log_event("serve_stop")
        return 0


def run(core, args) -> int:
    return Server(core, args).run()
