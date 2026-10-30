import QtQml
import TestSupport

QtObject {
    // Quickshell's default.
    property bool respectInhibitors: true
    property bool isIdle: respectInhibitors ? Fixture.initialIdle : Fixture.initialNoInput
    property int timeout: 0
    Component.onCompleted: {
        if (respectInhibitors) Fixture.monitor = this
        else Fixture.input = this
    }
}
