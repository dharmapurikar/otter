"""
A recording session: PipeWire -> Otter's ASR.

The protocol is documented in docs/PROTOCOL.md; the short version is

    POST speech_start        -> {ws_url, speech_id, otid}
    ws: {"action":"start", speech_id, offset}
    ws: <binary s16le PCM frames>
    ws: <- {"type":"ack","result":{"processed_offset": n}}   # n counts SAMPLES
    ws: {"action":"stop", speech_id}
    POST speech_finish

`processed_offset` is what makes this recoverable. Audio is appended to a
spool file as it is captured and only released once Otter acknowledges the
samples, so a dropped socket is a reconnect-and-replay rather than a lost
meeting. That is the single most important property here: a meeting you
cannot re-record is not something to lose to a flaky network.
"""

from __future__ import annotations

import json
import os
import select
import time
import uuid

import audio
import ws as wsclient

# Otter's own cap; a longer recording is rejected server-side anyway.
MAX_DURATION_SEC = 14400
# Bytes of PCM per websocket frame. 2048 bytes = 1024 samples = 64 ms at
# 16 kHz, matching the extension's AudioWorklet frame size.
FRAME_BYTES = 2048
# `chrome-ext-non-meeting` is the appid the extension uses when it is not in a
# Google Meet tab, which is the closest description of a native recorder.
APP_ID = "chrome-ext-non-meeting"
# How much unacknowledged audio still counts as a finished upload. Otter's
# final ack routinely lags the last frame by a fraction of a second, and
# treating that as data loss meant keeping a spool file and warning about
# "0s of audio" -- alarming, meaningless, and wrong. Half a second of slack
# is well below anything a listener would notice missing.
ACK_TOLERANCE_BYTES = (audio.SAMPLE_RATE * audio.SAMPLE_WIDTH) // 2
# How long to let a reaped socket sit between its bind, its stop, and its
# close. Sub-second waits were not tested; a second either side is cheap
# against a path that runs once per crash and must actually land.
REAP_SETTLE_SEC = 1.0


class RecordError(Exception):
    pass


# ------------------------------------------------- the active-recording marker

# Where a live recording announces itself, so the *next* process can find it.
# The whole point is to be readable by a daemon that exists only because this
# one was killed, so it lives on disk and not in memory.
ACTIVE_NAME = "active.json"


def active_path(state_dir: str) -> str:
    return os.path.join(state_dir, ACTIVE_NAME)


def read_active(state_dir: str) -> dict | None:
    try:
        with open(active_path(state_dir)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("otid") else None


def write_active(state_dir: str, data: dict) -> None:
    """Record the live recording where a later process can find it.

    Written atomically, because the only reader that matters is a daemon
    started because this one died: a half-written marker is exactly the case
    that must not happen.
    """
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        tmp = active_path(state_dir) + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, active_path(state_dir))
    except OSError:
        pass  # a marker we could not write is not worth failing a start over


def clear_active(state_dir: str) -> None:
    try:
        os.unlink(active_path(state_dir))
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Whether a pid is still running *and* is still a helper.

    The pid alone is not enough. Pids get recycled, and mistaking an
    unrelated process for a live daemon would refuse to record for as long
    as that process lived -- a worse failure than the one being prevented.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False  # gone, or not ours to signal: either way not our daemon
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().decode("utf8", "replace")
    except OSError:
        return False
    return "otter.py" in cmdline


def reap_orphan(api, cookies: dict, userid: str, otid: str,
                start_time: int = 0, log=None,
                connect_timeout: float = 8.0) -> dict:
    """End a live speech that nobody owns any more.

    Three measured facts make this the shape it is:

    * A speech whose socket dies without a `{"action":"stop"}` frame stays
      `live_status: "live"` **indefinitely**. It does not time out.
    * `POST speech_finish` alone does **not** end it. The call answers OK and
      the speech carries on `RECORDING`; finish is metadata, the stop frame
      is the stop.
    * The stop frame is ignored on a socket that was not first bound by an
      `{"action":"start"}` -- a bare reconnect-and-stop left the speech live.

    And one that makes it possible at all: `speech_start` is **idempotent on
    `otid`**. Handing it an otid that already exists returns the *same*
    `speech_id` with a fresh record-scoped `ws_url`, so an orphan can be
    adopted from nothing but its otid -- no socket URL or token to persist.

    Only ever call this for a speech confirmed live. Re-opening a *finished*
    one would put it back into RECORDING, which is worse than the orphan.
    """
    log = log or (lambda **kw: None)
    if not otid:
        raise RecordError("no otid to reap")

    body = api.post("speech_start", cookies, params={
        "appid": APP_ID,
        "uuid": _uuid_hex(),
        "userid": userid,
        "start_time": str(int(start_time or time.time())),
        "ignore_event": "true",
        "otid": otid,
    })
    ws_url = str(body.get("ws_url") or "")
    speech_id = str(body.get("speech_id") or "")
    if not ws_url or not speech_id:
        raise RecordError(f"speech_start did not re-issue a socket for {otid}")

    stop_error = ""
    sock = None
    try:
        sock = wsclient.WebSocket.connect(ws_url, timeout=connect_timeout)
    except Exception as e:
        stop_error = f"could not reopen the socket: {e}"
    if sock:
        try:
            sock.send_text(json.dumps({
                "action": "start", "speech_id": speech_id, "offset": 0,
            }))
            time.sleep(REAP_SETTLE_SEC)
            sock.send_text(json.dumps({
                "action": "stop", "speech_id": speech_id,
            }))
            time.sleep(REAP_SETTLE_SEC)
        except Exception as e:
            stop_error = f"stop frame failed: {e}"
        finally:
            try:
                sock.close()
            except Exception:
                pass

    end_time = int(time.time())
    finish_error = ""
    try:
        api.post("speech_finish", cookies, data={
            "appid": APP_ID,
            "userid": userid,
            "otid": otid,
            "start_time": str(int(start_time or end_time)),
            "end_time": str(end_time),
        })
    except Exception as e:
        finish_error = str(e)

    result = {
        "otid": otid,
        "speechId": speech_id,
        "url": f"https://otter.ai/u/{otid}",
        "stopped": not stop_error,
        "stopError": stop_error,
        "finishError": finish_error,
    }
    log(event="reap", **result)
    return result




def _uuid_hex() -> str:
    """A UUID-shaped id, matching the extension's own generator."""
    return str(uuid.uuid4())


class Spool:
    """Append-only PCM spool with an acknowledged read cursor.

    Everything captured is written here first. `sent` tracks how far we have
    pushed onto the wire, `acked` how far Otter has confirmed. On reconnect we
    rewind `sent` back to `acked` and replay.
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "w+b")
        self.written = 0   # bytes captured
        self.sent = 0      # bytes handed to the socket
        self.acked = 0     # bytes Otter has confirmed

    def append(self, data: bytes) -> None:
        self._fh.seek(0, os.SEEK_END)
        self._fh.write(data)
        self._fh.flush()
        self.written += len(data)

    def unsent(self, limit: int) -> bytes:
        if self.sent >= self.written:
            return b""
        self._fh.seek(self.sent)
        return self._fh.read(min(limit, self.written - self.sent))

    def rewind_to_acked(self) -> None:
        self.sent = self.acked

    def close(self, remove: bool = True) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
        if remove:
            try:
                os.unlink(self.path)
            except OSError:
                pass


class Recorder:
    """One recording, driven by an external select loop."""

    def __init__(self, api, cookies: dict, userid: str, spool_dir: str,
                 mic_device: str = "", capture_system: bool = True,
                 emit=None, log=None, state_dir: str = ""):
        self.api = api
        self.cookies = cookies
        self.userid = userid
        self.spool_dir = spool_dir
        # Where the active-recording marker goes. Defaults to the directory
        # above the spool, which is the state dir the daemon already owns.
        self.state_dir = state_dir or os.path.dirname(
            os.path.abspath(spool_dir))
        self.mic_device = mic_device
        self.capture_system = capture_system
        self.emit = emit or (lambda **kw: None)
        self.log = log or (lambda **kw: None)

        self.otid = ""
        self.speech_id = ""
        self.ws_url = ""
        self.source_label = ""
        self.mic_source = ""
        self.started_at = 0
        self.stopping = False

        self._ws: wsclient.WebSocket | None = None
        self._proc = None
        self._spool: Spool | None = None
        self._level_at = 0.0
        self._level_peak = 0.0
        self._reconnect_at = 0.0
        self._reconnects = 0
        self._minute_peak = 0.0
        # ffmpeg's stderr is drained continuously rather than only when it
        # dies: it is a non-blocking pipe, so at the moment of failure the
        # message often hasn't arrived yet and a single read returns nothing.
        # Losing the one line that explains the failure is worse than the
        # bookkeeping.
        self._stderr_buf = ""
        self.result: dict | None = None
        # Secondary captures (the output monitor). Each is buffered and mixed
        # into the mic stream as it arrives; a suspended monitor simply
        # contributes nothing.
        self._aux: list = []
        self._aux_bufs: list[bytearray] = []
        self._aux_bytes: list[int] = []
        self._acks_seen = 0
        self._finalize_deadline = 0.0
        # Mic liveness bookkeeping: when the capture last produced a byte
        # (reset on spawn and on every read), how many times this recording
        # has switched mics, and every mic it has actually tried.
        self._mic_last_data = 0.0
        self.mic_switches = 0
        self._tried: list[str] = []
        self._gave_up = False
        # A liveness probe in flight (see _start_probe).
        self._probe: dict | None = None
        # (source, first-seen) of a default-source change awaiting settle.
        self._default_cand: tuple[str, float] | None = None
        # Human labels for known sources, from the most recent probe.
        self._source_labels: dict[str, str] = {}

    # -------------------------------------------------------------- start

    def start(self) -> None:
        sources, self.source_label = audio.resolve_sources(
            self.mic_device, self.capture_system)

        self.otid = _uuid_hex()
        self.started_at = int(time.time())

        params = {
            "appid": APP_ID,
            "uuid": _uuid_hex(),
            "userid": self.userid,
            "start_time": str(self.started_at),
            "ignore_event": "true",
            "otid": self.otid,
        }
        body = self.api.post("speech_start", self.cookies, params=params)
        self.ws_url = str(body.get("ws_url") or "")
        self.speech_id = str(body.get("speech_id") or "")
        # Otter may hand back its own otid; prefer it over our guess.
        self.otid = str(body.get("otid") or self.otid)
        if not self.ws_url or not self.speech_id:
            raise RecordError("speech_start did not return a ws_url/speech_id")

        os.makedirs(self.spool_dir, mode=0o700, exist_ok=True)
        self._spool = Spool(os.path.join(self.spool_dir, f"{self.otid}.pcm"))

        # sources[0] is the microphone and becomes the clock; anything after
        # it (the output monitor) is best-effort and mixed in as it arrives.
        self._proc = _spawn(audio.capture_command(sources[0]))
        self.mic_source = sources[0]
        for extra in sources[1:]:
            self._aux.append(_spawn(audio.capture_command(extra)))
            self._aux_bufs.append(bytearray())
            self._aux_bytes.append(0)
        # Every capture gets its own silence grace: a mid-recording switch
        # replaces the mic, and the newcomer must prove itself afresh.
        self._mic_last_data = time.monotonic()
        self._tried = [self.mic_source] if self.mic_source else []
        # Announce the recording before the first byte moves. If this process
        # dies from here on, the next one can find the speech and close it.
        self.mark_active()
        self._connect()

    def mark_active(self) -> None:
        """Publish (or refresh) the marker for this recording.

        `acked` is what makes the marker worth refreshing: it is where a
        later process resumes the upload from. It is only ever a lower
        bound, and that is safe -- the offset we declare on reconnect and
        the spool position we replay from come from this same number, so a
        stale one re-sends audio Otter already has rather than skipping any.
        """
        write_active(self.state_dir, {
            "otid": self.otid,
            "speechId": self.speech_id,
            "userid": self.userid,
            "startTime": self.started_at,
            "pid": os.getpid(),
            "spool": self._spool.path if self._spool else "",
            "acked": self._spool.acked if self._spool else 0,
            "written": self._spool.written if self._spool else 0,
            "updated": int(time.time()),
        })

    def _connect(self) -> None:
        self._ws = wsclient.WebSocket.connect(self.ws_url, timeout=20.0)
        offset_samples = (self._spool.acked // audio.SAMPLE_WIDTH) if self._spool else 0
        self._ws.send_text(json.dumps({
            "action": "start",
            "speech_id": self.speech_id,
            "offset": offset_samples,
        }))
        if self._spool:
            self._spool.rewind_to_acked()

    # --------------------------------------------------------------- poll

    # Keep at most this much un-mixed monitor audio. It only builds up if the
    # monitor outruns the mic, and an unbounded buffer would trade latency for
    # nothing -- old system audio is worth less than staying in sync.
    AUX_BUFFER_LIMIT = audio.SAMPLE_RATE * audio.SAMPLE_WIDTH  # ~1 second

    def fds(self) -> list:
        out = []
        if self._proc and self._proc.stdout:
            out.append(self._proc.stdout)
        for proc in self._aux:
            if proc and proc.stdout:
                out.append(proc.stdout)
        if self._probe:
            for _src, proc in self._probe["procs"]:
                if proc and proc.stdout:
                    out.append(proc.stdout)
        if self._ws and not self._ws.closed:
            out.append(self._ws)
        return out

    def pump(self, readable: list) -> None:
        """Move audio out and acks in. Call from the daemon's select loop."""
        self._collect_stderr()
        self._drain_aux()
        self._drain_probes()
        if self._proc and self._proc.stdout in readable:
            self._read_audio()
        if self._ws and not self._ws.closed:
            self._read_acks()
        self._flush()
        self._maybe_reconnect()
        self._tick_level()
        self._mic_health()

        if self.elapsed() >= MAX_DURATION_SEC and not self.stopping:
            self.emit(type="error", message="reached Otter's 4-hour limit; stopping")
            # begin_stop, not the synchronous stop(): pump runs inside the
            # daemon's only select loop, and finish() would block it on a
            # REST call. The loop notices `stopping` and finalizes.
            self.begin_stop()

    def _collect_stderr(self) -> None:
        for proc in [self._proc, *self._aux]:
            if not proc or not proc.stderr:
                continue
            try:
                data = proc.stderr.read()
            except (BlockingIOError, InterruptedError, ValueError):
                continue
            if data:
                self._stderr_buf += data.decode("utf8", "replace")
        if len(self._stderr_buf) > 4000:
            self._stderr_buf = self._stderr_buf[-4000:]

    def _drain_aux(self) -> None:
        """Pull whatever the monitor captures have produced, bounded."""
        for i, proc in enumerate(self._aux):
            if not proc or not proc.stdout:
                continue
            try:
                data = proc.stdout.read(FRAME_BYTES * 8)
            except (BlockingIOError, InterruptedError, ValueError):
                continue
            if not data:
                continue  # nothing available, or this capture ended
            self._aux_bytes[i] += len(data)
            buf = self._aux_bufs[i]
            buf += data
            if len(buf) > self.AUX_BUFFER_LIMIT:
                del buf[:len(buf) - self.AUX_BUFFER_LIMIT]

    def _take_aux(self, count: int) -> bytes:
        """Up to `count` bytes of mixed-down monitor audio."""
        out = bytearray()
        for buf in self._aux_bufs:
            if not buf:
                continue
            take = bytes(buf[:count])
            del buf[:len(take)]
            out = bytearray(audio.mix(bytes(out), take)) if out else bytearray(take)
        return bytes(out)

    def capture_error(self) -> str:
        """Whatever ffmpeg last complained about, tidied for one line."""
        # Give it a final chance to flush now that it has exited.
        self._collect_stderr()
        text = " ".join(self._stderr_buf.split())
        return text[-400:]

    def _read_audio(self) -> None:
        try:
            chunk = self._proc.stdout.read(FRAME_BYTES * 4)
        except (BlockingIOError, InterruptedError):
            return
        if not chunk:
            # ffmpeg exited. If we didn't ask it to, that's a capture
            # failure -- but only of this one device: probe the others for
            # a live mic before giving up on the recording.
            if not self.stopping:
                # Let it die and be reaped so we can report the exit code.
                code = None
                try:
                    code = self._proc.wait(timeout=1)
                except Exception:
                    pass
                err = self.capture_error()
                detail = err or "no error output"
                if code is not None:
                    detail = f"ffmpeg exited {code}: {detail}"
                self._proc = None
                self._on_mic_exit(detail)
            return
        self._mic_last_data = time.monotonic()
        # Mix in whatever system audio has arrived. A suspended monitor
        # yields nothing and the mic passes through untouched.
        mixed = audio.mix(chunk, self._take_aux(len(chunk)))
        if self._spool:
            self._spool.append(mixed)
        peak = audio.rms(mixed)
        if peak > self._level_peak:
            self._level_peak = peak

    def _flush(self) -> None:
        if not (self._ws and self._spool) or self._ws.closed:
            return
        while True:
            data = self._spool.unsent(FRAME_BYTES)
            if not data:
                return
            try:
                self._ws.send_binary(data)
            except wsclient.WebSocketClosed:
                # Keep the bytes; _maybe_reconnect will replay from `acked`.
                return
            self._spool.sent += len(data)

    def _read_acks(self) -> None:
        while True:
            try:
                frame = self._ws.recv(timeout=0)
            except wsclient.WebSocketClosed:
                return
            if frame is None:
                return
            opcode, payload = frame
            if opcode != wsclient.TEXT:
                continue
            try:
                msg = json.loads(payload.decode("utf8", "replace"))
            except ValueError:
                continue
            if msg.get("type") != "ack":
                continue
            offset = (msg.get("result") or {}).get("processed_offset")
            if not isinstance(offset, int):
                continue
            self._acks_seen += 1
            # processed_offset counts samples and is inclusive, so the next
            # unacknowledged sample is offset + 1.
            acked_bytes = (offset + 1) * audio.SAMPLE_WIDTH
            if self._spool and acked_bytes > self._spool.acked:
                self._spool.acked = min(acked_bytes, self._spool.written)

    def _maybe_reconnect(self) -> None:
        if self.stopping or not self._ws or not self._ws.closed:
            return
        now = time.monotonic()
        if now < self._reconnect_at:
            return
        self._reconnects += 1
        backoff = min(30.0, 1.0 * (2 ** min(self._reconnects, 5)))
        self._reconnect_at = now + backoff
        try:
            self._connect()
            self.emit(type="state", note="reconnected")
        except Exception as e:
            self.emit(type="error", message=f"reconnecting to Otter: {e}")

    # How long the current mic may deliver nothing before we probe the
    # others. Chosen against a device that reported unmuted, full volume,
    # RUNNING and still handed over no bytes at all.
    SILENCE_GRACE_SEC = 6.0
    # A probe captures ~2.5s from every *other* real input concurrently and
    # keeps the best one; the current mic is judged by its own stream, so a
    # device that was merely late keeps the recording unchanged. A source
    # counts as live once it hands over a quarter-second of PCM.
    PROBE_SEC = 2.5
    PROBE_MIN_BYTES = audio.SAMPLE_RATE * audio.SAMPLE_WIDTH // 4
    # A recording forced to switch more than this is flapping, and flapping
    # costs more audio than it saves.
    MAX_MIC_SWITCHES = 3
    # A system-default change must persist this long before we follow it,
    # so a device-list reshuffle does not restart the capture for nothing.
    DEFAULT_FOLLOW_SETTLE_SEC = 4.0

    def _mic_health(self) -> None:
        """Keep the microphone honest, judging by bytes rather than claims.

        A device can report unmuted, full volume and RUNNING and still
        deliver nothing -- observed on a USB mic that was the system
        default, exactly while another app was capturing from a different
        one. Aborting the recording there turned a wrong-device situation
        into a dead conversation, and every manual restart re-resolved the
        same dead default and died the same way. The identical trigger now
        probes the other inputs for a live one and switches to it; only a
        system where no microphone delivers anything at all stops the
        recording.
        """
        if self.stopping or self._gave_up:
            return
        if self._probe:
            self._finish_probe()
            return
        if not self._proc:
            return  # capture exited; _on_mic_exit already armed the probe
        if not self._mic_last_data:
            return
        if time.monotonic() - self._mic_last_data < self.SILENCE_GRACE_SEC:
            return
        self._start_probe(f"no audio from {self._describe(self.mic_source)}")

    def _describe(self, source: str) -> str:
        return self._source_labels.get(source) \
            or audio.prettify_source(source or "")

    def _start_probe(self, reason: str) -> None:
        """Capture a few seconds from every other real input, concurrently."""
        if self.stopping or self._probe or self._gave_up:
            return
        if self.mic_switches >= self.MAX_MIC_SWITCHES:
            self._give_up(reason)
            return
        try:
            sources = audio.list_sources()
        except Exception as e:
            self._give_up(f"{reason}; could not list devices ({e})")
            return
        probe = {
            "reason": reason,
            "deadline": time.monotonic() + self.PROBE_SEC,
            "start_written": self._spool.written if self._spool else 0,
            "procs": [],
            "bytes": {},
        }
        self._source_labels = {s["name"]: s.get("label") or s["name"]
                               for s in sources}
        for s in sources:
            if s["name"] == self.mic_source:
                continue  # the current mic is judged by its own stream
            try:
                probe["procs"].append(
                    (s["name"], _spawn(audio.capture_command(s["name"]))))
            except RecordError:
                continue
        self._probe = probe
        self.log(event="probe_start", reason=reason,
                 sources=[src for src, _p in probe["procs"]])

    def _drain_probes(self) -> None:
        probe = self._probe
        if not probe:
            return
        for src, proc in probe["procs"]:
            if not proc or not proc.stdout:
                continue
            try:
                data = proc.stdout.read(1 << 16)
            except (BlockingIOError, InterruptedError, ValueError):
                continue
            if data:
                probe["bytes"][src] = probe["bytes"].get(src, 0) + len(data)

    def _finish_probe(self) -> None:
        if time.monotonic() < self._probe["deadline"]:
            return
        probe, self._probe = self._probe, None
        for _src, proc in probe["procs"]:
            _terminate(proc)
        # The current mic keeps its own seat at the trial: bytes since the
        # probe began mean it recovered on its own and nothing should change.
        revived = bool(self._spool) and \
            self._spool.written > probe["start_written"]
        self.log(event="probe_done", revived=revived,
                 bytes=dict(probe["bytes"]),
                 grew=(self._spool.written - probe["start_written"])
                 if self._spool else 0)
        if revived:
            self._mic_last_data = time.monotonic()
            self.log(event="mic_recovered", source=self.mic_source)
            return
        live = [src for src, n in probe["bytes"].items()
                if n >= self.PROBE_MIN_BYTES]
        if live:
            try:
                default = audio.default_source()
            except Exception:
                default = ""
            # Prefer the system default among the live, then the best
            # deliverer.
            live.sort(key=lambda s: (s != default, -probe["bytes"][s]))
            self._switch_mic(live[0], probe["reason"])
            return
        self._give_up(probe["reason"], extra=list(probe["bytes"]))

    def _switch_mic(self, new_source: str, reason: str) -> None:
        """Move the recording to another mic, keeping otid and spool.

        The conversation is not interrupted: the mixer treats the mic as
        the clock, so the switch costs one short silence and the upload
        resumes from the spool as if nothing happened.
        """
        old = self.mic_source
        if self._proc:
            _terminate(self._proc)
        self._proc = _spawn(audio.capture_command(new_source))
        self.mic_source = new_source
        if new_source not in self._tried:
            self._tried.append(new_source)
        self.mic_switches += 1
        self._mic_last_data = time.monotonic()
        self._default_cand = None
        self.log(event="mic_switch", **{"from": old, "to": new_source,
                                        "reason": reason})
        self.emit(type="error", message=(
            f"{reason}; recording from {self._describe(new_source)} instead"))

    def _on_mic_exit(self, detail: str) -> None:
        """The mic capture died on its own: probe before abandoning."""
        self.log(event="capture_exit", source=self.mic_source,
                 detail=detail[-200:])
        self.emit(type="error", message="audio capture failed -- " + detail)
        self._start_probe(
            f"the capture on {self._describe(self.mic_source)} exited")

    def _give_up(self, reason: str, extra: list | None = None) -> None:
        """No microphone anywhere is delivering audio: stop loudly."""
        if self._gave_up or self.stopping:
            return
        self._gave_up = True
        tried = list(self._tried)
        for src in extra or []:
            if src not in tried:
                tried.append(src)
        names = ", ".join(self._describe(s) for s in tried) or "no device"
        hint = ""
        if sum(self._aux_bytes) > 0:
            hint = (" System audio is arriving, so this is the microphone "
                    "specifically.")
        self.emit(type="error", message=(
            f"{reason}, and no other microphone is delivering audio "
            f"(tried {names}); stopping instead of recording silence." + hint))
        self.log(event="mic_giveup", reason=reason, tried=tried)
        self.begin_stop()

    def note_default(self, source: str) -> None:
        """Follow the system default when it moves, after it settles.

        Only for the follow-the-default policy: a pinned device is the
        user's explicit choice and is never left for this. The newcomer is
        health-checked by the same silence grace as a fresh start, so
        following onto a dead default simply probes again.
        """
        if self.stopping or self._probe or self._gave_up or self.mic_device:
            return
        if not source or source == self.mic_source:
            self._default_cand = None
            return
        now = time.monotonic()
        cand, since = self._default_cand or (source, now)
        if cand != source:
            self._default_cand = (source, now)
            return
        if now - since < self.DEFAULT_FOLLOW_SETTLE_SEC:
            return
        self._default_cand = None
        if self.mic_switches >= self.MAX_MIC_SWITCHES:
            self.log(event="follow_ignored", to=source,
                     reason="switch cap reached")
            return
        self._switch_mic(source, "the system default microphone changed")

    def _tick_level(self) -> None:
        now = time.monotonic()
        if now - self._level_at < 0.1:
            return
        self._level_at = now
        if self._level_peak > self._minute_peak:
            self._minute_peak = self._level_peak
        self.emit(type="level", rms=round(self._level_peak, 4))
        self._level_peak = 0.0

    def take_minute_peak(self) -> float:
        """Highest RMS since the last call -- the health line's witness
        for whether a room was really silent."""
        peak = self._minute_peak
        self._minute_peak = 0.0
        return peak

    # ---------------------------------------------------------------- stop

    def elapsed(self) -> int:
        return max(0, int(time.time()) - self.started_at) if self.started_at else 0

    def backlog_sec(self) -> float:
        """Unacknowledged audio, in seconds -- how far the upload is behind."""
        if not self._spool:
            return 0.0
        bytes_per_sec = audio.SAMPLE_RATE * audio.SAMPLE_WIDTH
        return max(0.0, (self._spool.written - self._spool.acked) / bytes_per_sec)

    # Sending the last buffered frames should take milliseconds. This only
    # bounds a socket that has gone unresponsive.
    FINALIZE_TIMEOUT_SEC = 5.0

    def begin_stop(self) -> None:
        """End capture immediately and return. Never blocks.

        Split from finish() because the daemon runs one select loop: any
        blocking here freezes the command channel and the state stream, so
        the widget kept its clock running and its "stopping" label for the
        whole finalize. Capture ends now; the remaining bytes go out from the
        normal pump.
        """
        if self.stopping:
            return
        self.stopping = True
        if self._probe:
            for _src, proc in self._probe["procs"]:
                _terminate(proc)
            self._probe = None
        _terminate(self._proc)
        self._proc = None
        for proc in self._aux:
            _terminate(proc)
        self._aux = []
        self._aux_bufs = []
        self._finalize_deadline = time.monotonic() + self.FINALIZE_TIMEOUT_SEC

    def pending_bytes(self) -> int:
        """Captured audio not yet handed to the socket."""
        if not self._spool:
            return 0
        return max(0, self._spool.written - self._spool.sent)

    def drain_done(self) -> bool:
        """True once everything has been sent, or waiting is pointless.

        Deliberately keyed on *sent*, not acked. Finalizing does not need
        acknowledgements -- the extension sends `stop` and closes -- and
        waiting for them made every stop hang for twenty seconds. Acks earn
        their keep for resuming a disconnect mid-recording, which is a
        different problem.
        """
        if not self._spool:
            return True
        if not self._ws or self._ws.closed:
            return True
        if self.pending_bytes() <= 0:
            return True
        return time.monotonic() >= self._finalize_deadline

    def finish(self) -> dict:
        """Close the socket and finalize the speech. Call once drain_done()."""
        if self.result is not None:
            return self.result
        if not self.stopping:
            self.begin_stop()

        unsent = self.pending_bytes()
        unacked = 0
        if self._spool:
            unacked = max(0, self._spool.written - self._spool.acked)

        if self._ws and not self._ws.closed:
            try:
                self._ws.send_text(json.dumps({
                    "action": "stop", "speech_id": self.speech_id,
                }))
            except wsclient.WebSocketClosed:
                pass
            self._ws.close()
        self._ws = None

        end_time = int(time.time())
        finish_error = ""
        try:
            self.api.post("speech_finish", self.cookies, data={
                "appid": APP_ID,
                "userid": self.userid,
                "otid": self.otid,
                "start_time": str(self.started_at),
                "end_time": str(end_time),
            })
        except Exception as e:
            finish_error = str(e)

        captured = self._spool.written if self._spool else 0

        # Only discard the spool once Otter has everything. Otherwise keep it:
        # the audio on disk is the last copy of that meeting.
        #
        # Capturing nothing at all is a failure, not a complete upload. The
        # naive "unacked == 0" test called an empty recording a success,
        # which is how a silent ffmpeg failure produced a finalized, empty
        # conversation and no warning anywhere.
        # Completeness means "Otter received it", i.e. everything was sent.
        # Only audio we never managed to put on the wire is worth keeping.
        kept_path = ""
        if self._spool:
            complete = captured > 0 and unsent == 0 and not finish_error
            if not complete and captured > 0:
                kept_path = self._spool.path
            self._spool.close(remove=(captured == 0) or complete)
            self._spool = None

        # The speech is stopped either way -- the stop frame went out above --
        # so the marker must go, or the next daemon will try to reap a
        # conversation that is already closed.
        clear_active(self.state_dir)

        self.result = {
            "otid": self.otid,
            "url": f"https://otter.ai/u/{self.otid}" if self.otid else "",
            "duration": max(0, end_time - self.started_at),
            "capturedBytes": captured,
            "unsentBytes": unsent,
            "unackedBytes": unacked,
            "micSwitches": self.mic_switches,
            "keptSpool": kept_path,
            "finishError": finish_error,
            "captureError": self.capture_error() if captured == 0 else "",
        }
        return self.result

    def stop(self, drain_timeout: float = FINALIZE_TIMEOUT_SEC) -> dict:
        """Synchronous stop, for the CLI and for shutdown paths.

        The daemon uses begin_stop()/drain_done()/finish() so its loop stays
        responsive; this wraps them for callers that genuinely want to block.
        """
        if self.result is not None:
            return self.result
        self.begin_stop()
        deadline = time.monotonic() + drain_timeout
        while not self.drain_done() and time.monotonic() < deadline:
            try:
                self._flush()
                self._read_acks()
            except wsclient.WebSocketClosed:
                break
            time.sleep(0.02)
        return self.finish()


# ------------------------------------------------------------------ process


def _spawn(command: list[str]):
    import subprocess
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0,
        )
    except FileNotFoundError:
        raise RecordError(f"{command[0]} not found")
    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)
    return proc


def _terminate(proc) -> None:
    """Stop a child and always reap it.

    The previous shape wrapped terminate() and wait() in one try block, so
    when terminate() raised ProcessLookupError -- which is exactly what
    happens for a process that already died on its own -- wait() was skipped
    and the child was left a zombie. Signalling and reaping are separate
    concerns; only the signal is conditional.
    """
    if not proc:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass
