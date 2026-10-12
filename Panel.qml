import QtQuick
import QtQuick.Controls
import QtQuick.Effects
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
  property var cnHolidays: []
  property var cnHolidaysYears: []
  property bool loading: true
  property string errorText: ""
  property bool settingsOpen: false

  readonly property int refreshIntervalSec: Math.max(30, Number(root.setting("refreshIntervalSec", 900)) || 900)
  readonly property string hermesEnvFile: root.setting("hermesEnvFile", "~/.hermes/.env")
  readonly property int configuredCount: (root.providers || []).filter(function (p) { return p && p.configured; }).length

  // Hero subtitle under the panel title — mirrors the tracked-out caption the
  // built-in panels show ("Audio"/volume line): live summary when data exists.
  readonly property string heroCaption: {
    if (root.loading) return "Fetching usage…"
    if ((root.providers || []).length === 0 && root.errorText === "") return "No data yet"
    var n = root.configuredCount
    if (n === 0) return root.errorText !== "" ? "Fetch failed" : "No provider keys"
    return n + (n === 1 ? " subscription" : " subscriptions")
  }

  readonly property string barDisplay: String(root.setting("barDisplay", "Data"))
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
      var parts = []
      for (var i = 0; i < p.windows.length; i++) {
        var w = p.windows[i]
        var pct = w && w.percent !== null && w.percent !== undefined ? Math.round(w.percent) + "%" : "—"
        var reset = root.formatResetShort(root.resetRemainingMs(w ? w.resetsAt : null))
        parts.push((w ? w.label : "?") + " " + pct + (reset !== "" ? " (" + reset + ")" : ""))
      }
      return parts.join(" · ")
    }
    return p.label || "—"
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

  // DeepSeek peak/off-peak pricing (UTC): peak Mon-Fri 01:00-04:00 and
  // 06:00-10:00; off-peak the other 17 weekday hours, all of Sat/Sun and
  // every Chinese public holiday (full UTC days from Nager.Date, see
  // fetch_usage.py _load_cn_holidays). Pure UTC clock math — no timezone
  // offsets, DST-immune. Holidays only flip peak to off, never the reverse.
  function dsIsHolidayUTC(d) {
    var key = d.getUTCFullYear() + "-" + ("0" + (d.getUTCMonth() + 1)).slice(-2) + "-" + ("0" + d.getUTCDate()).slice(-2)
    return (root.cnHolidays || []).indexOf(key) !== -1
  }
  function dsPeakAt(d) {
    var day = d.getUTCDay()
    if (day === 0 || day === 6) return false
    if (root.dsIsHolidayUTC(d)) return false
    var m = d.getUTCHours() * 60 + d.getUTCMinutes()
    return (m >= 60 && m < 240) || (m >= 360 && m < 600)
  }
  function dsPeakNow() { return root.dsPeakAt(new Date(root.nowMs)) }
  // A UTC wall-clock time rendered in the PC's local zone (proves the
  // "in base alla time zone del PC" requirement in the panel).
  function dsLocalHHMM(h, m) {
    var n = new Date()
    return Qt.formatTime(new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate(), h, m)), "hh:mm")
  }
  readonly property string dsTooltipSuffix: root.dsPeakEnabled ? (root.dsPeakNow() ? " · DS peak" : " · DS off-peak") : ""
  // Pricing-clock colors: peak = theme red; off-peak green is derived from
  // that same red (the kit exposes no semantic green — Color has only
  // foreground/accent/urgent/muted), keeping saturation/value/alpha and
  // rotating the hue to green, so it follows every theme.
  readonly property color dsPeakColor: root.urgent
  readonly property color dsOffColor: Qt.hsva(0.33, root.urgent.hsvSaturation, root.urgent.hsvValue, root.urgent.a)
  // Pricing-clock master switch (off by default). Pure display + fetcher
  // flag; needs no API key. NOTE: the shell round-trips settings as STRINGS
  // (shell.json holds 'true'/'false'), so coerce instead of === true.
  readonly property bool dsPeakEnabled: {
    var v = root.setting("dsPeakClock", false);
    return v === true || v === "true" || v === 1;
  }
  // A toggle flip takes effect at the next fetch: re-fetch immediately so
  // the holidays payload (or its absence) matches the switch without
  // waiting for the 15-minute interval. If a fetch is already in flight
  // (e.g. the setting arrives after the startup fetch), defer to onExited.
  property bool dsRefetchPending: false
  onDsPeakEnabledChanged: {
    if (fetchProc.running) root.dsRefetchPending = true
    else root.refresh()
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
    command: ["python3", root.scriptPath(), "--env", root.hermesEnvFile].concat(root.dsPeakEnabled ? [] : ["--no-holidays"])
    stdout: StdioCollector {
      waitForEnd: true
      // streamFinished passes no argument: `text` is the collector's own
      // property. A handler parameter would shadow it with undefined.
      onStreamFinished: {
        var output = text || ""
        try {
          var data = JSON.parse(output)
          root.providers = data.providers || []
          root.cnHolidays = Array.isArray(data.cnHolidays) ? data.cnHolidays : []
          root.cnHolidaysYears = Array.isArray(data.cnHolidaysYears) ? data.cnHolidaysYears : []
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
      if (root.dsRefetchPending) {
        root.dsRefetchPending = false
        root.refresh()
      }
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
    tooltipText: "AI Subs" + root.dsTooltipSuffix
    active: root.errorText !== "" && root.errorText.indexOf("fetch-failed") === 0
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else root.toggle()
    }
  }

  // DeepSeek pricing clock: always visible, needs no API key. Green =
  // off-peak (cheap), red = peak. Overlays inside the slot, never resizes it.
  Rectangle {
    id: iconPeakDot
    anchors.top: button.top
    anchors.right: button.right
    width: Style.space(6)
    height: width
    radius: width / 2
    visible: !root.barShowsData && root.dsPeakEnabled
    color: root.dsPeakNow() ? root.dsPeakColor : root.dsOffColor
  }

  // Data mode: provider logo + compact one-liner for the default sub.
  // Composed by hand because WidgetButton renders text only and cannot host
  // an image; mirrors its tooltip, click-target registration and press
  // behavior against the bar.
  Item {
    id: dataButton
    anchors.fill: parent
    visible: root.barShowsData

    readonly property var sub: root.defaultSub
    readonly property bool hasWindows: !!sub && (sub.windows || []).length > 0
    readonly property string chipText: root.compactSubText(sub)
    readonly property bool failed: root.errorText !== "" && root.errorText.indexOf("fetch-failed") === 0
    readonly property color chipColor: failed ? root.urgent : root.foreground

    // The bar routes module clicks through this exact interface
    // (Bar.pressModuleClickTarget); without it the click is treated as
    // non-interactive and swallowed before our MouseArea sees it.
    function triggerPress(button) {
      if (root.bar) root.bar.hideTooltip(dataButton)
      if (button === Qt.RightButton) root.refresh()
      else root.toggle()
    }

    implicitWidth: (chipLogo.visible ? chipLogo.width + Style.space(6) : 0)
      + (dataButton.hasWindows ? chipWindowsRow.implicitWidth : chipLabel.implicitWidth)
      + (root.dsPeakEnabled ? chipDsLabel.implicitWidth + chipPeakDot.width + Style.space(9) : 0)
      + Style.space(17)
    implicitHeight: Math.max(chipLogo.height, dataButton.hasWindows ? chipWindowsRow.implicitHeight : chipLabel.implicitHeight)

    Image {
      id: chipLogo
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.font.body * 1.15
      height: width
      visible: !!dataButton.sub && dataButton.sub.logo !== ""
      source: visible ? Qt.resolvedUrl("assets/icons/" + dataButton.sub.logo + ".svg") : ""
      sourceSize.width: width * 2
      sourceSize.height: height * 2
      fillMode: Image.PreserveAspectFit
      mipmap: true
    }

    // Graphical progress bar view for windowed subscriptions (OpenCode, Command Code, etc.)
    Row {
      id: chipWindowsRow
      anchors.left: chipLogo.visible ? chipLogo.right : parent.left
      anchors.leftMargin: chipLogo.visible ? Style.space(6) : 0
      anchors.verticalCenter: parent.verticalCenter
      visible: dataButton.hasWindows
      spacing: Style.space(6)

      Repeater {
        model: dataButton.hasWindows ? dataButton.sub.windows : []

        Row {
          id: winItem
          required property var modelData
          required property int index
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(4)

          // Dot separator before subsequent windows
          Text {
            visible: winItem.index > 0
            anchors.verticalCenter: parent.verticalCenter
            text: "·"
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // Window label (5h, W, M)
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: winItem.modelData ? winItem.modelData.label : "?"
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // Percentage text
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: winItem.modelData && winItem.modelData.percent !== null && winItem.modelData.percent !== undefined
              ? Math.round(winItem.modelData.percent) + "%" : "—"
            textFormat: Text.PlainText
            color: dataButton.chipColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // Real graphical mini progress bar beside the %
          Item {
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(22)
            height: Style.space(4)
            visible: winItem.modelData && winItem.modelData.percent !== null && winItem.modelData.percent !== undefined

            readonly property real pct: parent.visible ? Number(winItem.modelData.percent) : 0
            readonly property real ratio: root.clamp(pct / 100.0, 0, 1)

            Rectangle {
              anchors.fill: parent
              radius: height / 2
              color: root.track
            }

            Rectangle {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              height: parent.height
              width: Math.round(parent.width * parent.ratio)
              radius: height / 2
              color: root.usageTone(parent.ratio)
            }
          }

          // Reset countdown hint: (13h)
          Text {
            readonly property real _tick: root.nowMs
            readonly property int remainingMs: _tick > 0 ? root.resetRemainingMs(winItem.modelData ? winItem.modelData.resetsAt : null) : -1
            readonly property string resetStr: root.formatResetShort(remainingMs)
            visible: resetStr !== ""
            anchors.verticalCenter: parent.verticalCenter
            text: "(" + resetStr + ")"
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }

    // Fallback single-label view for balance-only subs or errors
    Text {
      id: chipLabel
      anchors.left: chipLogo.visible ? chipLogo.right : parent.left
      anchors.leftMargin: chipLogo.visible ? Style.space(6) : 0
      anchors.verticalCenter: parent.verticalCenter
      visible: !dataButton.hasWindows
      text: dataButton.chipText
      textFormat: Text.PlainText
      color: dataButton.chipColor
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    // DeepSeek pricing clock: "DS" tag + dot, always visible when enabled
    // (needs no API key). Green = off-peak (cheap), red = peak.
    Text {
      id: chipDsLabel
      anchors.left: dataButton.hasWindows ? chipWindowsRow.right : chipLabel.right
      anchors.leftMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      visible: root.dsPeakEnabled
      text: "DS"
      textFormat: Text.PlainText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Rectangle {
      id: chipPeakDot
      anchors.left: chipDsLabel.right
      anchors.leftMargin: Style.space(4)
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(5)
      height: width
      radius: width / 2
      visible: root.dsPeakEnabled
      color: root.dsPeakNow() ? root.dsPeakColor : root.dsOffColor
    }

    MouseArea {
      id: dataMouse
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton | Qt.RightButton
      hoverEnabled: true
      onClicked: function (mouse) { dataButton.triggerPress(mouse.button) }
      onContainsMouseChanged: {
        if (!root.bar) return
        if (containsMouse) root.bar.showTooltip(dataButton, "AI Subs" + root.dsTooltipSuffix)
        else root.bar.hideTooltip(dataButton)
      }
      Component.onCompleted: if (root.bar && root.bar.registerClickTarget) root.bar.registerClickTarget(dataButton)
      Component.onDestruction: if (root.bar && root.bar.unregisterClickTarget) root.bar.unregisterClickTarget(dataButton)
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
    color: chip.selected ? root.foreground : (chipMouse.containsMouse ? root.alpha(root.foreground, 0.14) : root.alpha(root.foreground, 0.08))
    Behavior on color {
      ColorAnimation { duration: 140 }
    }
    scale: chipMouse.pressed ? 0.94 : 1
    Behavior on scale {
      NumberAnimation { duration: 120; easing.type: Easing.OutCubic }
    }

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
      id: chipMouse
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

    // Gentle rise-and-fade each time the model is (re)built, so a refresh
    // reads as new data arriving rather than a hard swap.
    opacity: block.entered ? 1 : 0
    property bool entered: false
    Component.onCompleted: Qt.callLater(function () { block.entered = true })
    Behavior on opacity {
      NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
    }
    transform: Translate {
      y: block.entered ? 0 : Style.space(8)
      Behavior on y {
        NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
      }
    }

    readonly property var p: block.modelData || {}
    readonly property bool hasWindows: (block.p.windows || []).length > 0

    Rectangle {
      width: parent.width
      implicitHeight: Math.max(badge.implicitHeight, valueText.implicitHeight) + Style.space(12)
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.07)

      Item {
        id: badge
        anchors.left: parent.left
        anchors.leftMargin: Style.space(10)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(26)
        height: Style.space(26)

        readonly property string logoSlug: block.p.logo || ""
        readonly property bool hasLogo: logoSlug !== ""

        // Letter fallback for providers without a bundled mark.
        Text {
          anchors.fill: parent
          visible: !badge.hasLogo || providerMark.status !== Image.Ready
          text: block.p.display
          textFormat: Text.PlainText
          color: Color.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
          horizontalAlignment: Text.AlignHCenter
          verticalAlignment: Text.AlignVCenter
        }

        Image {
          id: providerMark
          anchors.fill: parent
          visible: badge.hasLogo
          source: badge.hasLogo ? Qt.resolvedUrl("assets/icons/" + badge.logoSlug + ".svg") : ""
          sourceSize.width: width * 2
          sourceSize.height: height * 2
          fillMode: Image.PreserveAspectFit
          mipmap: true
        }


        // The bundled SVGs carry each provider's brand color, so the image
        // renders untinted.
      }

      Text {
        anchors.left: badge.right
        anchors.leftMargin: Style.space(8)
        anchors.rightMargin: Style.space(10)
        anchors.right: valueText.visible ? valueText.left : parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: block.p.name
        textFormat: Text.PlainText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
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

          // Hero header — mirrors the built-in panels ("Audio", "Network"):
          // display-size glyph, then title-case name over a small tracked-out
          // status line, settings toggle on the trailing edge.
          Item {
            width: parent.width
            implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

            Text {
              id: heroIcon
              text: "󰚩"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroLabels
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(14)
              anchors.right: settingsButton.left
              anchors.rightMargin: Style.space(10)
              spacing: Style.space(2)

              Text {
                width: parent.width
                text: "AI Subs"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: root.heroCaption.toUpperCase()
                textFormat: Text.PlainText
                color: Qt.darker(root.foreground, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
                elide: Text.ElideRight
              }
            }

            ModeChip {
              id: settingsButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              label: "\uF013"
              selected: root.settingsOpen
              onPicked: root.settingsOpen = !root.settingsOpen
            }
          }

          PanelSeparator { foreground: root.foreground }

          // Settings drawer: collapsed until the gear is pressed. Height and
          // fade animate together so the rows below slide up smoothly.
          Item {
            width: parent.width
            height: root.settingsOpen ? settingsInner.implicitHeight : 0
            clip: true
            enabled: root.settingsOpen
            Behavior on height {
              NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
            }
            opacity: root.settingsOpen ? 1 : 0
            Behavior on opacity {
              NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
            }

            Column {
              id: settingsInner
              anchors.top: parent.top
              width: parent.width
              spacing: Style.space(8)

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

              // Pricing-clock master switch (off by default, needs no key).
              Row {
                width: parent.width
                spacing: Style.space(8)

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(84)
                  text: "Clock Deepseek"
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                ModeChip {
                  label: "Off"
                  selected: !root.dsPeakEnabled
                  onPicked: root.persistSetting("dsPeakClock", false)
                }

                ModeChip {
                  label: "On"
                  selected: root.dsPeakEnabled
                  onPicked: root.persistSetting("dsPeakClock", true)
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
            }
          }

          // DeepSeek pricing clock legend: both colors explained, live state
          // in the title. ASCII-only strings on purpose.
          Column {
            id: dsClockRow
            visible: root.dsPeakEnabled
            width: parent.width
            spacing: Style.space(4)

            readonly property bool peak: root.dsPeakNow()
            readonly property string localRanges: root.dsLocalHHMM(1, 0) + "-" + root.dsLocalHHMM(4, 0)
              + ", " + root.dsLocalHHMM(6, 0) + "-" + root.dsLocalHHMM(10, 0) + " local"

            Text {
              width: parent.width
              text: "Deepseek peak/off times - now " + (dsClockRow.peak ? "PEAK" : "OFF-PEAK")
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              wrapMode: Text.WordWrap
            }

            Row {
              width: parent.width
              spacing: Style.space(6)

              Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(7)
                height: width
                radius: width / 2
                color: root.dsPeakColor
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - Style.space(7) - parent.spacing
                text: "peak  Mon-Fri 01:00-04:00, 06:00-10:00 UTC (" + dsClockRow.localRanges + ")"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            Row {
              width: parent.width
              spacing: Style.space(6)

              Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(7)
                height: width
                radius: width / 2
                color: root.dsOffColor
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - Style.space(7) - parent.spacing
                text: "off-peak  all other times, weekends, CN holidays"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
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
