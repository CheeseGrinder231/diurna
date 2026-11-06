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
    function finish(output, code, status) {
        stdout.text = output
        stdout.streamFinished()
        running = false
        exited(code, status)
    }
    Component.onCompleted: Fixture.process = this
}
