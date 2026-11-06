import QtQml
import TestSupport
QtObject {
    property var command: []
    property bool running: false
    property QtObject stdout: null
    signal exited(int code, int status)
    onRunningChanged: {
        if (running) Fixture.commands = Fixture.commands.concat([command.slice()])
    }
    function finish(code) {
        running = false
        exited(code || 0, 0)
    }
    Component.onCompleted: Fixture.process = this
}
