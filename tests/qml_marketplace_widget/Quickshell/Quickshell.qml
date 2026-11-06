pragma Singleton
import QtQml
import TestSupport

QtObject {
    function execDetached(command) {
        Fixture.detachedCommands = Fixture.detachedCommands.concat([command.slice()])
    }
}
