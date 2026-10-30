import QtQuick
import Quickshell.Io
import Quickshell.Wayland

// Omarchy 4.0.6 does not expose its private idle/lock service objects to plugins.
// Use the same native ext-idle-notify monitor as omarchy.idle. Never create a
// session lock or walk private shell objects to observe authentication state.
Item {
    id: root
    property var queue: []
    property bool ready: false

    function enqueue(event) {
        queue = queue.concat([event])
        drain()
    }
    function drain() {
        if (!ready || sender.running || retry.running || queue.length === 0) return
        sender.command = [@PROGRAM@, "signal", queue[0]]
        sender.running = true
    }

    IdleMonitor {
        id: monitor
        timeout: @TIMEOUT@
        respectInhibitors: true
        onIsIdleChanged: if (root.ready) root.enqueue(isIdle ? "idle" : "active")
    }
    Process {
        id: sender
        onExited: function(code, status) {
            if (code === 0 && status === 0) {
                root.queue = root.queue.slice(1)
                Qt.callLater(root.drain)
            } else {
                // State was persisted even if collection is restarting. Keep
                // event order; retry is a net for a missed acknowledgement.
                retry.start()
            }
        }
    }
    Timer {
        id: retry
        interval: 60000
        onTriggered: root.drain()
    }
    Component.onCompleted: {
        ready = true
        enqueue(monitor.isIdle ? "idle" : "active")
    }
}
