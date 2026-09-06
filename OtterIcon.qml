import QtQuick
import qs.Commons

// The Otter mark, as a themable glyph.
//
// A Nerd Font glyph rather than a bundled SVG on purpose: it inherits the
// theme color exactly, scales with the font tokens, and needs no colorization
// pass.
//
// U+F05CB, a person with sound waves: it reads as "someone speaking", which
// is what Otter is for. Chosen over the neighbours it has to sit beside --
// Omarchy's Microphone widget owns 󰍬 and its screen recorder owns 󰻂, and a
// third mic-or-record-dot in the same bar would be unreadable. It also beats
// the speech bubble this started as, which just said "chat". Verified present
// in JetBrainsMono Nerd Font and legible at the bar's 13px.
//
// Swap `glyph` here and `otterGlyph` in BarWidget.qml to change the mark.
// Alternatives that are covered by the font and also work: 󱥢 (waveform bars),
// 󰦨 (transcript lines), 󰕧 (video camera).
Text {
  id: root

  property real iconSize: Style.font.icon
  property string glyph: "󰗋"

  text: glyph
  textFormat: Text.PlainText
  color: Color.foreground
  font.family: Style.font.family
  font.pixelSize: iconSize
  verticalAlignment: Text.AlignVCenter
  horizontalAlignment: Text.AlignHCenter
}
