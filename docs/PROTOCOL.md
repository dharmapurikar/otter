# Otter.ai private API — as used by the Chrome extension

Reverse-engineered from the installed extension
`bnmojkbbkkonlmlfgejehefjldooiedp` v3.10.1 (unpacked at
`~/.config/google-chrome/Default/Extensions/`). Everything below is read
out of the shipped bundles, not guessed.

Unofficial and unversioned: Otter can change any of it without notice.

## Hosts

| Host | Role |
|---|---|
| `https://otter.ai/forward/api/v1/` | REST, cookie-authed (what the extension actually calls) |
| `https://api.aisense.com/api/v1/` | same REST API, direct origin |
| `wss://ws.aisense.com/api/v2/client` | audio upload / live ASR |
| `wss://ws.aisense.com/api/v2/client/speech_update` | live transcript subscription |

## Auth

`Origin` and `Referer` are required on any unsafe (POST) request. Otter's API
is Django, and its CSRF middleware validates them over HTTPS — without them a
perfectly valid session gets a 403 that looks exactly like an expired one.
The extension never had to send them because `fetch()` from a page context
does it automatically. This cost real debugging time; do not omit them.

Pure cookie auth. Two cookies on the `otter.ai` domain:

- `sessionid` — the session
- `csrftoken` — echoed back in the `x-csrftoken` request header

Every REST call is:

```
GET|POST https://otter.ai/forward/api/v1/<endpoint>
  credentials: include            # sends sessionid
  x-csrftoken: <csrftoken cookie>
  x-client-version: v3.95.0       # sent on some calls
  x-source: http-client
```

The extension also mirrors both cookies onto `api.aisense.com` and calls
`GET /api/v1/login_csrf`, which returns `{"logged-in": bool, "status": ...}` —
a cheap "am I still signed in?" probe.

## Endpoint table

Names come from a single enum in the bundle:

```
login_csrf              speech                   speech_start
speech_finish           user/profile             user/features
user/experiments        subscription             unlock_full_transcript
update_notification_setting                      get_notification_settings
update_annotation       remove_annotation        check_va_autojoin_status
update_agenda_settings  manage_virtual_assistant logout
meeting
```

Plus these, seen used directly:

| Endpoint | Purpose |
|---|---|
| `available_speeches?funnel=home_feed&page_size=6&source=home&speech_metadata=true&use_serializer=HomeFeedSpeechWithoutSharedGroupsSerializer` | the home feed → `{speeches:[…]}` |
| `speech_txt?otid=<otid>&speaker_names=true&speaker_timestamps=<bool>&branding=false` | **plain-text transcript** |
| `advanced_search?query=<q>&relevance=true&size=<n>&emails=<e>` | search → `{hits:[{speech_otid,title,start_time,duration}]}` |
| `get_llm_proxy_response` | Otter's own LLM proxy (defaults to `claude-sonnet-4-6`). **JSON body**, unlike the form-encoded endpoints |
| `user/features` | account feature flags, including `max_recording_duration` (14400 here) — honour it rather than hardcoding |
| `check_va_autojoin_status` | **not** a global calendar toggle: bare, it answers `{"status":"failed","message":"Missing conferencing URL."}`. It is a per-meeting check that wants the meeting URL |

### Feed item shape (verified against live data)

The two serializers use **different id key names** — worth knowing before you
write a normalizer:

| Endpoint | Id key |
|---|---|
| `available_speeches` (HomeFeed serializer) | `otid` — there is **no** `speech_otid` key here |
| `advanced_search` | `speech_otid` |

Also confirmed live: `title` **can be `null`** (render a placeholder, don't
assume a string), and `duration` is in **seconds** (short recordings are
genuinely 6s, so format sub-minute values with seconds or they look like a
parsing bug). `page_size` is an upper bound, not a promise — asking for 6 can
return 2.

Other fields observed: `displayed_start_time` / `start_time` / `created_at`,
`speakers[]`.

## Recording: upload protocol

Audio is **raw mono PCM, signed 16-bit little-endian, 16 000 Hz**. The
constant `16e3` appears as both the `AudioContext` sample rate and a
standalone `SAMPLE_RATE`. Frames are 1024 samples (2048 bytes).
Max recording length is 14400 s (4 h).

1. **Start the speech** — REST:

   ```
   POST /forward/api/v1/speech_start
     ?appid=<appid>&uuid=<random-uuid>&userid=<userid>
     &start_time=<unix-seconds>&ignore_event=true&otid=<client-otid>
   → { ws_url, speech_id, otid }
   ```

   - `appid` is one of `otter-google-meet` or `chrome-ext-non-meeting`
   - `uuid` and `otid` are client-generated random UUID-shaped hex strings
   - `userid` comes from `GET user/profile`

2. **Open the socket** to the returned `ws_url` and send a text frame:

   ```json
   {"action":"start","speech_id":"<speech_id>","offset":0}
   ```

3. **Stream** the PCM as binary frames.

4. **Acks** arrive as text frames:

   ```json
   {"type":"ack","result":{"processed_offset":<n>}}
   ```

   `processed_offset` counts **samples**, not bytes — the client frees
   `2 * delta` bytes from its retry buffer. Next send offset is `n + 1`.
   Reconnecting and re-sending `start` with the last offset resumes an
   interrupted upload, so the buffer is the crash-safety net.

5. **Stop** — text frame, then close:

   ```json
   {"action":"stop","speech_id":"<speech_id>"}
   ```

6. **Finish the speech** — REST:

   ```
   POST /forward/api/v1/speech_finish
     body: {appid, userid, otid, start_time, end_time}   # times unix seconds, as strings
   ```

The extension mixes tab-capture audio with an optional microphone stream
before the worklet. A native client mixes PipeWire sources instead, and can
take the *system output monitor* — strictly more than Chrome can capture.

## Live sessions, and reaping the strays

Verified 2026-09-07 by starting a real recording and watching it, then by
abandoning one deliberately.

A recording in progress reports:

| Field | Live | Finished | Opened, never fed |
|---|---|---|---|
| `live_status` | `live` | `none` | `none` |
| `speech_processing_state` | `RECORDING` | `ALL_DONE` | `UNSPECIFIED` |
| `process_finished` | `false` | `true` | `false` |
| `end_time` | `0` | `0` | `0` |

Both discriminating fields appear on the **home feed** serializer as well, so
enumerating live sessions costs no extra call. `process_finished` is `null` in
the feed and only filled in by `GET speech?otid=`. The flip happens within
~2 s of a clean stop.

**`end_time` is not a liveness signal.** It reads `0` on every speech in both
serializers, finished or not. Reading it as one calls the whole account live.

Three findings that shape how a stray is cleaned up:

1. **An abandoned speech stays live forever.** Drop the socket without a stop
   frame and it still reports `live_status: live` indefinitely — measured well
   past the point any timeout would have fired. It does not self-close.
2. **`speech_finish` does not end a session.** The call answers `OK` and the
   speech carries on `RECORDING`. Finish is metadata; the websocket
   `{"action":"stop"}` frame is the stop.
3. **The stop frame is ignored on an unbound socket.** Reconnecting and
   sending only `stop` left the speech live. The socket must first be bound
   with `{"action":"start", speech_id, offset}`.

And the one that makes recovery possible at all:

**`speech_start` is idempotent on `otid`.** Hand it an otid that already
exists and it returns the *same* `speech_id` with a fresh record-scoped
`ws_url` (the JWT carries `"permission":"record"` and that `speech_id`). So an
orphan can be adopted from nothing but its otid — no need to have persisted a
socket URL or token:

```
POST speech_start?...&otid=<existing otid>   → same speech_id, new ws_url
ws:  {"action":"start","speech_id":…,"offset":<acked samples>}
ws:  <replay whatever the spool still holds>
ws:  {"action":"stop","speech_id":…}
POST speech_finish
→ live_status live → none, RECORDING → ALL_DONE
```

Only ever do this to a speech confirmed live. Re-opening a *finished* one puts
it back into `RECORDING`, which is worse than the orphan.

`helper/otter.py live` lists live sessions and `helper/otter.py reap [otid…|
--all]` closes them; the daemon does the same sweep on startup and before
every start (see ARCHITECTURE.md, "One recording at a time").

## Live transcript: subscribe protocol

`GET speech?otid=<otid>` returns `{"speech": {...}}`. Verified live; the
fields that matter:

| Field | Note |
|---|---|
| `pubsub_jwt_persistent` | the subscription token (~356 chars) |
| `speech_id` | a short internal id such as `2OQUSKHEHMKZFAXV` — **not** the otid |
| `pubsub_v2_index` | often `null`; omit the `index` param when it is |
| `transcripts[]` | what already exists, useful for seeding |
| `live_status`, `speech_processing_state` | whether it is still running — **not** `end_time`, see below |

A transcript segment carries `uuid`, `sig`, `start_offset`, `end_offset`,
`speaker_id`, `speaker_model_label`, a convenience `transcript` string, and
`alignment[]` of `{word, start, end, startOffset, endOffset}`.

Build:

```
wss://ws.aisense.com/api/v2/client/speech_update
  ?index=<pubsub_v2_index>
  &token=<pubsub_jwt_persistent>
  &speech_id=<speech_id>
  &userid=<userid>
```

Incoming JSON frames are discriminated on `type`:

- `speech_pieces` → `speech_pieces.transcript_pieces[]`
- `annotation` → `{action:"create"|"delete", annotations:[…]}`
- `freemium_status`
- `virtual_assistant`

### Applying `transcript_pieces`

Each piece is an **incremental patch** against a transcript keyed by `uuid`:

```
{ uuid, start_offset, end_offset, speaker_id, speaker_model_label,
  sig, bs, id, sdk_speaker,
  deletions: [ {begin, end} ],
  insertions: [ {at, pieces: [ {s, e, t} ]} ] }
```

- unknown `uuid` → create a new segment with an empty `alignment[]`
- known `uuid` → **skip the patch unless `existing.sig === incoming.bs`**
  (`bs` is the base signature; this is the optimistic-concurrency check),
  then adopt the new `sig` and offsets
- apply `deletions` first: `alignment.splice(begin, end - begin)`
- then `insertions`: `alignment.splice(at, 0, ...pieces)` where each
  `{s,e,t}` becomes `{start: s/1000, end: e/1000, word: t}` — `s`/`e` are
  milliseconds, `t` is the word
- drop segments whose `alignment` is now empty, then sort by `start_offset`

## What this means for a native client

No Chrome, no extension, no bot. Given the two cookies, a native process can
list and search conversations, pull transcripts as text, subscribe to a live
transcript, record from PipeWire straight into Otter's ASR, and query Otter's
LLM proxy about a meeting.
