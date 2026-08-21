import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Hermes provider usage/balance in the Omarchy bar.
// Data comes from fetch_usage.py (stdlib replication of meviusisback/usage-stats),
// which reads provider API keys from the Hermes profile .env and queries each
// vendor. Click the icon to open the panel; right-click or 'r' refreshes.
Panel {
  id: root
  moduleName: "meviusisback.ai-subs"
  ipcTarget: "meviusisback.ai-subs"
  manageIpc: false
  // Bar.qml sizes each slot from activeItem.implicitWidth/Height; without these
  // the slot collapses to 0x0 and the icon never renders.
  implicitWidth: root.barShowsData ? Math.max(dataButton.implicitWidth, Style.space(120)) : button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color track: Style.selectedFillFor(foreground, Color.accent)

  property var providers: []
  property bool loading: true
  property string errorText: ""

  readonly property int refreshIntervalSec: Math.max(30, Number(root.setting("refreshIntervalSec", 900)) || 900)
  readonly property string hermesEnvFile: root.setting("hermesEnvFile", "~/.hermes/.env")
  readonly property int configuredCount: (root.providers || []).filter(function (p) { return p && p.configured; }).length

  readonly property string barDisplay: String(root.setting("barDisplay", "Icon"))
  readonly property bool barShowsData: root.barDisplay.toLowerCase() === "data"

  readonly property string defaultSubId: String(root.setting("defaultSub", "opencode"))
  // Show the selected sub when it has keys; otherwise fall back to the first
  // configured one so the bar never degrades to a bare placeholder.
  readonly property var defaultSubRecord: (root.providers || []).filter(function (p) { return p && p.id === root.defaultSubId; })[0] || null
  readonly property var defaultSub: root.defaultSubRecord && root.defaultSubRecord.configured
    ? root.defaultSubRecord
    : ((root.providers || []).filter(function (p) { return p && p.configured; })[0] || null)
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  // Compact one-liner for the bar in Data mode: window percents with a short
  // reset countdown per window for subscription-style subs, the balance label
  // otherwise. Reads nowMs (via resetRemainingMs) so the countdowns tick.
  function compactSubText(p) {
    if (!p) return "—"
    if ((p.windows || []).length > 0) {
      var parts = [p.display]
      for (var i = 0; i < p.windows.length; i++) {
        var w = p.windows[i]
        var pct = w && w.percent !== null && w.percent !== undefined ? Math.round(w.percent) + "%" : "—"
        var reset = root.formatResetShort(root.resetRemainingMs(w ? w.resetsAt : null))
        parts.push((w ? w.label : "?") + " " + pct + (reset !== "" ? " (" + reset + ")" : ""))
      }
      return parts.join(" · ")
    }
    return p.display + " " + (p.label || "—")
  }

  // Ticks once a second so reset countdowns stay honest while the panel sits open.
  property double nowMs: Date.now()

  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

  function resetRemainingMs(iso) {
    if (!iso) return -1
    var ms = new Date(iso).getTime()
    return isFinite(ms) ? ms - root.nowMs : -1
  }

  function formatDuration(ms) {
    if (!(ms > 0)) return "now"
    var minutes = Math.floor(ms / 60000)
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return days + "d " + (hours % 24) + "h"
    if (hours > 0) return hours + "h " + (minutes % 60) + "m"
    return Math.max(1, minutes) + "m"
  }

  // Single-unit reset countdown for the compact bar chip: 3h, 2d, 45m.
  function formatResetShort(ms) {
    if (!(ms > 0)) return ""
    var minutes = Math.floor(ms / 60000)
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return days + "d"
    if (hours > 0) return hours + "h"
    return Math.max(1, minutes) + "m"
  }

  // Persist one inline setting. The panel closes first on purpose: every
  // setting here can resize the bar slot (Icon <-> Data, sub switches), and a
  // resize under an open panel drags the popup around and can drop it.
  // Applying after the close keeps the widget swap predictable.
  function persistSetting(key, value) {
    root.close()
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    entry[key] = value
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  // Usage tone mirrors upstream usage-stats thresholds: urgent past 90%,
  // accent past 70%, plain foreground below, dim when there is no data.
  function usageTone(ratio) {
    if (!(ratio >= 0)) return root.dim
    if (ratio >= 0.9) return root.urgent
    if (ratio >= 0.7) return Color.accent
    return root.foreground
  }

  function refresh() {
    if (!fetchProc.running) fetchProc.running = true
  }

  function scriptPath() {
    return Qt.resolvedUrl("fetch_usage.py").toString().replace(/^file:\/\//, "")
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  IpcHandler {
    enabled: !root.manageIpc && root.ipcTarget !== ""
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
  }

  Process {
    id: fetchProc
    running: true
    command: ["python3", root.scriptPath(), "--env", root.hermesEnvFile]
    stdout: StdioCollector {
      waitForEnd: true
      // streamFinished passes no argument: `text` is the collector's own
      // property. A handler parameter would shadow it with undefined.
      onStreamFinished: {
        var output = text || ""
        try {
          var data = JSON.parse(output)
          root.providers = data.providers || []
          root.errorText = data.error || ""
        } catch (e) {
          console.warn("hermes-usage: unparseable output:", output.slice(0, 200))
          root.errorText = "parse-error"
        }
        root.loading = false
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("hermes-usage:", text.trim())
    }
    onExited: function (code) {
      root.loading = false
      if (code !== 0 && root.providers.length === 0) {
        root.errorText = "fetch-failed-" + code
        console.warn("hermes-usage: fetch exited with code", code)
      }
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    visible: !root.barShowsData
    bar: root.bar
    text: "Σ"
    tooltipText: "AI Subs"
    active: root.errorText !== "" && root.errorText.indexOf("fetch-failed") === 0
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else root.toggle()
    }
  }

  // Data mode: compact one-liner for the default sub instead of the glyph.
  WidgetButton {
    id: dataButton
    anchors.fill: parent
    visible: root.barShowsData
    bar: root.bar
    text: root.compactSubText(root.defaultSub)
    fontSize: Style.font.caption
    tooltipText: "AI Subs"
    active: root.errorText !== "" && root.errorText.indexOf("fetch-failed") === 0
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else root.toggle()
    }
  }

  // Rounded track showing how much of an allowance is used.
  component Meter: Item {
    id: meter

    property real ratio: -1
    property real thickness: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))

    implicitHeight: thickness

    Rectangle {
      id: meterTrack
      anchors.fill: parent
      radius: height / 2
      color: root.track
    }

    Rectangle {
      anchors.left: meterTrack.left
      anchors.verticalCenter: meterTrack.verticalCenter
      height: meterTrack.height
      radius: meterTrack.radius
      width: meterTrack.width * root.clamp(meter.ratio, 0, 1)
      color: root.usageTone(meter.ratio)

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }
  }

  // Small segmented-control chip for the in-panel settings rows.
  component ModeChip: Rectangle {
    id: chip

    property string label: ""
    property bool selected: false
    signal picked()

    implicitWidth: chipLabel.implicitWidth + Style.space(10)
    implicitHeight: chipLabel.implicitHeight + Style.space(6)
    radius: height / 2
    color: chip.selected ? root.foreground : root.alpha(root.foreground, 0.08)

    Text {
      id: chipLabel
      anchors.centerIn: parent
      text: chip.label
      color: chip.selected ? Color.background : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: chip.selected
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: chip.picked()
    }
  }

  // One subscription: highlighted header box, then per-window meters with
  // live reset countdowns for subscription-style subs; balance subs are the
  // box only.
  component ProviderBlock: Column {
    id: block

    required property var modelData
    spacing: Style.space(4)

    readonly property var p: block.modelData || {}
    readonly property bool hasWindows: (block.p.windows || []).length > 0

    Rectangle {
      width: parent.width
      implicitHeight: Math.max(badge.implicitHeight, valueText.implicitHeight) + Style.space(12)
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.07)

      Text {
        id: badge
        anchors.left: parent.left
        anchors.leftMargin: Style.space(10)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(40)
        text: block.p.display
        color: Color.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.subtitle
        font.bold: true
      }

      Text {
        anchors.left: badge.right
        anchors.right: valueText.visible ? valueText.left : parent.right
        anchors.rightMargin: Style.space(10)
        anchors.verticalCenter: parent.verticalCenter
        text: block.p.name
        textFormat: Text.PlainText
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      Text {
        id: valueText
        // Multi-window subs have no meaningful single percentage; the three
        // window meters below are the real numbers.
        visible: !block.hasWindows || !!block.p.error
        anchors.right: parent.right
        anchors.rightMargin: Style.space(10)
        anchors.verticalCenter: parent.verticalCenter
        text: block.p.label || (block.p.error ? block.p.error : "—")
        textFormat: Text.PlainText
        color: block.p.error ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.subtitle
        font.bold: true
      }
    }

    Column {
      visible: block.hasWindows
      width: parent.width
      spacing: Style.space(6)

      Repeater {
        model: block.hasWindows ? block.p.windows : []

        Column {
          id: winBlock
          required property var modelData
          width: parent.width
          spacing: Style.space(2)

          readonly property bool hasPct: winBlock.modelData && winBlock.modelData.percent !== null && winBlock.modelData.percent !== undefined
          readonly property real ratio: winBlock.hasPct ? Number(winBlock.modelData.percent) / 100.0 : -1

          Row {
            width: parent.width
            spacing: Style.space(8)

            Text {
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(48)
              text: winBlock.modelData.label
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Meter {
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - Style.space(48) - Style.space(40) - parent.spacing * 2
              ratio: winBlock.ratio
            }

            Text {
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(40)
              horizontalAlignment: Text.AlignRight
              text: winBlock.hasPct ? Math.round(winBlock.modelData.percent) + "%" : "—"
              color: root.usageTone(winBlock.ratio)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
          }

          Text {
            visible: root.resetRemainingMs(winBlock.modelData.resetsAt) > 0
            width: parent.width
            leftPadding: Style.space(48)
            horizontalAlignment: Text.AlignRight
            text: {
              var remaining = root.resetRemainingMs(winBlock.modelData.resetsAt)
              return remaining > 0 ? "resets in " + root.formatDuration(remaining) : ""
            }
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }

  }

  // Panel anchor pinned to the slot's right edge. The right section is
  // right-aligned past fixed-width neighbors, so this point stays put on
  // screen when the slot resizes between Icon/Data modes or across subs —
  // anchoring to the widget itself dragged the open panel sideways.
  Item {
    id: panelAnchor
    anchors.right: parent.right
    width: 1
    height: parent.height
  }

  KeyboardPanel {
    id: panel
    anchorItem: panelAnchor
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: Style.space(340)
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onActivateRequested: root.refresh()
      onCloseRequested: root.close()
      onTextKey: function (t) { if (t === "r" || t === "R") root.refresh() }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          Text {
            width: parent.width
            text: "AI SUBS"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Text {
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(40)
              text: "Bar"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            ModeChip {
              label: "Icon"
              selected: !root.barShowsData
              onPicked: root.persistSetting("barDisplay", "Icon")
            }

            ModeChip {
              label: "Data"
              selected: root.barShowsData
              onPicked: root.persistSetting("barDisplay", "Data")
            }
          }

          Row {
            visible: root.barShowsData && root.configuredCount > 0
            width: parent.width
            spacing: Style.space(4)

            Text {
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(40)
              text: "Sub"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Repeater {
              model: (root.providers || []).filter(function (p) { return p && p.configured; })

              Item {
                required property var modelData
                width: subChip.implicitWidth
                height: subChip.implicitHeight

                ModeChip {
                  id: subChip
                  anchors.fill: parent
                  label: modelData ? modelData.display : "—"
                  selected: root.defaultSubId === (modelData ? modelData.id : "")
                  onPicked: if (modelData) root.persistSetting("defaultSub", modelData.id)
                }
              }
            }
          }

          Text {
            visible: root.loading
            width: parent.width
            text: "Loading…"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          Text {
            visible: !root.loading && root.configuredCount === 0 && root.errorText === ""
            width: parent.width
            text: "No Hermes provider keys configured in " + root.hermesEnvFile + ".\nAdd keys (e.g. OPENCODE_GO_API_KEY) to see usage."
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Text {
            visible: !root.loading && root.configuredCount === 0 && root.errorText !== ""
            width: parent.width
            topPadding: Style.space(12)
            text: "Fetch failed (" + root.errorText + ").\nDetails: journalctl --user | grep hermes-usage"
            textFormat: Text.PlainText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: root.providers.filter(function (p) { return p && p.configured; })

            ProviderBlock { width: column.width }
          }

          Text {
            visible: !root.loading && root.configuredCount > 0
            width: parent.width
            topPadding: Style.space(6)
            text: "Right-click or press R to refresh"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }
        }
      }
    }
  }
}
