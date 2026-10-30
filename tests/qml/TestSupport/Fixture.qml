pragma Singleton
import QtQml

QtObject {
    property bool initialIdle: false
    property bool initialNoInput: false
    property var monitor: null
    property var input: null
    property var process: null
    property var commands: []
}
