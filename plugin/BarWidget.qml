import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
    id: root
    moduleName: "diurna.activity"

    readonly property string runner: decodeURIComponent(
        Qt.resolvedUrl("../plugin.py").toString().replace(/^file:\/\//, ""))
    property string outputText: "Diurna"
    property string outputTooltip: "Diurna: screen time unavailable"
    property bool unavailable: true

    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    function showUnavailable() {
        outputText = "Diurna"
        outputTooltip = "Diurna: screen time unavailable"
        unavailable = true
    }

    function update(raw) {
        try {
            const data = JSON.parse(raw)
            if (!data || typeof data !== "object" || Array.isArray(data)
                    || typeof data.text !== "string" || !data.text.trim()) {
                showUnavailable()
                return
            }
            outputText = data.text
            outputTooltip = typeof data.tooltip === "string" && data.tooltip
                ? data.tooltip : "Diurna: " + outputText
            // The shared CLI also serves manual systemd installs. This widget
            // owns a shell child, so its recovery guidance must name the plugin.
            outputTooltip = outputTooltip.replace("check diurna.service", "check the Diurna plugin")
            unavailable = data.alt === "unavailable"
                || data.class === "unavailable"
                || (Array.isArray(data.class) && data.class.indexOf("unavailable") !== -1)
        } catch (error) {
            showUnavailable()
        }
    }

    function refresh() {
        if (!summary.running) summary.running = true
    }

    function shellQuote(value) {
        // Omarchy's launch-or-focus helper evaluates one launch command.
        return "'" + String(value).replace(/'/g, "'\\''") + "'"
    }

    function openToday() {
        // TUI.float is an exact match in Omarchy's floating window rule. Keep
        // that class for placement, but identify our window by its own title.
        Quickshell.execDetached([
            "omarchy-launch-or-focus", "^Diurna Screen Time$",
            "uwsm-app -- xdg-terminal-exec --app-id=TUI.float "
                + "--title='Diurna Screen Time' -e python3 -B "
                + shellQuote(runner) + " today --watch"
        ])
    }

    Process {
        id: summary
        command: ["python3", "-B", root.runner, "bar"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: root.update(text)
        }
        onExited: function(code, status) {
            if (code !== 0 || status !== 0) root.showUnavailable()
        }
    }

    Timer {
        objectName: "summaryRefresh"
        interval: 60000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    FontMetrics {
        id: labelMetrics
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
    }

    BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: "\uf017"
        labelText: root.vertical ? "" : root.outputText
        fontSize: Style.font.body
        slotSize: root.vertical ? root.barSize
            : Math.ceil(labelMetrics.advanceWidth(labelText)) + canvasSize + Style.space(20)
        tooltipText: root.outputTooltip
        dimmed: root.unavailable
        onPressed: root.openToday()
    }
}
