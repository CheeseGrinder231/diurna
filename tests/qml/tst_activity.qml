import QtQuick
import QtTest
import Quickshell.Services.Mpris
import TestSupport

TestCase {
    name: "ActivityProducer"
    property var plugin: null
    readonly property string program: "/synthetic/diurna"
    Component { id: activity; Service {} }
    Component { id: player; Player {} }

    function init() {
        Fixture.commands = []
        Fixture.initialIdle = false
        Fixture.initialNoInput = false
        Mpris.players.values = []
    }
    function cleanup() {
        if (plugin) {
            plugin.ready = false
            // Break the fakes' initial-state bindings before deferred deletion,
            // so a later test cannot change this instance's monitor state.
            if (Fixture.monitor) Fixture.monitor.isIdle = false
            if (Fixture.input) Fixture.input.isIdle = false
            plugin.destroy()
        }
        plugin = null
        Fixture.monitor = null
        Fixture.input = null
        Fixture.process = null
        Mpris.players.values = []
    }
    function start(idle, noInput) {
        Fixture.initialIdle = idle
        Fixture.initialNoInput = !!noInput
        plugin = activity.createObject(null)
        verify(plugin !== null)
    }
    function acknowledge() {
        Fixture.process.finish()
        // Production schedules drain with callLater. Invoke it directly here
        // so this test needs no event-loop wait or wall-clock deadline.
        plugin.drain()
    }
    function settle() {
        while (plugin.queue.length) acknowledge()
    }
    function sent() {
        return Fixture.commands.map(command => {
            compare(command.slice(0, 2), [program, "signal"])
            return command.slice(2)
        })
    }
    function test_monitors() {
        start(false)
        // One respects inhibitors (idle, like omarchy.idle); one sees input only.
        compare([Fixture.monitor.respectInhibitors, Fixture.monitor.timeout], [true, 150])
        compare([Fixture.input.respectInhibitors, Fixture.input.timeout], [false, 60])
    }
    function test_initial_state_data() {
        return [{tag: "active", idle: false, noInput: false, events: [["active"], ["input"], ["media"]]},
                {tag: "idle", idle: true, noInput: true, events: [["idle"], ["noinput"], ["media"]]}]
    }
    function test_initial_state(data) {
        start(data.idle, data.noInput)
        compare(sent(), data.events.slice(0, 1))
        settle()
        compare(sent(), data.events)
    }
    function test_idle_active_commands_are_serialized() {
        start(false)
        Fixture.monitor.isIdle = true
        Fixture.monitor.isIdle = false
        // Changes queue while the initial command awaits acknowledgement.
        compare(sent(), [["active"]])
        settle()
        compare(sent(), [["active"], ["input"], ["media"], ["idle"], ["active"]])
        compare(plugin.queue, [])
    }
    function test_input_changes_are_sent() {
        start(false)
        settle()
        Fixture.input.isIdle = true
        settle()
        Fixture.input.isIdle = false
        settle()
        compare(sent().slice(3), [["noinput"], ["input"]])
    }
    function test_media_sends_player_names_and_playback_only() {
        const chromium = player.createObject(null, {desktopEntry: "chromium",
            dbusName: "org.mpris.MediaPlayer2.chromium.instance42"})
        const spotify = player.createObject(null, {dbusName: "org.mpris.MediaPlayer2.spotify",
                                                   isPlaying: true})
        Mpris.players.values = [chromium, spotify]
        start(false)
        settle()
        compare(sent()[2], ["media", "chromium:paused", "org.mpris.MediaPlayer2.chromium.instance42:paused",
                            "org.mpris.MediaPlayer2.spotify:playing"])
        chromium.isPlaying = true
        settle()
        compare(sent()[3], ["media", "chromium:playing", "org.mpris.MediaPlayer2.chromium.instance42:playing",
                            "org.mpris.MediaPlayer2.spotify:playing"])
        Mpris.players.values = []
        settle()
        compare(sent()[4], ["media"])
        verify(JSON.stringify(Fixture.commands).indexOf("PRIVATE") < 0)
        chromium.destroy()
        spotify.destroy()
    }
    function test_waiting_media_state_is_replaced_by_the_newer_one() {
        const mpv = player.createObject(null, {dbusName: "org.mpris.MediaPlayer2.mpv"})
        start(false)
        settle()
        Fixture.input.isIdle = true            // in flight
        Mpris.players.values = [mpv]           // waiting
        mpv.isPlaying = true                   // replaces it
        settle()
        compare(sent().slice(3), [["noinput"], ["media", "org.mpris.MediaPlayer2.mpv:playing"]])
        mpv.destroy()
    }
}
