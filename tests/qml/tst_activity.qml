import QtQuick
import QtTest
import TestSupport

TestCase {
    name: "ActivityProducer"
    property var plugin: null
    Component { id: activity; Service {} }

    function init() {
        Fixture.commands = []
        Fixture.initialIdle = false
    }
    function cleanup() {
        if (plugin) {
            plugin.ready = false
            // Break the fake's initial-state binding before deferred deletion,
            // so a later test cannot change this instance's monitor state.
            if (Fixture.monitor) Fixture.monitor.isIdle = false
            plugin.destroy()
        }
        plugin = null
        Fixture.monitor = null
        Fixture.process = null
    }
    function start(idle) {
        Fixture.initialIdle = idle
        plugin = activity.createObject(null)
        verify(plugin !== null)
    }
    function acknowledge() {
        Fixture.process.finish()
        // Production schedules drain with callLater. Invoke it directly here
        // so this test needs no event-loop wait or wall-clock deadline.
        plugin.drain()
    }
    function test_initial_state_data() {
        return [{tag: "active", idle: false, event: "active"},
                {tag: "idle", idle: true, event: "idle"}]
    }
    function test_initial_state(data) {
        start(data.idle)
        compare(Fixture.commands, [["/synthetic/diurna", "signal", data.event]])
    }
    function test_idle_active_commands_are_serialized() {
        start(false)
        Fixture.monitor.isIdle = true
        Fixture.monitor.isIdle = false
        // Changes queue while the initial command awaits acknowledgement.
        compare(Fixture.commands, [["/synthetic/diurna", "signal", "active"]])
        acknowledge()
        compare(Fixture.commands, [["/synthetic/diurna", "signal", "active"],
                                 ["/synthetic/diurna", "signal", "idle"]])
        acknowledge()
        compare(Fixture.commands, [["/synthetic/diurna", "signal", "active"],
                                 ["/synthetic/diurna", "signal", "idle"],
                                 ["/synthetic/diurna", "signal", "active"]])
        acknowledge()
        compare(plugin.queue, [])
    }
}
