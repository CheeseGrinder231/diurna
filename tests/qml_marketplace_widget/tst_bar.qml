import QtQuick
import QtTest
import TestSupport
import "plugin" as Plugin

TestCase {
    name: "MarketplaceBar"
    property var plugin: null
    Component { id: widget; Plugin.BarWidget {} }
    QtObject { id: host; property bool vertical: false; property int barSize: 32 }

    function init() {
        Fixture.commands = []
        Fixture.detachedCommands = []
        host.vertical = false
        plugin = widget.createObject(null, {bar: host})
        verify(plugin !== null)
    }
    function cleanup() {
        findChild(plugin, "summaryRefresh").running = false
        plugin.destroy()
        plugin = null
        Fixture.process = null
        Fixture.button = null
    }
    function test_initial_fallback() {
        compare(plugin.outputText, "Diurna")
        compare(plugin.outputTooltip, "Diurna: screen time unavailable")
        compare(Fixture.button.dimmed, true)
        compare(plugin.moduleName, "diurna.activity")
    }
    function test_refresh_runs_source_once_per_request() {
        plugin.refresh()
        compare(Fixture.commands, [["python3", "-B", plugin.runner, "bar"]])
        plugin.refresh()
        compare(Fixture.commands.length, 1)
        Fixture.process.finish('{"text":"1h 20m","tooltip":"Synthetic summary"}', 0, 0)
        plugin.refresh()
        compare(Fixture.commands.length, 2)
        verify(plugin.runner.indexOf("space's $dollar;name") !== -1)
    }
    function test_timer_refreshes_every_minute() {
        const timer = findChild(plugin, "summaryRefresh")
        compare(timer.interval, 60000)
        compare(timer.running, true)
        compare(timer.repeat, true)
        compare(timer.triggeredOnStart, true)
        timer.triggered()
        compare(Fixture.commands, [["python3", "-B", plugin.runner, "bar"]])
    }
    function test_summary_updates_label_and_tooltip() {
        plugin.refresh()
        Fixture.process.finish(JSON.stringify({text: "2h 15m", tooltip: "Today 2h 15m active\nSynthetic app 2h 15m", alt: "tracking"}), 0, 0)
        compare(Fixture.button.labelText, "2h 15m")
        compare(Fixture.button.tooltipText, "Today 2h 15m active\nSynthetic app 2h 15m")
        compare(Fixture.button.dimmed, false)
        verify(plugin.implicitWidth > Fixture.button.canvasSize)
        host.vertical = true
        compare(Fixture.button.labelText, "")
        compare(Fixture.button.tooltipText, plugin.outputTooltip)
        compare(plugin.implicitWidth, host.barSize)
    }
    function test_unavailable_report_is_preserved() {
        plugin.update('{"text":"not recording","tooltip":"Synthetic unavailable","class":["diurna","unavailable"]}')
        compare(plugin.outputText, "not recording")
        compare(plugin.outputTooltip, "Synthetic unavailable")
        compare(plugin.unavailable, true)
    }
    function test_recovery_guidance_names_the_plugin() {
        plugin.update('{"text":"not recording","tooltip":"Diurna: screen time unavailable; check diurna.service","class":["diurna","unavailable"]}')
        compare(plugin.outputTooltip, "Diurna: screen time unavailable; check the Diurna plugin")
        plugin.update('{"text":"1h not recording","tooltip":"Diurna: not recording; check diurna.service or the Hyprland session"}')
        compare(plugin.outputTooltip, "Diurna: not recording; check the Diurna plugin or the Hyprland session")
    }
    function test_malformed_output_data() {
        return [
            {tag: "empty", raw: ""},
            {tag: "syntax", raw: "not JSON"},
            {tag: "null", raw: "null"},
            {tag: "array", raw: "[]"},
            {tag: "missing text", raw: "{}"},
            {tag: "blank text", raw: '{"text":" "}'},
            {tag: "wrong text type", raw: '{"text":42}'}
        ]
    }
    function test_malformed_output(data) {
        plugin.update('{"text":"1h","tooltip":"Earlier synthetic summary"}')
        plugin.update(data.raw)
        compare(plugin.outputText, "Diurna")
        compare(plugin.outputTooltip, "Diurna: screen time unavailable")
        compare(plugin.unavailable, true)
    }
    function test_command_failure_data() {
        return [{tag: "exit error", code: 1, status: 0},
                {tag: "crash", code: 0, status: 1}]
    }
    function test_command_failure(data) {
        plugin.refresh()
        Fixture.process.finish('{"text":"1h","tooltip":"Incomplete summary"}', data.code, data.status)
        compare(plugin.outputText, "Diurna")
        compare(plugin.unavailable, true)
    }
    function test_open_today_quotes_launcher_path() {
        Fixture.button.pressed(Qt.LeftButton)
        compare(Fixture.detachedCommands, [["omarchy-launch-or-focus-tui", "--app-id=TUI.float",
            "python3", "-B", plugin.shellQuote(plugin.runner), "today", "--watch"]])
        compare(plugin.shellQuote("space's $dollar;name"), "'space'\\''s $dollar;name'")
        // The Python harness checks the exact argv recovered after the upstream
        // launcher joins its arguments. This never invokes a shell or desktop.
        console.log("DIURNA_LAUNCH:" + JSON.stringify({argv: Fixture.detachedCommands[0], runner: plugin.runner}))
    }
}
