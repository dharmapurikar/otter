# Auth: reading Otter's session from Chrome

## Decision

The plugin does **not** implement its own login. Otter's private API is
authenticated purely by two cookies, and you're already signed in inside
Chrome. So the helper reads those cookies out of Chrome's own cookie store and
reuses them. There is nothing to log into twice.

No separate login exists on purpose, and the two facts that make that work
were both verified on this machine:

- Chrome holds live `sessionid` + `csrftoken` cookies for `otter.ai`.
- The key that decrypts them is present in the running gnome-keyring
  (`secret-tool lookup application chrome` returns it).

## What the API needs

From [PROTOCOL.md](PROTOCOL.md): every REST call is cookie-authed with

- `sessionid` — the session (sent via the cookie header)
- `csrftoken` — also echoed in the `x-csrftoken` request header

Both live on the `otter.ai` domain (and are mirrored to `api.aisense.com`). We
only need those two; every other Otter cookie is analytics noise.

## Where Chrome keeps them

```
~/.config/google-chrome/Default/Cookies      # a SQLite DB
    table cookies(host_key, name, encrypted_value, …)
```

`encrypted_value` is not plaintext. On Linux, current Chrome writes **v11**
blobs (confirmed — every Otter cookie on this machine is `v11`):

- `v10` — AES-128-CBC with a fixed hard-coded key (`peanuts`). Legacy.
- `v11` — AES-128-CBC with a key derived from a random secret Chrome stores in
  the **system keyring** under the "Chrome Safe Storage" item.

## The decryption recipe (v11)

All standard, all reproducible — no reverse engineering needed:

1. **Get the secret** from the keyring:

   ```
   secret-tool lookup application chrome
   ```

   (Equivalently, the libsecret item labelled *Chrome Safe Storage*.) On this
   box gnome-keyring answers it directly.

2. **Derive the AES key:**

   ```
   key = PBKDF2-HMAC-SHA1(secret, salt="saltysalt", iterations=1, dklen=16)
   ```

   `hashlib.pbkdf2_hmac` covers this in the standard library.

3. **Decrypt** each `encrypted_value` (after stripping the 3-byte `v11`
   prefix):

   ```
   AES-128-CBC, key = <above>, IV = 16 spaces (0x20)
   then strip PKCS#7 padding
   ```

4. **Drop the SHA-256 domain prefix.** Recent Chrome prepends a 32-byte hash
   of the cookie's domain to the plaintext before encrypting (a tamper check).
   If present, discard the first 32 bytes; what remains is the cookie value.

   ✅ **Confirmed present** on this machine (Chrome as installed, Sept 2026) —
   the 32-byte strip *is* required. This was the one open unknown in the design;
   it's now settled by `scripts/otter_probe.py`.

No third-party crypto package is required: `hashlib` (stdlib) does the KDF, and
either the `cryptography` wheel or a shelled-out `openssl enc -aes-128-cbc`
does the cipher. The helper prefers `cryptography` if importable and falls back
to `openssl`, so the plugin has no hard Python dependency to install.

### Verified end to end

`scripts/otter_probe.py` was run on this machine and the whole chain works:

| Step | Result |
|---|---|
| `secret-tool lookup application chrome` | 24-byte pass-phrase returned |
| PBKDF2 → AES-128-CBC → PKCS#7 | both `sessionid` and `csrftoken` decrypted |
| 32-byte domain-hash prefix | **present — strip required** |
| cipher backend | `cryptography` absent, so the **openssl fallback** was the path exercised, and it works |
| `GET login_csrf` | `{"status":"OK","logged-in":true}` |
| `GET available_speeches` | `200` |

So the no-dependency posture is not theoretical: the plugin can ship with
stdlib + `openssl` only.

### If Chrome moves to the v2 keyring scheme

Chromium has a newer keyring item, `chrome_libsecret_os_crypt_password_v2`,
which stores a **raw 32-byte key used with AES-256-GCM** — not a PBKDF2
pass-phrase. Pushing that value through the v1 recipe above would silently
derive a wrong key and produce garbage plaintext.

The probe therefore reads **only** the v1 `application=<browser>` item and fails
loudly if it's absent, rather than falling back to v2 and guessing. When a
browser here migrates, the fix is a real GCM branch (detect the item, use the
raw key, AES-256-GCM with the blob's nonce/tag) — not a tweak to the KDF. This
is the concrete shape of the "one-line fix if Chrome changes" caveat below.

## The helper's auth flow

`helper/otter.py` (the daemon in `serve.py` caches the result in
memory and re-runs this path when a real rejection drops it):

```
get_cookies():
    cached = load_session()                   # session.json, 0600
    if cached and probe(cached) ok:           return cached
    secret  = keyring_secret()                # secret-tool lookup
    key     = pbkdf2(secret, "saltysalt", 1, 16)
    cookies = read_chrome_cookies(...)        # copy DB, decrypt v11 blobs
    if not signed_in(cookies):                raise "signed out"
    save_session(cookies)                     # back to session.json
    return cookies

probe():
    GET https://api.aisense.com/api/v1/login_csrf  → {"logged-in": bool}
```

`session.json` (mode `0600`, under `~/.local/state/omarchy/otter/`) caches the
two cookie values so we don't hit the keyring and cookie DB on every bar
interaction. The `userid` recording needs comes from `GET user/profile` and is
kept by the daemon alongside its in-memory cookies.

## Config knobs

- `browser` — which browser holds the session: `chrome` (default),
  `chromium`, `brave`, or `edge`. Each maps to its own config path and
  keyring label (*Chrome Safe Storage*, *Brave Safe Storage*, …); the scheme
  is the same v11 one.
- `chromeProfile` — which profile dir to read (default `Default`).
- The plugin **never writes** to the browser's cookie DB — strictly read-only,
  and it copies the DB to a temp file before opening it so a running
  browser's lock is never an issue.

## What happens when the session expires

Otter sessions are long-lived but not eternal. When the cached cookies stop
working:

1. The next REST call is rejected; the helper drops its cached cookies and
   `session.json`, reports `authenticated:false` with `needsLogin:true`, and
   polls briskly (every 2 s, backing off to 20 s) instead of the idle
   interval. A *network* failure is not treated this way — it keeps the
   cookies and the panel contents, because a flaky connection says nothing
   about the session.
2. The panel shows a "Sign in to Otter" row — a state, not red error text —
   which opens `https://otter.ai/signin`. While signed out the panel keeps
   checking for the new session, and clicking the row polls every 2 s for a
   minute, so signing in is noticed the moment it happens.
3. You sign in in the browser as normal; the helper re-reads the now-valid
   cookies and everything resumes on its own.

Signing out on purpose is in the panel's settings view: it calls Otter's
`logout` endpoint — which ends the browser's session too, hence the two-click
confirm — and forgets the cached session even if that call fails.

No token juggling, no OAuth dance — the browser remains the source of truth
for the session.

## Trade-offs, stated plainly

- **Pro:** zero extra login; survives Chrome restarts; no separate profile to
  maintain; the browser owns re-auth.
- **Con:** depends on Chrome's v11 scheme (could change in a future Chrome and
  need a one-line fix); needs the keyring unlocked (it is, in a normal Omarchy
  session); reads a DB that contains all cookies, though we extract only two and
  never persist the rest.

If Chrome ever changes the scheme, the fallback is the dedicated-login-window
approach (a small owned Chromium profile we sign into once); it's noted here
as the escape hatch but not built.
