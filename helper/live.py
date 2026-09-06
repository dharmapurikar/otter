"""
Live transcript: words arriving while the recording is still running.

A second socket, separate from the audio upload. The upload socket carries
PCM one way; this one carries incremental transcript patches the other. See
docs/PROTOCOL.md §"Live transcript".

    GET speech?otid=<otid>            -> pubsub_jwt_persistent, speech_id
    wss://ws.aisense.com/api/v2/client/speech_update
        ?token=<jwt>&speech_id=<id>&userid=<uid>[&index=<pubsub_v2_index>]

Frames are discriminated on `type`; `speech_pieces` is the one that matters.
Each piece is a *patch* against a segment identified by uuid, not a fresh
copy, so applying them in order is the only way to arrive at the right text.
"""

from __future__ import annotations

import json
import urllib.parse

import ws as wsclient

WS_BASE = "wss://ws.aisense.com/api/v2/client/speech_update"


class LiveTranscript:
    """Subscribes to one speech and keeps its transcript up to date."""

    def __init__(self, api, cookies: dict, otid: str, userid: str, emit=None):
        self.api = api
        self.cookies = cookies
        self.otid = otid
        self.userid = userid
        self.emit = emit or (lambda **kw: None)

        self.speech_id = ""
        self.connected = False
        self.error = ""
        self._ws: wsclient.WebSocket | None = None
        # uuid -> segment. A dict because patches address segments by uuid;
        # ordering is recovered from start_offset at render time.
        self._segments: dict[str, dict] = {}
        self._dirty = False

    # -------------------------------------------------------------- start

    def start(self) -> None:
        st, body = self.api.get(f"{_api_base()}/speech?otid={urllib.parse.quote(self.otid)}",
                                self.cookies)
        if st != 200:
            raise RuntimeError(f"speech returned {st}")
        try:
            speech = json.loads(body).get("speech") or {}
        except ValueError:
            raise RuntimeError("speech returned invalid JSON")

        token = str(speech.get("pubsub_jwt_persistent") or "")
        self.speech_id = str(speech.get("speech_id") or "")
        if not token or not self.speech_id:
            raise RuntimeError("speech carried no pubsub token")

        # Seed from whatever already exists, so re-attaching to a recording
        # in progress does not start from a blank panel.
        for seg in speech.get("transcripts") or []:
            uuid = str(seg.get("uuid") or "")
            if not uuid:
                continue
            self._segments[uuid] = {
                "uuid": uuid,
                "sig": seg.get("sig"),
                "start_offset": _int(seg.get("start_offset")),
                "end_offset": _int(seg.get("end_offset")),
                "speaker": _speaker(seg),
                "alignment": [
                    {"word": str(w.get("word") or "")}
                    for w in (seg.get("alignment") or [])
                ],
            }
        self._dirty = bool(self._segments)

        params = {"token": token, "speech_id": self.speech_id}
        if self.userid:
            params["userid"] = str(self.userid)
        index = speech.get("pubsub_v2_index")
        if index not in (None, ""):
            params["index"] = str(index)

        self._ws = wsclient.WebSocket.connect(
            WS_BASE + "?" + urllib.parse.urlencode(params), timeout=20.0)
        self.connected = True

    # --------------------------------------------------------------- pump

    def fds(self) -> list:
        return [self._ws] if self._ws and not self._ws.closed else []

    def pump(self) -> None:
        """Read whatever has arrived and fold it in. Never blocks."""
        if not self._ws or self._ws.closed:
            self.connected = False
            return
        while True:
            try:
                frame = self._ws.recv(timeout=0)
            except wsclient.WebSocketClosed as e:
                self.connected = False
                self.error = str(e)
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
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "speech_pieces":
                pieces = (msg.get("speech_pieces") or {}).get("transcript_pieces") or []
                self.apply(pieces)

    def apply(self, pieces: list) -> None:
        """Apply transcript patches. See docs/PROTOCOL.md for the contract."""
        for piece in pieces:
            if not isinstance(piece, dict):
                continue
            uuid = str(piece.get("uuid") or "")
            if not uuid:
                continue

            seg = self._segments.get(uuid)
            if seg is None:
                seg = {
                    "uuid": uuid,
                    "sig": piece.get("sig"),
                    "start_offset": _int(piece.get("start_offset")),
                    "end_offset": _int(piece.get("end_offset")),
                    "speaker": _speaker(piece),
                    "alignment": [],
                }
                self._segments[uuid] = seg
            else:
                # `bs` is the base signature the patch was computed against.
                # A mismatch means we are looking at a patch for a version of
                # this segment we do not have, and applying it would corrupt
                # the text -- so skip it and wait to be resynced.
                if seg.get("sig") != piece.get("bs"):
                    continue
                seg["sig"] = piece.get("sig")
                seg["start_offset"] = _int(piece.get("start_offset"), seg["start_offset"])
                seg["end_offset"] = _int(piece.get("end_offset"), seg["end_offset"])
                spk = _speaker(piece)
                if spk:
                    seg["speaker"] = spk

            words = seg["alignment"]
            # Deletions first, then insertions -- the offsets in each are
            # relative to that stage, not to the original.
            for d in piece.get("deletions") or []:
                begin = _int(d.get("begin"))
                end = _int(d.get("end"))
                if end > begin:
                    del words[begin:end]
            for ins in piece.get("insertions") or []:
                at = _int(ins.get("at"))
                at = max(0, min(at, len(words)))
                new = [{"word": str(w.get("t") or "")} for w in (ins.get("pieces") or [])]
                words[at:at] = new

        # A segment emptied by deletions is gone, not blank.
        for uuid in [u for u, s in self._segments.items() if not s["alignment"]]:
            del self._segments[uuid]
        self._dirty = True

    # ------------------------------------------------------------- render

    def text(self) -> str:
        """The transcript so far, with a speaker label when it changes."""
        segs = sorted(self._segments.values(), key=lambda s: s["start_offset"])
        out: list[str] = []
        last_speaker = None
        for seg in segs:
            words = " ".join(w["word"] for w in seg["alignment"] if w["word"]).strip()
            if not words:
                continue
            speaker = seg.get("speaker") or ""
            if speaker and speaker != last_speaker:
                out.append(f"{speaker}: {words}")
                last_speaker = speaker
            else:
                out.append(words)
        return "\n".join(out)

    def take_if_changed(self) -> str | None:
        """The text, but only when it has actually moved on."""
        if not self._dirty:
            return None
        self._dirty = False
        return self.text()

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self.connected = False


def _int(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _speaker(obj: dict) -> str:
    for key in ("speaker_name", "speaker_model_label", "label"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    sid = obj.get("speaker_id")
    return f"Speaker {sid}" if sid not in (None, "") else ""


def _api_base() -> str:
    # Imported lazily to avoid a circular import with the CLI module.
    return "https://otter.ai/forward/api/v1"
