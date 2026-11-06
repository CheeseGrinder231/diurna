import QtQuick
import QtTest
import TestSupport

TestCase {
    name: "MarketplaceService"
    property var plugin: null
    Component { id: service; Service {} }

    function init() {
        Fixture.commands = []
        Fixture.initialIdle = false
        Fixture.initialNoInput = false
        plugin = service.createObject(null)
        verify(plugin !== null)
    }
    function cleanup() {
        plugin.stop()
        plugin.destroy()
        plugin = null
        Fixture.monitor = null
        Fixture.input = null
    }
    function child(name) { return findChild(plugin, "diurna-" + name) }
    function configured(idle, handsOn) {
        const reader = child("settings")
        reader.stdout.text = JSON.stringify({idleSeconds: idle, handsOnSeconds: handsOn})
        reader.stdout.streamFinished()
        reader.finish()
    }
    function test_starts_source_once_and_waits_for_config_before_signals() {
        compare(Fixture.commands.length, 2)
        compare(Fixture.commands[0], ["python3", "-B", plugin.runner, "plugin-settings"])
        compare(Fixture.commands[1], ["python3", "-B", plugin.runner, "collect", "--wait-for-lock"])
        verify(plugin.runner.startsWith("/"))
        verify(plugin.runner.indexOf("%20") === -1)
        verify(plugin.runner.endsWith("/plugin.py"))
        configured(90, 45)
        compare(Fixture.monitor.timeout, 90)
        compare(Fixture.input.timeout, 45)
        compare(Fixture.commands[2], ["python3", "-B", plugin.runner, "signal", "active"])
    }
    function test_bad_config_keeps_bounded_defaults() {
        const reader = child("settings")
        reader.stdout.text = '{"idleSeconds":-2,"handsOnSeconds":99999}'
        reader.stdout.streamFinished()
        compare(Fixture.monitor.timeout, 150)
        compare(Fixture.input.timeout, 60)
    }
    function test_failed_config_still_initializes_monitors() {
        child("settings").finish(1)
        compare(plugin.configured, true)
        compare(Fixture.monitor.timeout, 150)
        compare(Fixture.input.timeout, 60)
    }
    function test_failed_collector_retries_with_one_owned_process() {
        const collector = child("collector")
        const restart = child("restart")
        collector.finish(1)
        verify(restart.running)
        compare(restart.interval, 5000)
        restart.stop()
        restart.triggered()
        verify(collector.running)
        compare(Fixture.commands.filter(command => command.indexOf("collect") !== -1).length, 2)
    }
    function test_disable_stops_owned_children_and_prevents_restart() {
        child("collector").finish(1)
        plugin.stop()
        verify(plugin.stopping)
        verify(!child("collector").running)
        verify(!child("settings").running)
        verify(!child("restart").running)
        child("collector").exited(1, 0)
        child("restart").triggered()
        verify(!child("collector").running)
        verify(!child("restart").running)
    }
    function test_reenable_creates_fresh_owned_children() {
        plugin.stop()
        plugin.destroy()
        Fixture.commands = []
        plugin = service.createObject(null)
        verify(child("collector").running)
        compare(Fixture.commands.length, 2)
        verify(!plugin.stopping)
    }
}
