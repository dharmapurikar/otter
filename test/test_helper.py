#!/usr/bin/env python3
"""
Tests for the Otter plugin helper: WebSocket framing, audio helpers, spool.

    python3 test/test_helper.py

The WebSocket tests run over a real socketpair rather than a mock, because the
things most likely to break are exactly the ones a mock would paper over:
select() behavior, partial reads, and frame reassembly across TCP boundaries.
"""

from __future__ import annotations

import io
import json as _json
import os
import socket
import struct
import sys
import tempfile
import shutil
import time
import unittest
from unittest import mock

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "helper")
sys.path.insert(0, os.path.abspath(HELPER))

import audio  # noqa: E402
import otter as core  # noqa: E402
import record  # noqa: E402
import ws as wsclient  # noqa: E402


def server_frame(opcode: int, payload: bytes, fin: bool = True,
                 mask: bool = False) -> bytes:
    """Build a frame the way a server would (unmasked by default)."""
    head = bytearray()
    head.append((0x80 if fin else 0x00) | opcode)
    length = len(payload)
    flag = 0x80 if mask else 0x00
    if length < 126:
        head.append(flag | length)
    elif length < (1 << 16):
        head.append(flag | 126)
        head += struct.pack("!H", length)
    else:
        head.append(flag | 127)
        head += struct.pack("!Q", length)
    if mask:
        key = b"\x01\x02\x03\x04"
        head += key
        payload = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
    return bytes(head) + payload


class PairedSocket:
    """A WebSocket wired to a socketpair, plus the peer end for the 'server'."""

    def __init__(self):
        self.server, client = socket.socketpair()
        self.ws = wsclient.WebSocket(client)

    def feed(self, data: bytes) -> None:
        self.server.sendall(data)

    def drain(self) -> bytes:
        self.server.setblocking(False)
        try:
            return self.server.recv(65536)
        except (BlockingIOError, OSError):
            return b""
        finally:
            self.server.setblocking(True)

    def close(self) -> None:
        for s in (self.server, self.ws._sock):
            try:
                s.close()
            except OSError:
                pass


class TestFrameParser(unittest.TestCase):
    def test_incomplete_returns_none(self):
        frame = server_frame(wsclient.TEXT, b"hello")
        # Every strict prefix must parse as "not yet", never as a short frame.
        for cut in range(len(frame)):
            self.assertIsNone(wsclient.WebSocket._parse_frame(frame[:cut]),
                              f"prefix of length {cut} parsed early")

    def test_complete_frame(self):
        frame = server_frame(wsclient.TEXT, b"hello")
        parsed = wsclient.WebSocket._parse_frame(frame)
        self.assertIsNotNone(parsed)
        consumed, fin, opcode, payload = parsed
        self.assertEqual(consumed, len(frame))
        self.assertTrue(fin)
        self.assertEqual(opcode, wsclient.TEXT)
        self.assertEqual(payload, b"hello")

    def test_extended_length_16bit(self):
        body = b"x" * 300
        parsed = wsclient.WebSocket._parse_frame(server_frame(wsclient.BINARY, body))
        self.assertEqual(parsed[3], body)

    def test_extended_length_64bit(self):
        body = b"y" * 70000
        parsed = wsclient.WebSocket._parse_frame(server_frame(wsclient.BINARY, body))
        self.assertEqual(parsed[3], body)

    def test_masked_frame_is_unmasked(self):
        parsed = wsclient.WebSocket._parse_frame(
            server_frame(wsclient.TEXT, b"masked!", mask=True))
        self.assertEqual(parsed[3], b"masked!")

    def test_trailing_bytes_left_in_buffer(self):
        buf = server_frame(wsclient.TEXT, b"one") + b"leftover"
        consumed, _, _, payload = wsclient.WebSocket._parse_frame(buf)
        self.assertEqual(payload, b"one")
        self.assertEqual(buf[consumed:], b"leftover")


class TestRecv(unittest.TestCase):
    def setUp(self):
        self.pair = PairedSocket()
        self.addCleanup(self.pair.close)

    def test_recv_single_frame(self):
        self.pair.feed(server_frame(wsclient.TEXT, b'{"type":"ack"}'))
        opcode, payload = self.pair.ws.recv(timeout=2)
        self.assertEqual(opcode, wsclient.TEXT)
        self.assertEqual(payload, b'{"type":"ack"}')

    def test_recv_timeout_returns_none(self):
        self.assertIsNone(self.pair.ws.recv(timeout=0.05))

    def test_recv_zero_timeout_still_polls_the_socket(self):
        # Regression guard. timeout=0 means "poll", not "do nothing". When a
        # zero timeout returned before ever reading, upload acks were never
        # received at all and resume-from-offset was silently dead.
        self.pair.feed(server_frame(wsclient.TEXT, b'{"type":"ack"}'))
        time.sleep(0.05)  # let it land in the kernel buffer
        frame = self.pair.ws.recv(timeout=0)
        self.assertIsNotNone(frame, "recv(timeout=0) did not read available data")
        self.assertEqual(frame[1], b'{"type":"ack"}')

    def test_recv_zero_timeout_with_nothing_available(self):
        self.assertIsNone(self.pair.ws.recv(timeout=0))

    def test_partial_then_complete(self):
        # The exact hazard the buffer-based parser exists for: a frame split
        # across reads must not corrupt the stream.
        frame = server_frame(wsclient.TEXT, b"split-me")
        self.pair.feed(frame[:3])
        self.assertIsNone(self.pair.ws.recv(timeout=0.05))
        self.pair.feed(frame[3:])
        opcode, payload = self.pair.ws.recv(timeout=2)
        self.assertEqual(payload, b"split-me")

    def test_two_frames_in_one_read(self):
        self.pair.feed(server_frame(wsclient.TEXT, b"first")
                       + server_frame(wsclient.TEXT, b"second"))
        self.assertEqual(self.pair.ws.recv(timeout=2)[1], b"first")
        self.assertEqual(self.pair.ws.recv(timeout=2)[1], b"second")

    def test_fragmented_message_reassembled(self):
        self.pair.feed(server_frame(wsclient.TEXT, b"he", fin=False)
                       + server_frame(wsclient.CONT, b"llo", fin=True))
        opcode, payload = self.pair.ws.recv(timeout=2)
        self.assertEqual(opcode, wsclient.TEXT)
        self.assertEqual(payload, b"hello")

    def test_ping_is_answered_with_pong(self):
        self.pair.feed(server_frame(wsclient.PING, b"hi")
                       + server_frame(wsclient.TEXT, b"after"))
        opcode, payload = self.pair.ws.recv(timeout=2)
        self.assertEqual(payload, b"after")  # ping never surfaces
        reply = self.pair.drain()
        parsed = wsclient.WebSocket._parse_frame(reply)
        self.assertIsNotNone(parsed, "no pong sent")
        self.assertEqual(parsed[2], wsclient.PONG)
        self.assertEqual(parsed[3], b"hi")

    def test_close_raises(self):
        self.pair.feed(server_frame(wsclient.CLOSE,
                                    struct.pack("!H", 1000) + b"bye"))
        with self.assertRaises(wsclient.WebSocketClosed) as ctx:
            self.pair.ws.recv(timeout=2)
        self.assertEqual(ctx.exception.code, 1000)
        self.assertTrue(self.pair.ws.closed)

    def test_eof_raises(self):
        self.pair.server.close()
        with self.assertRaises(wsclient.WebSocketClosed):
            self.pair.ws.recv(timeout=2)


class TestSend(unittest.TestCase):
    def setUp(self):
        self.pair = PairedSocket()
        self.addCleanup(self.pair.close)

    def test_client_frames_are_masked(self):
        self.pair.ws.send_binary(b"\x01\x02\x03")
        raw = self.pair.drain()
        self.assertTrue(raw[1] & 0x80, "client frame must set the mask bit")
        consumed, fin, opcode, payload = wsclient.WebSocket._parse_frame(raw)
        self.assertEqual(opcode, wsclient.BINARY)
        self.assertEqual(payload, b"\x01\x02\x03")

    def test_send_text_roundtrip(self):
        self.pair.ws.send_text('{"action":"start"}')
        parsed = wsclient.WebSocket._parse_frame(self.pair.drain())
        self.assertEqual(parsed[2], wsclient.TEXT)
        self.assertEqual(parsed[3], b'{"action":"start"}')

    def test_large_payload_uses_extended_length(self):
        body = b"z" * 5000
        self.pair.ws.send_binary(body)
        raw = b""
        while len(raw) < 5000:
            chunk = self.pair.drain()
            if not chunk:
                break
            raw += chunk
        parsed = wsclient.WebSocket._parse_frame(raw)
        self.assertEqual(parsed[3], body)


class TestAudio(unittest.TestCase):
    def test_rms_silence_is_zero(self):
        self.assertEqual(audio.rms(b"\x00\x00" * 100), 0.0)

    def test_rms_full_scale_is_one(self):
        loud = struct.pack("<h", -32768) * 100
        self.assertAlmostEqual(audio.rms(loud), 1.0, places=3)

    def test_rms_is_bounded(self):
        for buf in (b"", b"\x01", struct.pack("<h", 32767) * 10):
            self.assertGreaterEqual(audio.rms(buf), 0.0)
            self.assertLessEqual(audio.rms(buf), 1.0)

    def test_rms_tolerates_odd_length(self):
        # A short read can hand us half a sample; it must not raise.
        self.assertIsInstance(audio.rms(struct.pack("<h", 1000) * 5 + b"\x7f"), float)

    def test_capture_command_single_source(self):
        cmd = audio.capture_command("mysrc")
        self.assertIn("pulse", cmd)
        self.assertIn("mysrc", cmd)
        # Exactly the shape Otter's ASR requires: mono, 16 kHz, s16le, stdout.
        self.assertEqual(cmd[-7:],
                         ["-ac", "1", "-ar", "16000", "-f", "s16le", "-"])

    def test_capture_command_never_uses_amix(self):
        # Regression guard. A single ffmpeg with amix stalls forever when one
        # input is a monitor of a SUSPENDED sink: amix waits on every
        # non-EOF input, so a quiet room produced zero bytes and an empty
        # conversation. Mixing must stay in our hands, one process per source.
        cmd = audio.capture_command("mic")
        self.assertNotIn("-filter_complex", cmd)
        self.assertNotIn("-map", cmd)
        self.assertEqual(cmd.count("-i"), 1)

    def test_capture_command_rejects_empty(self):
        with self.assertRaises(audio.AudioError):
            audio.capture_command("")

    def test_mix_with_no_secondary_passes_through(self):
        primary = struct.pack("<hhh", 100, -200, 300)
        self.assertEqual(audio.mix(primary, b""), primary)

    def test_mix_sums_samples(self):
        a = struct.pack("<hh", 100, -200)
        b = struct.pack("<hh", 50, -50)
        self.assertEqual(audio.mix(a, b), struct.pack("<hh", 150, -250))

    def test_mix_saturates_instead_of_wrapping(self):
        a = struct.pack("<hh", 32000, -32000)
        b = struct.pack("<hh", 32000, -32000)
        self.assertEqual(audio.mix(a, b), struct.pack("<hh", 32767, -32768))

    def test_mix_pads_short_secondary_with_silence(self):
        # The suspended-monitor case: only part of the buffer has system
        # audio, and the rest must survive unchanged rather than be dropped.
        a = struct.pack("<hhhh", 10, 20, 30, 40)
        b = struct.pack("<hh", 5, 5)
        self.assertEqual(audio.mix(a, b), struct.pack("<hhhh", 15, 25, 30, 40))

    def test_mix_preserves_primary_length(self):
        a = struct.pack("<hhh", 1, 2, 3)
        b = struct.pack("<hhhhhh", 1, 1, 1, 1, 1, 1)
        self.assertEqual(len(audio.mix(a, b)), len(a))

    def test_mix_tolerates_odd_length_secondary(self):
        a = struct.pack("<hh", 100, 100)
        self.assertEqual(len(audio.mix(a, b"\x01")), len(a))

    def test_mic_captures_exclude_ffmpeg_and_keep_identity(self):
        # Otter's own captures must never read as "in a meeting", or a
        # recording would look like a call that never ends.
        raw = """[
          {"properties": {"application.process.binary": "ffmpeg",
                          "application.name": "ffmpeg"}},
          {"properties": {"application.process.binary": "chrome",
                          "application.name": "Microsoft Teams (PWA)"}}
        ]"""
        self.assertEqual(audio.mic_captures_from_json(raw),
                         [{"name": "Microsoft Teams (PWA)", "binary": "chrome"}])

    def test_mic_captures_exclude_level_peekers(self):
        # Omarchy's audio panel peeks input levels through a source-output
        # named "Quickshell Peak Detect"; with a meeting window on screen
        # that must not read as joining a call.
        raw = '[{"properties": {"application.name": "Quickshell Peak Detect"}}]'
        self.assertEqual(audio.mic_captures_from_json(raw), [])

    def test_mic_captures_keep_native_clients_without_binary(self):
        raw = '[{"properties": {"application.name": "pw-cat", "node.name": "pw-cat"}}]'
        self.assertEqual(audio.mic_captures_from_json(raw),
                         [{"name": "pw-cat", "binary": ""}])

    def test_mic_captures_tolerate_empty_and_null(self):
        # pactl prints literal `null` when there are no source outputs.
        self.assertEqual(audio.mic_captures_from_json("null"), [])
        self.assertEqual(audio.mic_captures_from_json("[]"), [])
        self.assertEqual(audio.mic_captures_from_json(""), [])
        self.assertEqual(audio.mic_captures_from_json("not json"), [])

    def test_mic_captures_dedupes(self):
        raw = """[
          {"properties": {"application.process.binary": "chrome",
                          "application.name": "Microsoft Teams (PWA)"}},
          {"properties": {"application.process.binary": "chrome",
                          "application.name": "Microsoft Teams (PWA)"}}
        ]"""
        self.assertEqual(audio.mic_captures_from_json(raw),
                         [{"name": "Microsoft Teams (PWA)", "binary": "chrome"}])

    def test_mic_captures_resolve_the_bound_source(self):
        # "Which mic is Teams on" is answerable from the same poll: the
        # stream's source id joined against the sources list.
        raw = _json.dumps([{"source": 71, "properties": {
            "application.process.binary": "chrome",
            "application.name": "Microsoft Teams (PWA)"}}])
        caps = audio.mic_captures_from_json(
            raw, sources_by_id={"71": "alsa_input.usb-Headset.analog-stereo"})
        self.assertEqual(caps[0]["sourceName"],
                         "alsa_input.usb-Headset.analog-stereo")

    def test_mic_captures_drop_monitor_bindings(self):
        # A chrome app capturing the desktop's audio is screen recording,
        # not joining a meeting; it must not hold the meeting signal up.
        raw = _json.dumps([{"source": 3, "properties": {
            "application.process.binary": "chrome",
            "application.name": "Screen recorder"}}])
        caps = audio.mic_captures_from_json(
            raw, sources_by_id={"3": "alsa_output.desktop.monitor"})
        self.assertEqual(caps, [])

    def test_mic_captures_keep_unresolved_bindings(self):
        # A device vanishing mid-poll must not flap the join/leave signal.
        raw = _json.dumps([{"source": 99, "properties": {
            "application.process.binary": "chrome",
            "application.name": "Microsoft Teams (PWA)"}}])
        caps = audio.mic_captures_from_json(raw, sources_by_id={})
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["sourceName"], "")

    def test_mic_captures_without_a_map_keep_the_legacy_shape(self):
        raw = _json.dumps([{"properties": {
            "application.process.binary": "chrome",
            "application.name": "Microsoft Teams (PWA)"}}])
        self.assertEqual(audio.mic_captures_from_json(raw),
                         [{"name": "Microsoft Teams (PWA)",
                           "binary": "chrome"}])

    def test_mic_captures_same_app_two_mics_are_two_bindings(self):
        # The overlap while an app switches devices is real information;
        # collapsing it hides the switch.
        entry = {"properties": {"application.process.binary": "chrome",
                                "application.name": "Microsoft Teams (PWA)"}}
        raw = _json.dumps([dict(entry, source=71), dict(entry, source=72)])
        caps = audio.mic_captures_from_json(
            raw, sources_by_id={"71": "mic-a", "72": "mic-b"})
        self.assertEqual([c["sourceName"] for c in caps], ["mic-a", "mic-b"])



class TestSpool(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="otter_spool_test_")
        self.spool = record.Spool(os.path.join(self.dir, "s.pcm"))
        self.addCleanup(self.spool.close)

    def test_append_and_read_unsent(self):
        self.spool.append(b"abcdef")
        self.assertEqual(self.spool.written, 6)
        self.assertEqual(self.spool.unsent(4), b"abcd")
        self.spool.sent += 4
        self.assertEqual(self.spool.unsent(100), b"ef")
        self.spool.sent += 2
        self.assertEqual(self.spool.unsent(100), b"")

    def test_rewind_replays_unacked_audio(self):
        # The whole point of the spool: an unacked tail is re-sent after a
        # reconnect rather than lost.
        self.spool.append(b"0123456789")
        self.spool.sent = 10
        self.spool.acked = 4
        self.spool.rewind_to_acked()
        self.assertEqual(self.spool.sent, 4)
        self.assertEqual(self.spool.unsent(100), b"456789")

    def test_unsent_never_exceeds_written(self):
        self.spool.append(b"xy")
        self.assertEqual(self.spool.unsent(1000), b"xy")


class FakeWs:
    """Just enough WebSocket to drive the finalize state machine."""

    def __init__(self):
        self.closed = False
        self.sent_text: list[str] = []
        self.sent_binary: list[bytes] = []

    def send_text(self, text):
        if self.closed:
            raise wsclient.WebSocketClosed()
        self.sent_text.append(text)

    def send_binary(self, data):
        if self.closed:
            raise wsclient.WebSocketClosed()
        self.sent_binary.append(data)

    def recv(self, timeout=None):
        return None

    def close(self, code=1000):
        self.closed = True


class FakeApi:
    def __init__(self, fail=None):
        self.calls: list[tuple] = []
        self.fail = fail

    def post(self, endpoint, cookies, params=None, data=None):
        self.calls.append((endpoint, params, data))
        if self.fail:
            raise RuntimeError(self.fail)
        return {}


class TestFinalize(unittest.TestCase):
    """The stop path: capture ends at once, the tail goes out from the loop.

    This is the machinery that replaced a stop() which blocked the daemon's
    only select loop -- the widget's clock kept running and a spurious "Otter
    did not respond" fired. Worth pinning down.
    """

    def make(self, captured=b"", sent=0, api=None):
        self.dir = tempfile.mkdtemp(prefix="otter_fin_")
        self.emitted = []
        rec = record.Recorder(
            api=api or FakeApi(), cookies={}, userid="1",
            spool_dir=self.dir,
            emit=lambda **kw: self.emitted.append(kw),
        )
        rec.otid = "otid-1"
        rec.speech_id = "sid-1"
        rec.started_at = int(time.time()) - 5
        rec._spool = record.Spool(os.path.join(self.dir, "s.pcm"))
        if captured:
            rec._spool.append(captured)
        rec._spool.sent = sent
        rec._ws = FakeWs()
        self.addCleanup(lambda: rec._spool and rec._spool.close())
        return rec

    def test_begin_stop_is_immediate_and_idempotent(self):
        rec = self.make(captured=b"\x00" * 100)
        rec.begin_stop()
        self.assertTrue(rec.stopping)
        self.assertIsNone(rec._proc)
        first = rec._finalize_deadline
        rec.begin_stop()
        self.assertEqual(rec._finalize_deadline, first)

    def test_drain_done_when_everything_sent(self):
        rec = self.make(captured=b"\x00" * 100, sent=100)
        rec.begin_stop()
        self.assertEqual(rec.pending_bytes(), 0)
        self.assertTrue(rec.drain_done())

    def test_not_done_while_bytes_remain(self):
        rec = self.make(captured=b"\x00" * 100, sent=0)
        rec.begin_stop()
        self.assertEqual(rec.pending_bytes(), 100)
        self.assertFalse(rec.drain_done())

    def test_done_when_deadline_passes(self):
        # A socket that stops accepting data must not hold the stop open.
        rec = self.make(captured=b"\x00" * 100, sent=0)
        rec.begin_stop()
        rec._finalize_deadline = time.monotonic() - 1
        self.assertTrue(rec.drain_done())

    def test_done_when_socket_closed(self):
        rec = self.make(captured=b"\x00" * 100, sent=0)
        rec.begin_stop()
        rec._ws.closed = True
        self.assertTrue(rec.drain_done())

    def test_finish_sends_stop_then_closes_and_finalizes(self):
        api = FakeApi()
        rec = self.make(captured=b"\x00" * 100, sent=100, api=api)
        ws = rec._ws
        rec.begin_stop()
        result = rec.finish()
        self.assertIn("stop", ws.sent_text[-1])
        self.assertIn("sid-1", ws.sent_text[-1])
        self.assertTrue(ws.closed)
        self.assertEqual([c[0] for c in api.calls], ["speech_finish"])
        self.assertEqual(result["capturedBytes"], 100)
        self.assertEqual(result["unsentBytes"], 0)
        self.assertEqual(result["keptSpool"], "")

    def test_finish_is_idempotent(self):
        rec = self.make(captured=b"\x00" * 10, sent=10)
        rec.begin_stop()
        first = rec.finish()
        self.assertIs(rec.finish(), first)

    def test_unsent_audio_is_kept_on_disk(self):
        # The only copy of that audio; deleting it would be the worst bug here.
        rec = self.make(captured=b"\x00" * 100, sent=40)
        rec.begin_stop()
        rec._ws.closed = True
        result = rec.finish()
        self.assertEqual(result["unsentBytes"], 60)
        self.assertTrue(result["keptSpool"])
        self.assertTrue(os.path.exists(result["keptSpool"]))

    def test_fully_sent_audio_is_discarded(self):
        rec = self.make(captured=b"\x00" * 100, sent=100)
        path = rec._spool.path
        rec.begin_stop()
        rec.finish()
        self.assertFalse(os.path.exists(path))

    def test_empty_capture_reports_and_keeps_nothing(self):
        rec = self.make()
        path = rec._spool.path
        rec.begin_stop()
        result = rec.finish()
        self.assertEqual(result["capturedBytes"], 0)
        self.assertEqual(result["keptSpool"], "")
        self.assertFalse(os.path.exists(path))

    def test_finish_survives_speech_finish_failure(self):
        rec = self.make(captured=b"\x00" * 10, sent=10, api=FakeApi(fail="boom"))
        rec.begin_stop()
        result = rec.finish()
        self.assertIn("boom", result["finishError"])
        # A failed finalize must keep the audio rather than trust the server.
        self.assertTrue(result["keptSpool"])

    def test_synchronous_stop_wrapper(self):
        rec = self.make(captured=b"\x00" * 10, sent=10)
        result = rec.stop()
        self.assertTrue(rec.stopping)
        self.assertEqual(result["capturedBytes"], 10)


class FakeProc:
    """Stands in for an ffmpeg: stdout drains like a non-blocking pipe."""

    def __init__(self, data: bytes = b""):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class TestMicLiveness(unittest.TestCase):
    """A microphone that delivers nothing is a device problem, not a reason
    to lose the meeting: probe the other inputs and switch. Only a system
    where nothing delivers at all stops the recording."""

    def make(self, aux_bytes=0):
        self.dir = tempfile.mkdtemp(prefix="otter_sil_")
        self.emitted = []
        self.logged = []
        rec = record.Recorder(
            api=FakeApi(), cookies={}, userid="1", spool_dir=self.dir,
            emit=lambda **kw: self.emitted.append(kw),
            log=lambda **kw: self.logged.append(kw),
        )
        rec.otid = "o"
        rec.speech_id = "s"
        rec.started_at = int(time.time())
        rec._spool = record.Spool(os.path.join(self.dir, "s.pcm"))
        rec._proc = FakeProc()
        rec._tried = ["mic-dead"]
        rec._ws = FakeWs()
        rec.mic_source = "mic-dead"
        rec._source_labels = {"mic-dead": "Dead Mic", "mic-live": "Live Mic",
                              "mic-alt": "Alt Mic"}
        rec._aux_bytes = [aux_bytes] if aux_bytes else []
        self.addCleanup(lambda: rec._spool and rec._spool.close())
        return rec

    def messages(self):
        return " ".join(e.get("message", "") for e in self.emitted)

    def sources(self, *names):
        return [{"name": n, "label": n, "isDefault": False} for n in names]

    def test_start_with_system_audio_initializes_aux_state(self):
        # Regression: start() with a monitor resolved appends to
        # _aux_bytes before anything else runs; an initializer eaten out
        # of __init__ killed every start with system audio on, with no
        # trace beyond a cryptic toast.
        tmp = tempfile.mkdtemp(prefix="otter_start_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rec = record.Recorder(
            api=FakeApi(), cookies={}, userid="1", spool_dir=tmp,
            emit=lambda **kw: None, log=lambda **kw: None)
        with mock.patch.object(audio, "resolve_sources",
                               return_value=(["mic-a", "out.monitor"],
                                             "Mic A")), \
             mock.patch.object(record, "_spawn",
                               return_value=FakeProc()), \
             mock.patch.object(record.Recorder, "_connect",
                               lambda self: None):
            rec.api.post = lambda *a, **k: {
                "ws_url": "ws://example/s", "speech_id": "sp1", "otid": "o1"}
            rec.start()
        self.addCleanup(lambda: rec._spool and rec._spool.close())
        self.assertEqual(rec._aux_bytes, [0])
        self.assertEqual(rec.mic_source, "mic-a")
        self.assertEqual(rec._tried, ["mic-a"])
        self.assertTrue(rec._aux)

    def test_quiet_within_grace_does_nothing(self):
        # Six quiet seconds are a device claim away from action; before
        # that, nothing may move.
        rec = self.make()
        rec._mic_last_data = time.monotonic()
        rec._mic_health()
        self.assertIsNone(rec._probe)
        self.assertFalse(rec.stopping)

    def test_silence_after_grace_probes_and_switches_to_a_live_mic(self):
        rec = self.make()
        rec._mic_last_data = time.monotonic() - (rec.SILENCE_GRACE_SEC + 1)
        with mock.patch.object(audio, "list_sources",
                               return_value=self.sources("mic-dead",
                                                         "mic-live",
                                                         "mic-alt")), \
             mock.patch.object(audio, "default_source",
                               return_value="mic-alt"), \
             mock.patch.object(record, "_spawn",
                               side_effect=lambda cmd: FakeProc(b"x" * 40000)):
            rec._mic_health()
            self.assertIsNotNone(rec._probe)
            rec._drain_probes()
            rec._probe["deadline"] = time.monotonic() - 1
            rec._finish_probe()
        # The default is live and preferred over the bigger deliverer.
        self.assertEqual(rec.mic_source, "mic-alt")
        self.assertEqual(rec.mic_switches, 1)
        self.assertFalse(rec.stopping)
        self.assertIn("recording from mic-alt instead", self.messages())
        # The probe's evidence is recorded: per-source byte counts, so a
        # give-up months later can say what each device delivered.
        self.assertTrue(any(e.get("event") == "probe_done" and
                            e.get("bytes", {}).get("mic-alt", 0) >= 40000
                            for e in self.logged),
                        self.logged)

    def test_a_mic_that_recovers_during_the_probe_is_kept(self):
        # The whole point of the probe's own window: a device that was
        # merely late keeps the recording untouched.
        rec = self.make()
        rec._mic_last_data = time.monotonic() - (rec.SILENCE_GRACE_SEC + 1)
        with mock.patch.object(audio, "list_sources",
                               return_value=self.sources("mic-dead",
                                                         "mic-live")), \
             mock.patch.object(record, "_spawn",
                               return_value=FakeProc(b"x" * 40000)):
            rec._mic_health()
            rec._spool.append(b"\x00" * 100)   # the current mic woke up
            rec._drain_probes()
            rec._probe["deadline"] = time.monotonic() - 1
            rec._finish_probe()
        self.assertEqual(rec.mic_source, "mic-dead")
        self.assertEqual(rec.mic_switches, 0)
        self.assertEqual(self.emitted, [])

    def test_no_live_mic_stops_loudly_and_names_what_was_tried(self):
        rec = self.make()
        rec._mic_last_data = time.monotonic() - (rec.SILENCE_GRACE_SEC + 1)
        with mock.patch.object(audio, "list_sources",
                               return_value=self.sources("mic-dead")), \
             mock.patch.object(record, "_spawn", return_value=FakeProc()):
            rec._mic_health()
            rec._probe["deadline"] = time.monotonic() - 1
            rec._finish_probe()
        self.assertTrue(rec.stopping)
        self.assertIn("no other microphone is delivering audio", self.messages())
        self.assertIn("Dead Mic", self.messages())

    def test_system_audio_hint_survives_the_giveup(self):
        # The clue that distinguishes "mic is dead" from "capture broken".
        rec = self.make(aux_bytes=50000)
        rec._mic_last_data = time.monotonic() - (rec.SILENCE_GRACE_SEC + 1)
        with mock.patch.object(audio, "list_sources",
                               return_value=self.sources("mic-dead")), \
             mock.patch.object(record, "_spawn", return_value=FakeProc()):
            rec._mic_health()
            rec._probe["deadline"] = time.monotonic() - 1
            rec._finish_probe()
        self.assertIn("System audio is arriving", self.messages())

    def test_switch_cap_ends_in_giveup_not_flapping(self):
        rec = self.make()
        rec.mic_switches = rec.MAX_MIC_SWITCHES
        rec._mic_last_data = time.monotonic() - (rec.SILENCE_GRACE_SEC + 1)
        rec._mic_health()
        self.assertIsNone(rec._probe)
        self.assertTrue(rec.stopping)

    def test_capture_exit_probes_instead_of_killing_the_recording(self):
        # Yesterday's ffmpeg death ended the conversation; now it is just
        # this device's opinion.
        rec = self.make()
        with mock.patch.object(audio, "list_sources",
                               return_value=self.sources("mic-dead",
                                                         "mic-live")), \
             mock.patch.object(record, "_spawn", return_value=FakeProc(b"y")):
            rec._on_mic_exit("ffmpeg exited 1: device busy")
            self.assertIsNotNone(rec._probe)
            self.assertFalse(rec.stopping)
        self.assertIn("audio capture failed", self.messages())

    def test_follows_a_settled_default_change(self):
        # "Follow the default" is the documented policy for an unset mic:
        # when the user moves the default mid-meeting to fix audio, the
        # recording follows instead of staying pinned to the dead device.
        rec = self.make()
        with mock.patch.object(record, "_spawn",
                               return_value=FakeProc()) as spawn:
            rec.note_default("mic-live")            # first sighting
            rec._default_cand = ("mic-live", time.monotonic() - 10)
            rec.note_default("mic-live")            # settled: switch
        self.assertEqual(rec.mic_source, "mic-live")
        self.assertEqual(rec.mic_switches, 1)
        self.assertIn("system default microphone changed", self.messages())

    def test_default_flap_resets_the_settle_window(self):
        rec = self.make()
        rec.note_default("mic-live")
        rec._default_cand = ("mic-live", time.monotonic() - 10)
        rec.note_default("mic-alt")                 # changed its mind
        self.assertEqual(rec.mic_source, "mic-dead")
        self.assertEqual(rec.mic_switches, 0)
        self.assertEqual(rec._default_cand[0], "mic-alt")

    def test_pinned_mic_never_follows_the_default(self):
        rec = self.make()
        rec.mic_device = "mic-dead"
        rec._default_cand = ("mic-live", time.monotonic() - 10)
        rec.note_default("mic-live")
        self.assertEqual(rec.mic_source, "mic-dead")
        self.assertEqual(rec.mic_switches, 0)

    def test_bytes_still_flowing_mean_healthy(self):
        # The check is transport liveness, not loudness: a silent room
        # still produces bytes, and must never trip this machinery.
        rec = self.make()
        rec._mic_last_data = time.monotonic()
        rec._spool.append(b"\x00" * 32000)  # one second of digital silence
        rec._mic_health()
        self.assertIsNone(rec._probe)
        self.assertFalse(rec.stopping)


class StubCore:
    """Stands in for otter, so the daemon's dispatch can be tested
    without going near a cookie store or the network."""

    STATE_DIR = tempfile.mkdtemp(prefix="otter_state_test_")
    Api = object()
    # The daemon distinguishes these, so a stand-in must expose the real one.
    OtterNetworkError = core.OtterNetworkError

    def user_features(self, cookies):
        return {}

    def __init__(self, recent=None, fail=None):
        self._recent = recent if recent is not None else []
        self._fail = fail

    def get_cookies(self, args):
        if self._fail:
            raise RuntimeError(self._fail)
        return {"sessionid": "x", "csrftoken": "y"}

    def user_id(self, cookies):
        return "12345"

    def fetch_recent(self, cookies, n):
        if self._fail:
            raise RuntimeError(self._fail)
        return self._recent[:n]


class NetworkFailCore(StubCore):
    """Auth is fine; the network is not."""

    def fetch_recent(self, cookies, n):
        raise core.OtterNetworkError("request timed out")


class Args:
    n = 3
    refresh = 300
    browser = "chrome"
    profile = "Default"
    no_cache = False


class TestServerDispatch(unittest.TestCase):
    def make(self, core=None):
        import serve
        server = serve.Server(core or StubCore(), Args())
        self.emitted = []
        server.emit = lambda **kw: self.emitted.append(kw)
        return server

    def types(self):
        return [e["type"] for e in self.emitted]

    def test_refresh_emits_state_with_recent(self):
        core = StubCore(recent=[{"otid": "a", "title": "Standup"}])
        server = self.make(core)
        server.refresh()
        self.assertEqual(self.types(), ["state"])
        state = self.emitted[0]
        self.assertTrue(state["authenticated"])
        self.assertFalse(state["recording"])
        self.assertEqual(len(state["recent"]), 1)

    def test_network_failure_keeps_the_session(self):
        # A dropped connection must not read as "signed out": that throws away
        # good cookies, blanks the panel, and sends the next attempt back to
        # the keyring for nothing.
        server = self.make(NetworkFailCore())
        server.cookies = {"sessionid": "x", "csrftoken": "y"}
        server.authenticated = True
        server.refresh()
        state = self.emitted[0]
        self.assertTrue(state["authenticated"], "network blip logged us out")
        self.assertIsNotNone(server.cookies, "network blip discarded the session")
        self.assertIn("timed out", state["error"])

    def test_refresh_failure_reports_and_deauthenticates(self):
        server = self.make(StubCore(fail="keyring locked"))
        server.refresh()
        state = self.emitted[0]
        self.assertFalse(state["authenticated"])
        self.assertIn("keyring locked", state["error"])
        # The cached session must be dropped so the next try re-reads it.
        self.assertIsNone(server.cookies)

    def test_signed_out_polls_briskly_and_flags_needs_login(self):
        # Signed out used to wait out the 300s idle interval, so signing back
        # in on the web took minutes to show. And it must be reported as a
        # state, not a fault.
        server = self.make(StubCore(fail="not signed in"))
        server.refresh()
        state = self.emitted[0]
        self.assertFalse(state["authenticated"])
        self.assertTrue(state["needsLogin"])
        self.assertLessEqual(server.refresh_interval(), 20.0)
        self.assertLess(server.refresh_interval(), 300.0)

    def test_retry_backs_off_then_resets_on_success(self):
        import serve as serve_mod
        server = self.make(StubCore(fail="not signed in"))
        first = server.refresh_interval()
        for _ in range(6):
            server.refresh()
        self.assertGreater(server.refresh_interval(), first)
        self.assertLessEqual(server.refresh_interval(), serve_mod.AUTH_RETRY_MAX_SEC)
        # Signing in returns it to the idle cadence.
        server.core = StubCore(recent=[{"otid": "a", "title": "t"}])
        server.cookies = None
        server.refresh()
        self.assertTrue(server.authenticated)
        self.assertFalse(server.needs_login)
        self.assertEqual(server.refresh_interval(), 300.0)

    def test_network_error_does_not_set_needs_login(self):
        # A blip is not a logout, so it must not show the sign-in row.
        server = self.make(NetworkFailCore())
        server.cookies = {"sessionid": "x", "csrftoken": "y"}
        server.authenticated = True
        server.refresh()
        self.assertFalse(self.emitted[0]["needsLogin"])

    def test_unknown_command_is_reported(self):
        server = self.make()
        server.handle({"cmd": "wat"})
        self.assertEqual(self.types(), ["error"])

    def test_stop_without_recording_is_reported_not_crashed(self):
        server = self.make()
        server.handle({"cmd": "stop"})
        self.assertIn("stop_failed", self.types())
        self.assertIn("error", self.types())
        self.assertTrue(any("not recording" in e.get("message", "") for e in self.emitted))

    def test_start_failure_surfaces_error(self):
        # No real audio/API here, so start() must fail cleanly rather than
        # leaving a half-built recorder behind.
        server = self.make(StubCore(fail="no session"))
        server.handle({"cmd": "start"})
        self.assertIn("start_failed", self.types())
        self.assertIn("error", self.types())
        self.assertIsNone(server.recorder)

    def test_double_start_is_refused(self):
        server = self.make()
        server.recorder = object()  # pretend a recording is live
        server.handle({"cmd": "start"})
        self.assertIn("start_failed", self.types())
        self.assertIn("error", self.types())
        self.assertTrue(any("already recording" in e.get("message", "") for e in self.emitted))

    def test_stdin_parses_multiple_commands_in_one_chunk(self):
        server = self.make()
        seen = []
        server.handle = lambda cmd: seen.append(cmd.get("cmd"))
        server._stdin_buf = b'{"cmd":"a"}\n{"cmd":"b"}\n{"cmd":"c"'
        # Drive the line-splitting directly; a partial trailing line must wait.
        while b"\n" in server._stdin_buf:
            line, _, server._stdin_buf = server._stdin_buf.partition(b"\n")
            import json as _json
            server.handle(_json.loads(line.decode()))
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(server._stdin_buf, b'{"cmd":"c"')

    def test_quit_clears_running(self):
        server = self.make()
        server.handle({"cmd": "quit"})
        self.assertFalse(server.running)


class TestEventLog(unittest.TestCase):
    """The events log is what made yesterday's mis-detection investigable
    only via notification toasts; it must actually record transitions."""

    def setUp(self):
        import serve
        self.serve = serve
        self.path = os.path.join(StubCore.STATE_DIR, "events.log")
        for suffix in ("", ".1"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def make(self):
        server = self.serve.Server(StubCore(), Args())
        server.emit = lambda **kw: None
        return server

    def lines(self, suffix=""):
        try:
            with open(self.path + suffix, encoding="utf8") as fh:
                return [line for line in fh.read().splitlines() if line]
        except FileNotFoundError:
            return []

    def test_events_append_as_json_lines(self):
        server = self.make()
        server.log_event("start", otid="abc", mic="some-mic")
        server.log_event("stopped", otid="abc", capturedBytes=10)
        rows = [_json.loads(line) for line in self.lines()]
        self.assertEqual([r["event"] for r in rows], ["start", "stopped"])
        self.assertEqual(rows[0]["mic"], "some-mic")
        self.assertTrue(rows[0]["ts"])

    def test_log_rotates_when_past_the_cap(self):
        server = self.make()
        with mock.patch.object(self.serve, "EVENTS_LOG_MAX_BYTES", 16):
            server.log_event("one")
            server.log_event("two")
        self.assertEqual(len(self.lines(".1")), 1)
        self.assertEqual(len(self.lines()), 1)

    def test_capture_transitions_are_logged_with_bindings(self):
        server = self.make()
        capture = {"name": "Microsoft Teams (PWA)", "binary": "chrome",
                   "sourceName": "alsa_input.usb-Headset"}
        with mock.patch.object(audio, "mic_captures",
                               return_value=[capture]):
            server.check_mic_apps()
        with mock.patch.object(audio, "mic_captures", return_value=[]):
            server.check_mic_apps()
        rows = [_json.loads(line) for line in self.lines()]
        self.assertEqual([r["event"] for r in rows], ["captures", "captures"])
        self.assertEqual(rows[0]["captures"][0]["sourceName"],
                         "alsa_input.usb-Headset")
        self.assertEqual(rows[1]["captures"], [])

    def test_note_command_logs_shell_side_events(self):
        # Meeting edges and countdowns arrive as `note` and land in the
        # same log, prefixed so shell-side truth is distinguishable.
        server = self.make()
        server.handle({"cmd": "note", "event": "leave",
                       "label": "Microsoft Teams"})
        rows = [_json.loads(line) for line in self.lines()]
        self.assertEqual(rows[-1]["event"], "ui:leave")
        self.assertEqual(rows[-1]["label"], "Microsoft Teams")

    def test_failed_start_is_logged(self):
        # A start that raises must reach the events log, not just a
        # toast: a failed meeting with no log line is undiagnosable.
        server = self.make()
        emitted = []
        server.emit = lambda **kw: emitted.append(kw)

        class Boom:
            def __init__(self, **kw):
                pass

            def start(self):
                raise RuntimeError("boom")

        server.userid = "1"
        with mock.patch.object(self.serve.record, "Recorder", Boom), \
             mock.patch.object(server, "ensure_auth", return_value={}):
            server.start({"cmd": "start"})
        rows = [_json.loads(line) for line in self.lines()]
        self.assertEqual(rows[-1]["event"], "start_fail")
        self.assertIn("boom", rows[-1]["error"])
        self.assertTrue(any(e.get("type") == "error" and e.get("message") == "boom" for e in emitted))
        self.assertTrue(any(e.get("type") == "start_failed" and e.get("message") == "boom" for e in emitted))

    def test_health_line_while_recording(self):
        server = self.make()

        class FakeRecorder:
            otid = "o"
            stopping = False

            def elapsed(self):
                return 61

            def backlog_sec(self):
                return 0.2

            def take_minute_peak(self):
                return 0.13

            def note_default(self, source):
                pass

        server.recorder = FakeRecorder()
        with mock.patch.object(audio, "mic_captures", return_value=[]), \
             mock.patch.object(audio, "default_source",
                               return_value="mic-a"):
            server.tick()
        rows = [_json.loads(line) for line in self.lines()]
        health = [r for r in rows if r["event"] == "health"]
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["elapsed"], 61)
        self.assertEqual(health[0]["rmsPeak"], 0.13)


class TestAstInitCompleteness(unittest.TestCase):
    """Guard against eaten initializers in Recorder and Server classes.

    Both regressions this week were caused by dropped self._x assignments in
    __init__ that went unnoticed until hit at runtime. Every self attribute
    any method touches must be assigned in __init__ (or be a method/class
    attribute) -- the AST walk makes that a build-time fact, not a runtime
    surprise.
    """

    def assert_class_initialized(self, module_file, class_name):
        import ast
        with open(module_file, "r", encoding="utf8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef) and node.name == class_name):
                continue
            methods = {i.name for i in node.body if isinstance(i, ast.FunctionDef)}
            class_attrs = {t.id for i in node.body if isinstance(i, ast.Assign)
                           for t in i.targets if isinstance(t, ast.Name)}
            init = next((i for i in node.body
                         if isinstance(i, ast.FunctionDef) and i.name == "__init__"),
                        None)
            assigned = set()
            for sub in ast.walk(init or ast.Pass()):
                if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                    assigned.update(t.attr for t in targets
                                    if isinstance(t, ast.Attribute)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id == "self")
            referenced = {s.attr for i in node.body if isinstance(i, ast.FunctionDef)
                          for s in ast.walk(i)
                          if isinstance(s, ast.Attribute)
                          and isinstance(s.value, ast.Name)
                          and s.value.id == "self"
                          and s.attr not in methods
                          and s.attr not in class_attrs}
            missing = sorted(referenced - assigned)
            self.assertEqual(
                missing, [],
                f"{class_name} uses attributes not initialized in __init__: {missing}")

    def test_recorder_attributes_assigned_in_init(self):
        self.assert_class_initialized(
            os.path.join(os.path.dirname(__file__), "..", "helper", "record.py"),
            "Recorder")

    def test_server_attributes_assigned_in_init(self):
        self.assert_class_initialized(
            os.path.join(os.path.dirname(__file__), "..", "helper", "serve.py"),
            "Server")


class TestErrorTaxonomy(unittest.TestCase):
    def test_classification_categories(self):
        import serve
        # Device errors
        cls, _ = serve.classify_error(audio.AudioError("no mic"))
        self.assertEqual(cls, "device")
        cls, _ = serve.classify_error("the audio capture on default exited")
        self.assertEqual(cls, "device")

        # Network errors
        cls, _ = serve.classify_error(TimeoutError("timed out"))
        self.assertEqual(cls, "network")
        cls, _ = serve.classify_error("connection refused by peer")
        self.assertEqual(cls, "network")

        # Auth errors
        cls, _ = serve.classify_error("session expired -- sign in")
        self.assertEqual(cls, "auth")
        cls, _ = serve.classify_error("could not determine your Otter user id")
        self.assertEqual(cls, "auth")

        # Otter API errors
        cls, _ = serve.classify_error("speech_start returned 500")
        self.assertEqual(cls, "otter-api")

        # Internal errors
        cls, _ = serve.classify_error(AttributeError("'Recorder' object has no attribute '_aux_bytes'"))
        self.assertEqual(cls, "internal")
        cls, _ = serve.classify_error(KeyError("missing_key"))
        self.assertEqual(cls, "internal")


class TestConfirmedCommands(unittest.TestCase):
    def test_successful_start_emits_started_terminal_frame(self):
        import serve
        server = serve.Server(StubCore(), Args())
        emitted = []
        server.emit = lambda **kw: emitted.append(kw)

        class GoodRecorder:
            otid = "otid-123"
            source_label = "Built-in Mic"
            mic_source = "alsa_input"
            _aux = []
            def __init__(self, **kw): pass
            def start(self): pass
            def elapsed(self): return 0

        with mock.patch.object(serve.record, "Recorder", GoodRecorder), \
             mock.patch.object(server, "ensure_auth", return_value={"sessionid": "s"}), \
             mock.patch.object(server, "start_live", lambda otid: None):
            server.userid = "u1"
            server.start({"cmd": "start"})

        types = [e["type"] for e in emitted]
        self.assertIn("started", types)
        started = next(e for e in emitted if e["type"] == "started")
        self.assertEqual(started["otid"], "otid-123")
        self.assertEqual(started["sourceLabel"], "Built-in Mic")

    def test_failed_finalize_emits_stop_failed_terminal_frame(self):
        import serve
        server = serve.Server(StubCore(), Args())
        emitted = []
        server.emit = lambda **kw: emitted.append(kw)

        class BrokenRecorder:
            def finish(self):
                raise RuntimeError("upload broke mid-stream")

        server.finalizing = BrokenRecorder()
        server.finish_finalizing()

        types = [e["type"] for e in emitted]
        self.assertIn("stop_failed", types)
        failed = next(e for e in emitted if e["type"] == "stop_failed")
        self.assertEqual(failed["errorClass"], "internal")
        self.assertIn("upload broke mid-stream", failed["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

