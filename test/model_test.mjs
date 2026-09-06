// Tests for Model.js -- the pure formatting/parsing helpers behind the panel.
//
//   node test/model_test.mjs
//
// Model.js is QML JS (`.pragma library`), which node can't import directly, so
// strip the pragma and evaluate it. Keeping these pure is the whole reason the
// formatting lives in Model.js instead of inline in bindings.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "Model.js"), "utf8")
  .replace(/^\.pragma library\s*$/m, "");

const Model = {};
new Function(
  "exports",
  src + "\nexports.durationText = durationText;" +
        "\nexports.titleText = titleText;" +
        "\nexports.dayText = dayText;" +
        "\nexports.parseStatus = parseStatus;" +
        "\nexports.shortError = shortError;" +
        "\nexports.humanError = humanError;"
)(Model);

let failures = 0;
function eq(got, want, label) {
  const ok = got === want;
  if (!ok) failures++;
  console.log(
    `${ok ? "ok  " : "FAIL"} ${label}: ${JSON.stringify(got)}` +
    (ok ? "" : ` != ${JSON.stringify(want)}`)
  );
}

// Durations arrive in seconds. Sub-minute clips must keep their seconds --
// Otter's feed is full of 6-second recordings and "0m" reads like a bug.
eq(Model.durationText(6), "6s", "6 seconds");
eq(Model.durationText(59), "59s", "59 seconds");
eq(Model.durationText(60), "1m", "exactly a minute");
eq(Model.durationText(2520), "42m", "42 minutes");
eq(Model.durationText(3660), "1h01m", "hour pads minutes");
eq(Model.durationText(0), "", "zero is blank");
eq(Model.durationText(null), "", "null is blank");
eq(Model.durationText("nope"), "", "garbage is blank");

// `title` is nullable in the HomeFeed serializer.
eq(Model.titleText(null), "Untitled", "null title");
eq(Model.titleText("   "), "Untitled", "blank title");
eq(Model.titleText("Standup"), "Standup", "real title");

// Relative day. Pin "now" so these never fail on a calendar boundary.
const now = new Date(2026, 8, 5, 12, 0, 0).getTime();
const at = (y, m, d) => new Date(y, m, d, 9, 0, 0).getTime() / 1000;
eq(Model.dayText(at(2026, 8, 5), now), "Today", "today");
eq(Model.dayText(at(2026, 8, 4), now), "Yesterday", "yesterday");
eq(Model.dayText(at(2026, 8, 1), now), "Tue", "within a week -> weekday");
eq(Model.dayText(at(2026, 6, 4), now), "4 Jul", "older -> date");
eq(Model.dayText(0, now), "", "missing date");

// The helper contracts to print one JSON object, but a stray warning on
// stdout must not break the panel.
eq(Model.parseStatus('{"ok":true,"recent":[]}').ok, true, "clean JSON");
eq(Model.parseStatus('warning: something\n{"ok":true}').ok, true, "ignores leading noise");
eq(Model.parseStatus("").ok, false, "empty output is an error");
eq(Model.parseStatus("not json").ok, false, "garbage is an error");

eq(Model.shortError("a\n\n  b   c"), "a b c", "collapses whitespace");
eq(Model.shortError("x".repeat(200)).length, 120, "caps length");

// Error taxonomy mapping
eq(Model.humanError("auth", "keyring locked"),
   "Signed out of Otter · Sign in via Chrome or the panel", "auth error");
eq(Model.humanError("device", "no mic found"),
   "Microphone error: no mic found", "device error with message");
eq(Model.humanError("device", ""),
   "Microphone unavailable · Check audio settings", "device error empty message");
eq(Model.humanError("network", "timeout"),
   "Network connection failed · Will retry", "network error");
eq(Model.humanError("otter-api", "status 500"),
   "Otter error: status 500", "otter-api error");
eq(Model.humanError("internal", "'Recorder' object has no attribute '_aux_bytes'"),
   "Internal error · See events.log ('Recorder' object has no attribute '_aux_bytes')", "internal error");
eq(Model.humanError("unknown", "weird error"),
   "weird error", "fallback unknown error");

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
