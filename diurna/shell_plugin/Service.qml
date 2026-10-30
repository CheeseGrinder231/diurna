import QtQuick
import Quickshell.Io
import Quickshell.Services.Mpris
import Quickshell.Wayland

// Omarchy 4.0.6 does not expose its private idle/lock service objects to plugins.
// Use the same native ext-idle-notify monitor as omarchy.idle. Never create a
// session lock or walk private shell objects to observe authentication state.
Item {
    id: root
    property var queue: []
    property bool ready: false
    // Each MPRIS player's names and whether it is Playing: never titles or
    // track metadata. The collector keeps only whether the focused app plays.
    readonly property string media: {
        const players = Mpris.players ? Mpris.players.values : []
        const tokens = []
        for (let i = 0; i < players.length; i++) {
            const player = players[i]
            if (!player) continue
            const state = player.isPlaying ? "playing" : "paused"
            for (const name of [player.desktopEntry, player.dbusName]) {
                const clean = String(name || "").replace(/[^A-Za-z0-9._-]/g, "").slice(0, 128)
                if (clean) tokens.push(clean + ":" + state)
            }
        }
        return tokens.sort().filter((token, i, all) => token !== all[i - 1]).slice(0, 64).join(" ")
    }

    function mediaEvent() {
        return ["media"].concat(media ? media.split(" ") : [])
    }
    function enqueue(event) {
        // A newer player state replaces one still waiting. Keep the one in flight.
        const busy = sender.running || retry.running
        if (event[0] === "media")
            queue = queue.filter((queued, i) => queued[0] !== "media" || (i === 0 && busy))
        queue = queue.concat([event])
        drain()
    }
    function drain() {
        if (!ready || sender.running || retry.running || queue.length === 0) return
        sender.command = [@PROGRAM@, "signal"].concat(queue[0])
        sender.running = true
    }

    onMediaChanged: if (ready) enqueue(mediaEvent())

    IdleMonitor {
        id: monitor
        timeout: @TIMEOUT@
        respectInhibitors: true
        onIsIdleChanged: if (root.ready) root.enqueue([isIdle ? "idle" : "active"])
    }
    // Real keyboard or pointer input only: an inhibitor (a video, the
    // screensaver) never makes this one active.
    IdleMonitor {
        id: input
        timeout: @HANDS_ON@
        respectInhibitors: false
        onIsIdleChanged: if (root.ready) root.enqueue([isIdle ? "noinput" : "input"])
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
        enqueue([monitor.isIdle ? "idle" : "active"])
        enqueue([input.isIdle ? "noinput" : "input"])
        enqueue(mediaEvent())
    }
}
