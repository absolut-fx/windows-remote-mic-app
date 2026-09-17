// Top-level Windows settings window (XRBM-030 In-scope item 2). It uses a
// native Qt Quick window and Windows' own title-bar/Fluent chrome.
//
// `SettingsController`/`ButtonMappingModel` below are QML SINGLETON types
// (registered via qmlRegisterSingletonInstance in
// qt_settings_app.run_settings_window(), imported from the
// "OvbRc003Settings" module) - deliberately not exposed as
// engine.rootContext() context properties. During this task, an
// isolated repro proved that a context property can read back as null the
// first time it is accessed from a binding evaluated during a nested
// component's own construction (e.g. inside a ScrollView's deferred content,
// or a ListView's currentIndex binding, before an externally-supplied
// property has finished propagating down to it) - a QML singleton has no
// such hazard, since every file that imports the module gets the same
// already-fully-constructed instance immediately, resolved once by the
// type system rather than walked through a context hierarchy each time.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

ApplicationWindow {
    id: window
    title: qsTr("Remote Mic Settings")
    width: 900
    height: 680
    minimumWidth: 780
    minimumHeight: 600
    visible: true

    property Tokens tokens: Tokens {}

    color: tokens.background

    // XRBM-030 RETRY 1 blocker 2: Qt Quick Controls' "FluentWinUI3" style
    // resolves its OWN default text/background colors from
    // Qt.styleHints.colorScheme independently of Tokens.qml's colors (which
    // come from the plain QtQuick `SystemPalette` type instead) - under the
    // offscreen QPA platform (no real desktop/compositor), colorScheme
    // reports `Unknown`, and FluentWinUI3 falls back to a DARK-styled
    // control appearance (white button/tab/field text) while SystemPalette
    // separately falls back to a LIGHT one (white base, black text) - two
    // independent "default" guesses that disagree, producing white text on
    // a light background. Explicitly setting this window's own `palette`
    // (which Qt Quick Controls propagates down to every descendant Button/
    // TabButton/ComboBox/TextField automatically, verified by a minimal
    // isolated repro during this task) from the SAME SystemPalette-derived
    // tokens used for the rest of this window keeps both systems reading
    // from one source of truth in both light AND dark real Windows
    // sessions - this does not hard-code a light-only palette, since every
    // value below is itself OS-derived via Tokens.qml.
    palette.window: tokens.background
    palette.windowText: tokens.textPrimary
    palette.button: tokens.buttonBackground
    palette.buttonText: tokens.buttonText
    palette.base: tokens.fieldBackground
    palette.text: tokens.textPrimary
    palette.highlight: tokens.accent
    palette.highlightedText: tokens.accentText

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        TabBar {
            id: tabBar
            objectName: "tabBar"  // lets tooling (e.g. a screenshot script) drive tab switching via QObject.findChild
            Layout.fillWidth: true

            TabButton {
                objectName: "generalTabButton"
                text: qsTr("General")
                Accessible.name: text
            }
            TabButton {
                objectName: "connectionTabButton"  // test hook: for the rendered contrast regression test
                text: qsTr("Connection")
                Accessible.name: text
                // Pre-selected so the connection tab is the initial landing page;
                // preserves the historical "open Settings → land on Connection" UX
                // even though General is now the leftmost tab.
                Component.onCompleted: checked = true
            }
            TabButton {
                objectName: "mappingTabButton"
                text: SettingsController.mappingPageTitle
                Accessible.name: text
            }
            TabButton {
                text: qsTr("Permissions")
                Accessible.name: text
            }
            TabButton {
                objectName: "diagnosticsTabButton"  // test hook: for driving this tab in the offscreen screenshot/interaction tests
                text: qsTr("Diagnostics")
                Accessible.name: text
            }
        }

        StackLayout {
            id: pageStack
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: tabBar.currentIndex

            GeneralPage { tokens: window.tokens }
            ConnectionPage { tokens: window.tokens }
            ButtonsPage { tokens: window.tokens }
            PermissionsPage { tokens: window.tokens }
            DiagnosticsPage { tokens: window.tokens }
        }

        // Footer band: a single thin row at the bottom of the settings
        // window with a small version label so the user can confirm at a
        // glance which build of RemoteMicRC003 is currently running. Reads
        // ``SettingsController.version`` (a ``constant=True`` property bound
        // to ``ovb_rc003.__version__`` in qt_settings_app.py), so QML never
        // needs to know the literal version string.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 22
            color: tokens.border

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: tokens.spacingMedium
                anchors.rightMargin: tokens.spacingMedium

                Item { Layout.fillWidth: true }

                Text {
                    text: qsTr("RemoteMicRC003 v") + SettingsController.version
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                    Accessible.ignored: true
                }
            }
        }
    }
}