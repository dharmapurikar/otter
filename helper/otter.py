#!/usr/bin/env python3
"""
otter.py -- the helper process behind the Otter plugin for Omarchy
(io.github.dharmapurikar.otter).

The QML side (Service.qml) never touches the network or a cookie; it runs this
and parses JSON. See docs/ARCHITECTURE.md.

Subcommands:

    status [--n 3]        auth state + recent conversations
    transcript <otid>     plain-text transcript
    search <query>        search conversations (emails match participants)
    summarize <otid>      ask Otter's own LLM proxy about a conversation
    devices               PipeWire inputs, for the microphone picker
    selftest              capture only: no network, audio counted and discarded
    serve                 daemon mode, driven by the QML side over stdin

Contract: **always** print one JSON object to stdout and exit 0, even on
failure, so the panel always has something to render. `ok: false` plus `error`
is how problems are reported; a non-zero exit means the helper itself broke.

Auth and endpoints are documented in docs/AUTH.md and docs/PROTOCOL.md, and
were verified end to end (see AUTH.md "Verified end to end").
"""

from __future__ import annotations

__version__ = "1.0.0"

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

# config dir under ~/.config, keyring label, keyring `application=` attribute
BROWSERS = {
    "chrome":   ("google-chrome",               "Chrome Safe Storage",         "chrome"),
    "chromium": ("chromium",                    "Chromium Safe Storage",       "chromium"),
    "brave":    ("BraveSoftware/Brave-Browser", "Brave Safe Storage",          "brave"),
    "edge":     ("microsoft-edge",              "Microsoft Edge Safe Storage", "microsoft-edge"),
}

OTTER_HOSTS = ("otter.ai", ".otter.ai")
WANT = ("sessionid", "csrftoken")

HOST = "https://otter.ai"
API = HOST + "/forward/api/v1"
LOGIN_CSRF = "https://api.aisense.com/api/v1/login_csrf"

STATE_DIR = os.path.expanduser("~/.local/state/omarchy/otter")
SESSION_FILE = os.path.join(STATE_DIR, "session.json")


class OtterError(Exception):
    """Anything we can explain to the user in the panel."""


class OtterNetworkError(OtterError):
    """The network failed, which says nothing about the session.

    Worth its own type: treating a dropped connection as "signed out" throws
    away good cookies, blanks the panel, and sends the next attempt back to
    the keyring for no reason. Wi-Fi coming and going should not look like
    being logged out.
    """


# --------------------------------------------------------------------- auth


def keyring_secret(keyring_app: str, label: str) -> bytes:
    """The browser's Safe Storage pass-phrase, from the system keyring.

    Only the v1 `application=<name>` item is read. The newer
    `chrome_libsecret_os_crypt_password_v2` item holds a raw 32-byte
    AES-256-GCM key, not a pass-phrase -- running it through PBKDF2 +
    AES-128-CBC would silently derive garbage, so we'd rather fail loudly.
    """
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", "application", keyring_app],
            capture_output=True, timeout=10,
        ).stdout
    except FileNotFoundError:
        raise OtterError("secret-tool not found; install libsecret")
    except subprocess.TimeoutExpired:
        raise OtterError("keyring timed out (is it unlocked?)")
    if not out:
        raise OtterError(
            f"could not read '{label}' from the keyring "
            f"(secret-tool lookup application {keyring_app}); "
            "is the keyring unlocked?"
        )
    return out


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return dec.update(data) + dec.finalize()
    except ImportError:
        pass
    p = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-d", "-nopad",
         "-K", key.hex(), "-iv", iv.hex()],
        input=data, capture_output=True,
    )
    if p.returncode != 0:
        raise OtterError("openssl decrypt failed: "
                         + p.stderr.decode("utf8", "replace").strip())
    return p.stdout


def _printable(b: bytes) -> bool:
    try:
        s = b.decode("utf8")
    except UnicodeDecodeError:
        return False
    return bool(s) and s.isprintable()


def decrypt_cookie(blob: bytes, key: bytes) -> str | None:
    if not blob:
        return None
    if blob[:3] not in (b"v10", b"v11"):
        # Unencrypted (rare) -- take it as-is if it looks sane.
        return blob.decode("utf8", "replace") if _printable(blob) else None
    plain = _aes_cbc_decrypt(key, b" " * 16, blob[3:])
    if not plain:
        return None
    pad = plain[-1]
    if 0 < pad <= 16:
        plain = plain[:-pad]  # PKCS#7
    # Current Chrome prepends a 32-byte SHA-256 domain hash. Verified present
    # on this machine, but handle both shapes.
    if not _printable(plain) and _printable(plain[32:]):
        plain = plain[32:]
    return plain.decode("utf8", "replace")


def read_chrome_cookies(browser: str, profile: str) -> dict[str, str]:
    if browser not in BROWSERS:
        raise OtterError(f"unknown browser {browser!r}")
    config_dir_name, label, keyring_app = BROWSERS[browser]
    src = os.path.expanduser(f"~/.config/{config_dir_name}/{profile}/Cookies")
    if not os.path.exists(src):
        raise OtterError(f"no cookie database at {src}")

    key = hashlib.pbkdf2_hmac("sha1", keyring_secret(keyring_app, label),
                              b"saltysalt", 1, 16)

    # Copy first: a running browser holds a lock on the live DB.
    with tempfile.NamedTemporaryFile(prefix="otter_", delete=False) as fh:
        tmp = fh.name
    try:
        shutil.copy(src, tmp)
        con = sqlite3.connect(tmp)
        try:
            qmarks = ",".join("?" * len(OTTER_HOSTS))
            rows = con.execute(
                f"SELECT name, encrypted_value FROM cookies "
                f"WHERE host_key IN ({qmarks}) AND name IN (?,?)",
                (*OTTER_HOSTS, *WANT),
            ).fetchall()
        finally:
            con.close()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    cookies: dict[str, str] = {}
    for name, blob in rows:
        val = decrypt_cookie(blob, key)
        if val:
            cookies[name] = val
    missing = [c for c in WANT if c not in cookies]
    if missing:
        raise OtterError(
            f"not signed in ({', '.join(missing)} missing) -- sign in to Otter in {browser}"
        )
    return cookies


def save_session(data: dict) -> None:
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
    except OSError:
        pass  # a cache miss is not worth failing a refresh over


def clear_session() -> None:
    """Forget the cached session.

    Called when Otter actually rejects it, so the next attempt goes straight
    to the browser instead of spending a round trip re-probing cookies we
    already know are dead.
    """
    try:
        os.unlink(SESSION_FILE)
    except OSError:
        pass


def load_session() -> dict | None:
    try:
        with open(SESSION_FILE) as fh:
            data = json.load(fh)
        return data if all(k in data.get("cookies", {}) for k in WANT) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------- api


def _headers(cookies: dict[str, str]) -> dict[str, str]:
    """Common request headers.

    `Origin` and `Referer` are not decoration. Otter's API is Django, and
    Django's CSRF middleware checks them on any unsafe (POST) request over
    HTTPS -- without them a perfectly valid session gets a 403. The Chrome
    extension never had to think about this because fetch() from a page
    context sets both automatically; a native client must send them itself.
    """
    return {
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "x-csrftoken": cookies.get("csrftoken", ""),
        "x-source": "http-client",
        "Origin": HOST,
        "Referer": HOST + "/",
        "User-Agent": "io.github.dharmapurikar.otter/1.0.0",
    }


def _auth_error(status: int, raw: bytes, endpoint: str) -> OtterError:
    """Turn a 401/403 into something that names the actual problem.

    Conflating the two sent us chasing an expired session when the real
    answer was a missing CSRF header, so keep them distinct and quote the
    server.
    """
    detail = raw[:200].decode("utf8", "replace").replace("\n", " ").strip()
    if status == 401:
        return OtterError("session expired -- sign in to Otter in your browser")
    if "CSRF" in detail or "csrf" in detail or "Referer" in detail:
        return OtterError(f"rejected by CSRF check on {endpoint}: {detail}")
    return OtterError(f"{endpoint} refused (403)" + (f": {detail}" if detail else ""))


def api_get(url: str, cookies: dict[str, str], timeout: int = 20) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=_headers(cookies))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise OtterNetworkError(f"network unavailable: {e.reason}")
    except TimeoutError:
        raise OtterNetworkError("request timed out")


def api_post(endpoint: str, cookies: dict[str, str],
             params: dict | None = None, data: dict | None = None,
             timeout: int = 20) -> dict:
    """POST to a forward/api/v1 endpoint and return the parsed JSON body.

    Otter takes some arguments as query parameters even on a POST (speech_start
    does), so both are supported. A form-encoded body matches what the
    extension sends.
    """
    url = f"{API}/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode("utf8") if data else b""
    headers = _headers(cookies)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except urllib.error.URLError as e:
        raise OtterNetworkError(f"network unavailable: {e.reason}")
    except TimeoutError:
        raise OtterNetworkError("request timed out")

    if status in (401, 403):
        raise _auth_error(status, raw, endpoint)
    if status != 200:
        detail = raw[:200].decode("utf8", "replace").strip()
        raise OtterError(f"{endpoint} returned {status}"
                         + (f": {detail}" if detail else ""))
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise OtterError(f"{endpoint} returned invalid JSON")
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def api_post_json(endpoint: str, cookies: dict[str, str], payload: dict,
                  timeout: int = 60) -> dict:
    """POST a JSON body. The LLM proxy wants JSON, not form encoding."""
    body = json.dumps(payload).encode("utf8")
    headers = _headers(cookies)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}/{endpoint}", data=body,
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except urllib.error.URLError as e:
        raise OtterNetworkError(f"network unavailable: {e.reason}")
    except TimeoutError:
        raise OtterNetworkError("request timed out")

    if status in (401, 403):
        raise _auth_error(status, raw, endpoint)
    if status != 200:
        detail = raw[:200].decode("utf8", "replace").strip()
        raise OtterError(f"{endpoint} returned {status}"
                         + (f": {detail}" if detail else ""))
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise OtterError(f"{endpoint} returned invalid JSON")
    return parsed if isinstance(parsed, dict) else {"result": parsed}


# Otter's own default when the extension calls its proxy. Left as Otter set it
# rather than pinned by us: it is their quota and their choice of model.
LLM_MODEL = "claude-sonnet-4-6"
LLM_MAX_TOKENS = 2000

SUMMARY_PROMPT = (
    "Summarize this meeting transcript. Give a short paragraph of context, "
    "then bullet points for the key points, then a list of action items with "
    "an owner where one is named. Be concise and do not invent anything that "
    "is not in the transcript."
)


def ask_llm(cookies: dict[str, str], transcript: str, prompt: str = "") -> str:
    """Ask Otter's LLM proxy about a transcript.

    This is Otter's own endpoint and their quota, so it is used sparingly and
    only on transcripts the account already owns.
    """
    question = (prompt or "").strip() or SUMMARY_PROMPT
    # Long meetings can exceed what the proxy will take; trim from the front
    # so the most recent discussion survives.
    body = transcript
    limit = 120000
    if len(body) > limit:
        body = "…(earlier transcript omitted)…\n" + body[-limit:]

    payload = {
        "llm_request": {
            "chat_content": [{"type": "text", "text": f"{question}\n\n{body}"}],
            "sys_content": [],
            "option": {
                "models": [LLM_MODEL],
                "temperature": 0.4,
                "max_tokens": LLM_MAX_TOKENS,
            },
        }
    }
    data = api_post_json("get_llm_proxy_response", cookies, payload)
    content = (data.get("response") or {}).get("content")
    if data.get("status") != "OK" or not content:
        raise OtterError(str(data.get("message") or "the LLM proxy returned nothing"))
    return str(content)


class Api:
    """The surface record.py needs, so it doesn't import this module back."""

    post = staticmethod(api_post)
    get = staticmethod(api_get)


def signed_in(cookies: dict[str, str]) -> bool:
    """Whether Otter still accepts this session.

    Network failures propagate rather than returning False: "I could not ask"
    is not the same answer as "no".
    """
    st, body = api_get(LOGIN_CSRF, cookies, timeout=10)
    if st != 200:
        return False
    try:
        return bool(json.loads(body).get("logged-in"))
    except ValueError:
        return False


def fetch_recent(cookies: dict[str, str], n: int) -> list[dict]:
    # page_size is an upper bound, not a promise; ask for headroom.
    params = urllib.parse.urlencode({
        "funnel": "home_feed",
        "page_size": max(n, 6),
        "source": "home",
        "speech_metadata": "true",
        "use_serializer": "HomeFeedSpeechWithoutSharedGroupsSerializer",
    })
    st, body = api_get(f"{API}/available_speeches?{params}", cookies)
    if st in (401, 403):
        raise _auth_error(st, body, "available_speeches")
    if st != 200:
        raise OtterError(f"available_speeches returned {st}")
    try:
        speeches = json.loads(body).get("speeches", []) or []
    except ValueError:
        raise OtterError("available_speeches returned invalid JSON")

    out = []
    for s in speeches[:n]:
        # The HomeFeed serializer uses `otid`; advanced_search uses
        # `speech_otid`. Accept either so one normalizer covers both.
        otid = s.get("otid") or s.get("speech_otid") or ""
        if not otid:
            continue
        out.append({
            "otid": otid,
            "title": s.get("title") or "",
            "duration": int(s.get("duration") or 0),
            "startTime": int(s.get("displayed_start_time")
                             or s.get("start_time")
                             or s.get("created_at") or 0),
            "url": f"{HOST}/u/{otid}",
        })
    return out


def fetch_transcript(cookies: dict[str, str], otid: str,
                     speaker_timestamps: bool = False) -> str:
    """Plain-text transcript for one conversation.

    Otter returns text, not JSON, already speaker-labelled -- which is why
    this endpoint beats reassembling `transcript_pieces` for anything that is
    not live.
    """
    if not otid:
        raise OtterError("no conversation id")
    params = urllib.parse.urlencode({
        "otid": otid,
        "speaker_names": "true",
        "speaker_timestamps": "true" if speaker_timestamps else "false",
        "branding": "false",
    })
    st, body = api_get(f"{API}/speech_txt?{params}", cookies)
    if st in (401, 403):
        raise _auth_error(st, body, "speech_txt")
    if st != 200:
        raise OtterError(f"speech_txt returned {st}")
    return body.decode("utf8", "replace")


def search(cookies: dict[str, str], query: str, size: int = 10) -> list[dict]:
    """Search conversations. Emails in the query are matched as participants.

    Note the id key: advanced_search returns `speech_otid`, while the home
    feed returns `otid`. Normalizing here keeps that difference out of the UI.
    """
    query = (query or "").strip()
    if not query:
        return []

    emails = re.findall(r"\S+@\S+", query)
    text = re.sub(r"\S+@\S+", "", query).strip()
    params: list[tuple[str, str]] = [("relevance", "true"), ("size", str(size))]
    if text:
        params.append(("query", text))
    for e in emails:
        params.append(("emails", e))

    st, body = api_get(f"{API}/advanced_search?{urllib.parse.urlencode(params)}",
                       cookies)
    if st in (401, 403):
        raise _auth_error(st, body, "advanced_search")
    if st != 200:
        raise OtterError(f"advanced_search returned {st}")
    try:
        hits = json.loads(body).get("hits", []) or []
    except ValueError:
        raise OtterError("advanced_search returned invalid JSON")

    out, seen = [], set()
    for h in hits:
        otid = h.get("speech_otid") or h.get("otid") or h.get("speech_id") or ""
        if not otid or otid in seen:
            continue
        seen.add(otid)
        out.append({
            "otid": otid,
            "title": h.get("title") or "",
            "duration": int(h.get("duration") or 0),
            "startTime": int(h.get("start_time") or h.get("created_at") or 0),
            "url": f"{HOST}/u/{otid}",
        })
    return out


def logout(cookies: dict[str, str]) -> None:
    """End the Otter session.

    Worth being clear-eyed about: this is the *same* session the browser
    holds, so it signs otter.ai out there too. There is no plugin-only
    session to end. The cached copy is dropped either way -- if the call
    fails we still stop using cookies the user has asked us to forget.
    """
    try:
        api_post("logout", cookies)
    except OtterError:
        pass
    finally:
        clear_session()


def user_features(cookies: dict[str, str]) -> dict:
    """Feature flags for the account, including the real recording cap.

    Otter publishes max_recording_duration per account, so honour it instead
    of hardcoding four hours and being wrong for somebody.
    """
    st, body = api_get(f"{API}/user/features", cookies)
    if st != 200:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return ((data.get("user") or {}).get("features") or {}) if isinstance(data, dict) else {}


def user_id(cookies: dict[str, str]) -> str:
    st, body = api_get(f"{API}/user/profile", cookies)
    if st != 200:
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    for key in ("userid", "id"):
        if isinstance(data, dict):
            if key in data:
                return str(data[key])
            user = data.get("user") or {}
            if isinstance(user, dict) and key in user:
                return str(user[key])
    return ""


# ---------------------------------------------------------------- commands


def get_cookies(args) -> dict[str, str]:
    """Cached cookies if they still work, else re-read them from the browser."""
    cached = load_session()
    if cached and not args.no_cache:
        cookies = cached["cookies"]
        if signed_in(cookies):
            return cookies
    cookies = read_chrome_cookies(args.browser, args.profile)
    if not signed_in(cookies):
        raise OtterError("cookies found but Otter reports signed out")
    save_session({"cookies": cookies})
    return cookies


def cmd_status(args) -> dict:
    cookies = get_cookies(args)
    return {
        "ok": True,
        "authenticated": True,
        "recent": fetch_recent(cookies, args.n),
        "error": "",
    }


def cmd_transcript(args) -> dict:
    cookies = get_cookies(args)
    return {"ok": True, "authenticated": True,
            "transcript": fetch_transcript(cookies, args.otid,
                                           getattr(args, "timestamps", False)),
            "error": ""}


def cmd_search(args) -> dict:
    cookies = get_cookies(args)
    return {"ok": True, "authenticated": True,
            "results": search(cookies, args.query, args.n), "error": ""}


def cmd_summarize(args) -> dict:
    cookies = get_cookies(args)
    transcript = fetch_transcript(cookies, args.otid)
    if not transcript.strip():
        raise OtterError("that conversation has no transcript yet")
    return {"ok": True, "authenticated": True,
            "summary": ask_llm(cookies, transcript, args.prompt), "error": ""}


def cmd_devices(args) -> dict:
    import audio
    return {"ok": True, "authenticated": True,
            "devices": audio.list_sources(), "error": ""}


def cmd_selftest(args) -> dict:
    """Exercise the capture pipeline only -- no network, no Otter, no upload.

    Capture has been the failure layer, and proving it in isolation beats
    spending a real recording to find out. Audio is counted and discarded;
    nothing is written to disk and nothing leaves the machine.
    """
    import select as _select
    import subprocess as _sp
    import time as _time

    import audio

    sources, label = audio.resolve_sources(args.mic or "", not args.no_system)
    procs, bytes_per = [], []
    try:
        for src in sources:
            procs.append(_sp.Popen(audio.capture_command(src),
                                   stdout=_sp.PIPE, stderr=_sp.PIPE,
                                   stdin=_sp.DEVNULL, bufsize=0))
            bytes_per.append(0)
        for p in procs:
            os.set_blocking(p.stdout.fileno(), False)
            os.set_blocking(p.stderr.fileno(), False)

        deadline = _time.monotonic() + args.seconds
        peak = 0.0
        while _time.monotonic() < deadline:
            fds = [p.stdout for p in procs if p.poll() is None]
            if not fds:
                break
            ready, _, _ = _select.select(fds, [], [], 0.2)
            for i, p in enumerate(procs):
                if p.stdout in ready:
                    try:
                        data = p.stdout.read(65536)
                    except (BlockingIOError, ValueError):
                        continue
                    if data:
                        bytes_per[i] += len(data)
                        if i == 0:
                            peak = max(peak, audio.rms(data))

        errors = []
        for p in procs:
            try:
                err = (p.stderr.read() or b"").decode("utf8", "replace")
            except (BlockingIOError, ValueError):
                err = ""
            if err.strip():
                errors.append(" ".join(err.split())[:300])
    finally:
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=2)
                except Exception:
                    pass

    expected = int(audio.SAMPLE_RATE * audio.SAMPLE_WIDTH * args.seconds)
    per_source = [
        {"source": s, "bytes": b, "role": "mic (clock)" if i == 0 else "system",
         "flowing": b > 0}
        for i, (s, b) in enumerate(zip(sources, bytes_per))
    ]
    return {
        "ok": bytes_per[0] > 0 if bytes_per else False,
        "authenticated": True,
        "label": label,
        "seconds": args.seconds,
        "expectedBytesPerSource": expected,
        "sources": per_source,
        "micPeakLevel": round(peak, 4),
        "ffmpegMessages": errors,
        "error": "" if (bytes_per and bytes_per[0] > 0)
                 else "microphone capture produced no audio",
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="otter.py")
    ap.add_argument("--browser", default="chrome", choices=list(BROWSERS))
    ap.add_argument("--profile", default="Default")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the cached session and re-read the browser")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="auth state + recent conversations")
    p_status.add_argument("--n", type=int, default=3)
    p_status.set_defaults(func=cmd_status)

    p_tx = sub.add_parser("transcript", help="plain-text transcript for an otid")
    p_tx.add_argument("otid")
    p_tx.add_argument("--timestamps", action="store_true")
    p_tx.set_defaults(func=cmd_transcript)

    p_search = sub.add_parser("search", help="search conversations")
    p_search.add_argument("query")
    p_search.add_argument("--n", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    p_sum = sub.add_parser("summarize", help="summarize a conversation via Otter's LLM")
    p_sum.add_argument("otid")
    p_sum.add_argument("--prompt", default="")
    p_sum.set_defaults(func=cmd_summarize)

    p_dev = sub.add_parser("devices", help="list PipeWire input devices")
    p_dev.set_defaults(func=cmd_devices)

    p_self = sub.add_parser(
        "selftest", help="capture a few seconds locally and report byte counts "
                         "(no network, audio discarded)")
    p_self.add_argument("--seconds", type=float, default=3.0)
    p_self.add_argument("--mic", default="")
    p_self.add_argument("--no-system", action="store_true")
    p_self.set_defaults(func=cmd_selftest)

    # Daemon mode. Unlike the one-shot commands this streams many JSON lines
    # and is driven by commands on stdin, so it bypasses the wrapper below.
    p_serve = sub.add_parser("serve", help="long-running daemon (stdin/stdout JSON)")
    p_serve.add_argument("--n", type=int, default=3)
    p_serve.add_argument("--refresh", type=int, default=300)
    p_serve.set_defaults(func=None)

    args = ap.parse_args()

    if args.cmd == "serve":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import serve
        return serve.run(sys.modules[__name__], args)

    try:
        result = args.func(args)
    except OtterError as e:
        result = {"ok": False, "authenticated": False, "recent": [], "error": str(e)}
    except Exception as e:  # never let the panel see a traceback
        result = {"ok": False, "authenticated": False, "recent": [],
                  "error": f"{type(e).__name__}: {e}"}

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
