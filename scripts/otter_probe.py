#!/usr/bin/env python3
"""
Otter plugin M0 spike: prove the two unknowns end to end.

  1. Decrypt Chrome's v11-encrypted Otter cookies (sessionid, csrftoken)
     using the key from the system keyring.
  2. Use them to hit the real Otter private API:
       - GET login_csrf        -> are we signed in?
       - GET available_speeches -> print the recent conversations.

Read-only. Touches nothing but Chrome's cookie DB (copied to a temp file
first) and the network. Stdlib only, except the cipher, which uses the
`cryptography` package if importable and otherwise shells out to `openssl`.

See docs/AUTH.md and docs/PROTOCOL.md for the why behind every step.

Usage:
    python3 scripts/otter_probe.py [--profile Default] [--browser chrome] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

# Per Chromium-family browser: config dir under ~/.config, the human-readable
# keyring label, and the `application=` attribute its keyring item is stored
# under (which is not always derivable from the config dir name).
BROWSERS = {
    "chrome":   ("google-chrome",               "Chrome Safe Storage",        "chrome"),
    "chromium": ("chromium",                    "Chromium Safe Storage",      "chromium"),
    "brave":    ("BraveSoftware/Brave-Browser", "Brave Safe Storage",         "brave"),
    "edge":     ("microsoft-edge",              "Microsoft Edge Safe Storage", "microsoft-edge"),
}

OTTER_HOSTS = ("otter.ai", ".otter.ai")
WANT = ("sessionid", "csrftoken")

API = "https://otter.ai/forward/api/v1"
LOGIN_CSRF = "https://api.aisense.com/api/v1/login_csrf"
RECENT = (
    API + "/available_speeches"
    "?funnel=home_feed&page_size=6&source=home&speech_metadata=true"
    "&use_serializer=HomeFeedSpeechWithoutSharedGroupsSerializer"
)


def keyring_secret(keyring_app: str, label: str) -> bytes:
    """Fetch the browser's 'Safe Storage' pass-phrase from the keyring.

    Only the v1 (`application=<name>`) item is accepted. Chromium's newer
    `chrome_libsecret_os_crypt_password_v2` item is deliberately NOT consulted:
    it holds a raw 32-byte key for AES-256-GCM, not a pass-phrase, so feeding
    it through PBKDF2 + AES-128-CBC would silently derive garbage. If the v1
    item ever disappears we want a loud failure, not a wrong answer.
    """
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", "application", keyring_app],
            capture_output=True, timeout=10,
        ).stdout
    except FileNotFoundError:
        raise SystemExit("secret-tool not found (install libsecret).")
    if out:
        return out
    raise SystemExit(
        f"Could not read '{label}' from the keyring.\n"
        f"Tried: secret-tool lookup application {keyring_app}\n"
        "Is the keyring unlocked? If this browser has moved to the v2 scheme\n"
        "(chrome_libsecret_os_crypt_password_v2 = raw AES-256-GCM key), this\n"
        "script needs the GCM path adding -- see docs/AUTH.md."
    )


def derive_key(secret: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", 1, 16)


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return dec.update(data) + dec.finalize()
    except ImportError:
        p = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-nopad",
             "-K", key.hex(), "-iv", iv.hex()],
            input=data, capture_output=True,
        )
        if p.returncode != 0:
            raise SystemExit("openssl decrypt failed: " + p.stderr.decode("utf8", "replace"))
        return p.stdout


def decrypt_cookie(blob: bytes, key: bytes) -> str | None:
    if not blob or blob[:3] not in (b"v10", b"v11"):
        return None  # unencrypted or unknown scheme
    plain = _aes_cbc_decrypt(key, b" " * 16, blob[3:])
    if not plain:
        return None
    pad = plain[-1]
    if 0 < pad <= 16:
        plain = plain[:-pad]  # strip PKCS#7

    def printable(b: bytes) -> bool:
        try:
            s = b.decode("utf8")
        except UnicodeDecodeError:
            return False
        return bool(s) and s.isprintable()

    # Newer Chrome prepends a 32-byte SHA-256 domain hash before the value.
    if not printable(plain) and printable(plain[32:]):
        plain = plain[32:]
    return plain.decode("utf8", "replace")


def read_cookies(config_dir: str, profile: str, key: bytes) -> dict[str, str]:
    src = os.path.join(config_dir, profile, "Cookies")
    if not os.path.exists(src):
        raise SystemExit(f"No cookie DB at {src}")
    # Copy first: a running Chrome holds a lock on the live DB.
    with tempfile.NamedTemporaryFile(prefix="otter_cookies_", delete=False) as fh:
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
        os.unlink(tmp)
    got: dict[str, str] = {}
    for name, blob in rows:
        val = decrypt_cookie(blob, key)
        if val:
            got[name] = val
    return got


def api_get(url: str, cookies: dict[str, str]) -> tuple[int, bytes]:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(url, headers={
        "Cookie": cookie_header,
        "x-csrftoken": cookies.get("csrftoken", ""),
        "x-source": "http-client",
        "User-Agent": "otter-plugin-probe/0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def fmt_duration(sec) -> str:
    """Human duration. Sub-minute clips keep their seconds -- Otter's feed is
    full of 6-second test recordings, and '0m' reads like a parsing bug."""
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "?"
    if sec < 60:
        return f"{sec}s"
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", default="chrome", choices=list(BROWSERS))
    ap.add_argument("--profile", default="Default")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    config_dir_name, label, keyring_app = BROWSERS[args.browser]
    config_dir = os.path.expanduser(f"~/.config/{config_dir_name}")

    key = derive_key(keyring_secret(keyring_app, label))
    cookies = read_cookies(config_dir, args.profile, key)

    result: dict = {"cookies_found": sorted(cookies)}
    missing = [c for c in WANT if c not in cookies]
    if missing:
        result["error"] = f"missing cookies: {missing} (sign in to Otter in {args.browser})"
        print(json.dumps(result, indent=2))
        return 1

    st, body = api_get(LOGIN_CSRF, cookies)
    try:
        result["login_csrf"] = {"status": st, **json.loads(body)}
    except json.JSONDecodeError:
        result["login_csrf"] = {"status": st, "raw": body[:200].decode("utf8", "replace")}

    st, body = api_get(RECENT, cookies)
    if st != 200:
        result["recent_error"] = {"status": st, "raw": body[:300].decode("utf8", "replace")}
        print(json.dumps(result, indent=2))
        return 1

    speeches = json.loads(body).get("speeches", [])
    # Confirmed against live data: the HomeFeed serializer returns `otid`
    # (there is no `speech_otid` key here -- that name is the advanced_search
    # shape), `title` may be null, and `duration` is in seconds.
    recent = [{
        "otid": s.get("otid") or s.get("speech_otid"),
        "title": s.get("title") or "Untitled",
        "duration": fmt_duration(s.get("duration")),
    } for s in speeches[:3]]
    result["recent"] = recent
    result["feed_total"] = len(speeches)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"cookies: {', '.join(result['cookies_found'])}")
        print(f"login_csrf: {result['login_csrf']}")
        print(f"recent (feed returned {len(speeches)}):")
        for r in recent:
            print(f"  - {r['title']:<40} {r['duration']:>6}  {r['otid']}")
        if not recent:
            print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
