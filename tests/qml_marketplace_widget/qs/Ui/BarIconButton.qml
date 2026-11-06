import QtQuick
import TestSupport

Item {
    property var bar: null
    property string text: ""
    property string labelText: ""
    property string fontFamily: "sans-serif"
    property real fontSize: 12
    property real slotSize: 32
    property real canvasSize: 20
    property string tooltipText: ""
    property bool dimmed: false
    implicitWidth: slotSize
    implicitHeight: 32
    signal pressed(int button)
    Component.onCompleted: Fixture.button = this
}
