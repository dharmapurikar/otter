"""
PipeWire/Pulse capture for the Otter plugin.

Otter's ASR wants exactly one shape: **mono signed-16-bit little-endian PCM at
16 000 Hz** (docs/PROTOCOL.md). ffmpeg is asked to deliver precisely that on
stdout, which also makes resampling, channel-downmixing, and -- when both the
mic and the system monitor are captured -- clock drift between two independent
devices someone else's problem. Hand-mixing two `pw-record` pipes would mean
owning all three.

Capturing the output monitor is the piece the Chrome extension cannot do: it
is limited to one tab, while the monitor carries the whole system.
"""

from __future__ import annotations

import array
import math
import subprocess

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # s16le


class AudioError(Exception):
    pass


def _pactl(*args: str) -> str:
    try:
        p = subprocess.run(["pactl", *args], capture_output=True, timeout=10)
    except FileNotFoundError:
        raise AudioError("pactl not found; is PipeWire/Pulse running?")
    except subprocess.TimeoutExpired:
        raise AudioError("pactl timed out")
    if p.returncode != 0:
        raise AudioError("pactl " + " ".join(args) + ": "
                         + p.stderr.decode("utf8", "replace").strip())
    return p.stdout.decode("utf8", "replace").strip()


def default_source() -> str:
    """The system default input (mic)."""
    return _pactl("get-default-source")


def default_sink_monitor() -> str:
    """The monitor of the system default output -- i.e. 'what you can hear'."""
    sink = _pactl("get-default-sink")
    return sink + ".monitor" if sink else ""


def source_index() -> dict[str, str]:
    """Map each source's pactl id to its pulse name.

    `pactl list source-outputs` names only the *id* of the source each app
    stream is bound to, so answering "which mic is Teams using" means joining
    the two lists. The ids are PipeWire globals and are reassigned when a
    device reappears, so the map is only valid for the poll that built it.
    """
    out: dict[str, str] = {}
    try:
        raw = _pactl("-f", "json", "list", "sources")
        import json
        parsed = json.loads(raw)
    except (AudioError, ValueError, TypeError):
        return out
    if isinstance(parsed, list):
        for s in parsed:
            if isinstance(s, dict) and s.get("name") is not None:
                out[str(s.get("index"))] = str(s["name"])
    return out


def list_sources() -> list[dict]:
    """Real inputs, with Pulse's own description as the label.

    Monitors are excluded: they are reachable through the separate
    'capture system audio' toggle, and listing them among microphones would
    make the picker confusing.
    """
    try:
        default = default_source()
    except AudioError:
        default = ""

    out = []
    for name, description in _source_names_and_descriptions():
        if name.endswith(".monitor"):
            continue
        out.append({
            "name": name,
            "label": description or prettify_source(name),
            "isDefault": name == default,
        })
    return out

def mic_captures_from_json(raw: str,
                           exclude_binaries=("ffmpeg",),
                           exclude_name_fragments=("peak detect",
                                                    "quickshell",
                                                    "pavucontrol"),
                           sources_by_id: dict[str, str] | None = None) -> list[dict]:
    """Apps currently capturing a microphone, parsed from
    `pactl -f json list source-outputs` output, as {name, binary} dicts.

    A call client (the Teams PWA, Discord, a Meet tab) opens exactly one of
    these for the whole length of a call and closes it on leave -- the one
    reliable join/leave signal there is. Window titles are not: the Teams
    window keeps its idle title during a call on at least this machine.

    Excluded, because they capture without anyone being in a call:
    Otter's own ffmpeg (or recording would look like a meeting that never
    ends), and level peekers -- Omarchy's own audio panel appears here as
    "Quickshell Peak Detect", and opening it with a meeting window on
    screen must not start a recording. `pactl` prints `null` (not `[]`)
    when there are no source outputs.

    With `sources_by_id` (see `source_index`), each capture also carries the
    pulse name of the source it is bound to as `sourceName` -- which mic the
    app believes it is talking on -- and streams bound to an output monitor
    are dropped entirely: capturing system audio is not using a microphone,
    and a chrome-based screen recorder must not read as joining a meeting.
    An id that resolves to nothing (a device vanishing mid-poll) keeps the
    capture with an empty `sourceName` rather than dropping it, so the
    join/leave signal does not flap.
    """
    import json
    try:
        outputs = json.loads(raw or "null")
    except ValueError:
        return []
    if not isinstance(outputs, list):
        return []
    captures = []
    for out in outputs:
        if not isinstance(out, dict):
            continue
        # pactl 17 names the property dict "properties". Native PipeWire
        # clients (pw-cat, Quickshell) set no application.process.binary;
        # Pulse-protocol clients (Chrome, Electron, native Zoom) do.
        props = out.get("properties") or out.get("props") or {}
        binary = str(props.get("application.process.binary") or "")
        name = str(props.get("application.name") or binary
                  or props.get("node.name") or "")
        if not name:
            continue
        low = name.lower()
        if binary in exclude_binaries or low in exclude_binaries:
            continue
        if any(frag in low for frag in exclude_name_fragments):
            continue
        source_name = ""
        if sources_by_id is not None:
            source_name = sources_by_id.get(str(out.get("source") or ""), "")
            if source_name.endswith(".monitor"):
                continue
        if any(c["name"] == name and c["binary"] == binary
               and c.get("sourceName", "") == source_name
               for c in captures):
            continue
        entry = {"name": name, "binary": binary}
        if sources_by_id is not None:
            entry["sourceName"] = source_name
        captures.append(entry)
    return captures


def mic_captures(exclude_binaries=("ffmpeg",)) -> list[dict]:
    """Live mic-capturing apps, each with the mic it is bound to.
    Raises AudioError off-PipeWire."""
    raw = _pactl("-f", "json", "list", "source-outputs")
    return mic_captures_from_json(raw, exclude_binaries,
                                  sources_by_id=source_index())

def _source_names_and_descriptions() -> list[tuple[str, str]]:
    """(name, description) per source.

    Pulse already knows the human name -- "Blue Microphones Analog Stereo"
    beats anything we could recover from the device id -- so ask for JSON and
    use it. Older pactl builds lack `-f json`, hence the short-list fallback
    with no descriptions.
    """
    import json
    try:
        raw = _pactl("-f", "json", "list", "sources")
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [(str(s.get("name") or ""), str(s.get("description") or ""))
                    for s in parsed if s.get("name")]
    except (AudioError, ValueError, TypeError):
        pass

    out = []
    for line in _pactl("list", "short", "sources").splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            out.append((cols[1], ""))
    return out


def prettify_source(name: str) -> str:
    """Last-resort label when Pulse gives us no description."""
    n = name
    for prefix in ("alsa_input.", "alsa_output."):
        if n.startswith(prefix):
            n = n[len(prefix):]
    n = n.split(".")[0]
    if n.startswith("usb-"):
        n = n[4:]
    parts = [p for p in n.replace("-", "_").split("_") if p and not p.isdigit()]
    kept = [p for p in parts if not (len(p) > 12 and any(c.isdigit() for c in p))]
    return " ".join(kept).strip() or name


def resolve_sources(mic_device: str, capture_system: bool) -> tuple[list[str], str]:
    """Pick the pulse sources to record, plus a short label for the UI.

    An empty `mic_device` means "follow the system default", which is the
    zero-setup path and keeps working when headsets are swapped. A pinned
    device that has since disappeared falls back to the default rather than
    failing the recording outright.
    """
    sources: list[str] = []
    labels: list[str] = []

    available = {s["name"] for s in list_sources()}
    mic = mic_device.strip()
    if mic and mic not in available:
        mic = ""  # unplugged; fall back
    if not mic:
        mic = default_source()
    if mic:
        sources.append(mic)
        labels.append("mic")

    if capture_system:
        monitor = default_sink_monitor()
        if monitor:
            sources.append(monitor)
            labels.append("system")

    if not sources:
        raise AudioError("no audio source available to record")
    return sources, "+".join(labels)


def capture_command(source: str) -> list[str]:
    """An ffmpeg command emitting 16 kHz mono s16le for ONE source on stdout.

    One process per source, mixed by us -- deliberately not a single ffmpeg
    with `amix`. `amix` waits for a frame from every input that has not hit
    EOF, and a monitor source whose sink is SUSPENDED (nothing is playing)
    delivers no frames at all. That stalls the whole graph, so ffmpeg runs
    happily and writes zero bytes for as long as the room is quiet -- which
    is exactly the state a meeting starts in. Observed live: mic RUNNING,
    HDMI monitor SUSPENDED, spool stuck at 0 bytes.

    Driving the mix ourselves makes the microphone the clock and lets a
    silent monitor contribute silence instead of blocking.
    """
    if not source:
        raise AudioError("no source")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        # A generous queue keeps a busy CPU from dropping pulse packets.
        "-thread_queue_size", "1024",
        "-f", "pulse", "-i", source,
        "-ac", str(CHANNELS),
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-",
    ]


def mix(primary: bytes, secondary: bytes) -> bytes:
    """Saturating sum of two s16le buffers, truncated to `primary`'s length.

    `secondary` is padded with silence when short, which is what makes a
    suspended monitor harmless. Saturating rather than averaging: halving
    both inputs to dodge clipping makes quiet speech quieter, and quiet
    speech is what transcription actually struggles with.
    """
    if not secondary:
        return primary
    n = min(len(primary), len(secondary))
    n -= n % SAMPLE_WIDTH
    if n <= 0:
        return primary

    a = array.array("h")
    a.frombytes(primary[:n])
    b = array.array("h")
    b.frombytes(secondary[:n])
    for i in range(len(a)):
        v = a[i] + b[i]
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        a[i] = v
    return a.tobytes() + primary[n:]


def rms(pcm: bytes) -> float:
    """Normalized 0..1 loudness of an s16le buffer, for the level meter.

    Computed here because we already hold the bytes -- no extra process, no
    numpy, and `audioop` is gone as of Python 3.13.
    """
    if len(pcm) < SAMPLE_WIDTH:
        return 0.0
    samples = array.array("h")
    usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return min(1.0, math.sqrt(total / len(samples)) / 32768.0)
