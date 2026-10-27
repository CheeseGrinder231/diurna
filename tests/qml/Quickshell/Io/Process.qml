import QtQml
import TestSupport

QtObject {
    property var command: []
    property bool running: false
    signal exited(int code, int status)
    onRunningChanged: {
        if (running) Fixture.commands = Fixture.commands.concat([command.slice()])
    }
    function finish() {
        running = false
        exited(0, 0)
    }
    Component.onCompleted: Fixture.process = this
}
