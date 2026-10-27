pragma Singleton
import QtQml

QtObject {
    property bool initialIdle: false
    property var monitor: null
    property var process: null
    property var commands: []
}
