import QtQuick
import Quickshell.Io

Item {
    id: root
    readonly property string runner: decodeURIComponent(Qt.resolvedUrl("../plugin.py").toString().replace(/^file:\/\//, ""))
    readonly property var command: ["python3", "-B", runner]
    property bool stopping: false
    property bool configured: false
    property int idleSeconds: 150
    property int handsOnSeconds: 60

    function configure(text) {
        try {
            const value = JSON.parse(text)
            if (Number.isInteger(value.idleSeconds) && value.idleSeconds >= 1 && value.idleSeconds <= 86400)
                idleSeconds = value.idleSeconds
            if (Number.isInteger(value.handsOnSeconds) && value.handsOnSeconds >= 1 && value.handsOnSeconds <= 3600)
                handsOnSeconds = value.handsOnSeconds
        } catch (error) {
            // A missing/malformed config retains the same safe default monitors.
        }
        configured = true
    }

    function stop() {
        stopping = true
        restart.stop()
        collector.running = false
        configReader.running = false
    }

    Process {
        id: configReader
        objectName: "diurna-settings"
        command: root.command.concat(["plugin-settings"])
        stdout: StdioCollector { onStreamFinished: root.configure(text) }
        onExited: if (!root.configured) root.configure("")
    }
    Loader {
        active: root.configured && !root.stopping
        sourceComponent: Component {
            Activity {
                command: root.command
                idleSeconds: root.idleSeconds
                handsOnSeconds: root.handsOnSeconds
            }
        }
    }
    Process {
        id: collector
        objectName: "diurna-collector"
        // Never detached. Quickshell owns this child and kills it on unload.
        // The database lock lets an existing manual collector retain ownership.
        command: root.command.concat(["collect", "--wait-for-lock"])
        onExited: if (!root.stopping) restart.start()
    }
    Timer {
        id: restart
        objectName: "diurna-restart"
        interval: 5000
        onTriggered: if (!root.stopping) collector.running = true
    }
    Component.onCompleted: {
        configReader.running = true
        collector.running = true
    }
    Component.onDestruction: stop()
}
