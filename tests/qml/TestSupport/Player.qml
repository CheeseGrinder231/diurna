import QtQml

QtObject {
    property string desktopEntry: ""
    property string dbusName: ""
    property bool isPlaying: false
    // Present on real players; the plugin must never send it.
    property string trackTitle: "PRIVATE TRACK"
    property string identity: "PRIVATE IDENTITY"
}
