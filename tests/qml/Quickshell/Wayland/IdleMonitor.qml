import QtQml
import TestSupport

QtObject {
    property bool isIdle: Fixture.initialIdle
    property int timeout: 0
    property bool respectInhibitors: false
    Component.onCompleted: Fixture.monitor = this
}
