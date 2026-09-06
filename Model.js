.pragma library

// Pure helpers for the Otter panel. No QML types, no side effects, so this
// stays trivially testable and cheap to call from bindings.

// Parse one JSON object from the helper. The helper contracts to always print
// valid JSON, but a crashed interpreter or a stray warning on stdout would
// break that, so treat unparseable output as an error rather than throwing
// inside a binding.
function parseStatus(raw) {
  var text = String(raw || "").trim()
  if (text === "") return { ok: false, error: "No response from helper" }
  // Be forgiving about anything printed before the JSON (a python warning,
  // say): take the last line that parses as an object.
  var lines = text.split("\n")
  for (var i = lines.length - 1; i >= 0; i--) {
    var line = lines[i].trim()
    if (line.charAt(0) !== "{") continue
    try {
      var parsed = JSON.parse(line)
      if (parsed && typeof parsed === "object") return parsed
    } catch (e) {
      // keep looking
    }
  }
  return { ok: false, error: "Unreadable helper output" }
}

// Durations come back in seconds. Sub-minute clips keep their seconds --
// Otter is full of 6-second recordings and "0m" reads like a bug.
function durationText(seconds) {
  var s = Number(seconds)
  if (!isFinite(s) || s <= 0) return ""
  if (s < 60) return Math.round(s) + "s"
  var mins = Math.floor(s / 60)
  var h = Math.floor(mins / 60)
  var m = mins % 60
  if (h > 0) return h + "h" + (m < 10 ? "0" + m : m) + "m"
  return m + "m"
}

// `title` is nullable in the feed.
function titleText(title) {
  var t = String(title || "").trim()
  return t === "" ? "Untitled" : t
}

// Short relative day, in the spirit of the bar's other panels: Today /
// Yesterday / weekday for the last week / a date beyond that.
function dayText(unixSeconds, nowMs) {
  var secs = Number(unixSeconds)
  if (!isFinite(secs) || secs <= 0) return ""
  var then = new Date(secs * 1000)
  var now = nowMs ? new Date(nowMs) : new Date()

  var startOf = function (d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() }
  var dayMs = 86400000
  var diffDays = Math.round((startOf(now) - startOf(then)) / dayMs)

  if (diffDays <= 0) return "Today"
  if (diffDays === 1) return "Yesterday"
  if (diffDays < 7) return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][then.getDay()]
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  return then.getDate() + " " + months[then.getMonth()]
}

// Collapse whitespace and cap length so a long API error can't stretch the
// panel or wrap into a wall of text.
function shortError(text, limit) {
  var value = String(text || "").replace(/\s+/g, " ").trim()
  var max = limit || 120
  return value.length > max ? value.substring(0, max - 1) + "…" : value
}

// Map an error class and message to an actionable, user-facing description.
// Keeps technical traces (AttributeError, KeyError) from leaking into toasts
// and panel text, pointing to the events log instead.
function humanError(errorClass, rawMessage) {
  var raw = shortError(rawMessage || "")
  var cls = String(errorClass || "").toLowerCase()
  switch (cls) {
  case "auth":
    return "Signed out of Otter · Sign in via Chrome or the panel"
  case "device":
    return raw !== "" ? "Microphone error: " + raw : "Microphone unavailable · Check audio settings"
  case "network":
    return "Network connection failed · Will retry"
  case "otter-api":
    return raw !== "" ? "Otter error: " + raw : "Otter.ai rejected request"
  case "internal":
    return "Internal error · See events.log" + (raw !== "" ? " (" + raw + ")" : "")
  default:
    return raw !== "" ? raw : "Unknown error"
  }
}

