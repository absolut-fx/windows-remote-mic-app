// "通用" tab (XRBM-EXTENSION tray+autostart): system tray toggle, start-with-
// Windows toggle, and a short summary card explaining the relationship between
// the two (what "close window" actually does in each combination).
//
// Every string shown here comes from SettingsController's *_status
// properties (or, for the descriptive labels, from the qsTr() catalog) -
// this file only lays the text out, it never decides what the text says.

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            width: root.width - tokens.spacingLarge * 2
            x: tokens.spacingLarge
            y: tokens.spacingLarge
            spacing: tokens.spacingLarge

            // -- System tray toggle ------------------------------------------
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: trayColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: trayColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: qsTr("启用系统托盘图标")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                            Layout.fillWidth: true
                        }
                        Switch {
                            id: traySwitch
                            objectName: "trayEnabledSwitch"
                            text: ""
                            checked: SettingsController.trayEnabled
                            enabled: SettingsController.trayAvailable
                            Accessible.name: qsTr("启用系统托盘图标")
                            onToggled: SettingsController.setTrayEnabled(checked)
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr(
                            "启用后关闭设置窗口时程序继续在托盘后台运行，单击托盘图标可重新打开设置窗口。"
                        )
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.trayStatusText
                        color: SettingsController.trayAvailable
                            ? tokens.textSecondary
                            : tokens.errorColor
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }
            }

            // -- Start with Windows toggle -----------------------------------
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: autostartColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: autostartColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: qsTr("开机时自动启动")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                            Layout.fillWidth: true
                        }
                        Switch {
                            id: autostartSwitch
                            objectName: "startWithWindowsSwitch"
                            text: ""
                            // Two-way binding: ``checked`` always tracks
                            // SettingsController.startWithWindows, even
                            // after the user clicks (a one-way binding
                            // gets broken by the imperative assignment
                            // that Switch performs on click). The
                            // ``when: !pressed`` guard prevents feedback
                            // loops while the user is dragging the
                            // switch - we only re-push the property
                            // back to the controller when the user is
                            // not currently interacting with it.
                            checked: SettingsController.startWithWindows
                            onCheckedChanged: {
                                if (checked !== SettingsController.startWithWindows
                                        && !pressed) {
                                    SettingsController.setStartWithWindows(checked)
                                }
                            }
                            Accessible.name: qsTr("开机时自动启动")
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr(
                            "启用后注册并开启开机自启动计划任务（RemoteMicRC003-Elevated）：登录时由任务计划程序以管理员权限启动本程序。必须以管理员权限运行，Frida 才能注入 WUDFHost，音量/返回键才可用。"
                        )
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.startWithWindowsStatusText
                        color: SettingsController.startWithWindowsStatusIsError
                            ? tokens.errorColor
                            : tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }
            }

            // -- Close-the-panel-after-boot toggle ---------------------------
            // Only meaningful together with the start-with-Windows toggle
            // above, so it sits directly under it.
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: closeAfterBootColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: closeAfterBootColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: qsTr("开机启动后自动关闭设置窗口")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                            Layout.fillWidth: true
                        }
                        Switch {
                            id: closeAfterBootSwitch
                            objectName: "closePanelAfterBootSwitch"
                            text: ""
                            // Two-way binding with the same when-not-pressed
                            // guard as the start-with-Windows switch above -
                            // a one-way binding gets broken by the imperative
                            // assignment Switch performs on click.
                            checked: SettingsController.closePanelAfterBoot
                            onCheckedChanged: {
                                if (checked !== SettingsController.closePanelAfterBoot
                                        && !pressed) {
                                    SettingsController.setClosePanelAfterBoot(checked)
                                }
                            }
                            Accessible.name: qsTr("开机启动后自动关闭设置窗口")
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr(
                            "仅对登录自动启动生效：桥接启动成功后，设置窗口会隐藏到系统托盘，程序继续在后台运行（不会退出，也不会停止桥接），单击托盘图标可重新打开。手动打开设置窗口不受影响。桥接启动失败时窗口会保留，以便查看原因。需要托盘图标可用。"
                        )
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.closeAfterBootStatusText
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }
            }

            // NOTE: the "登录后后台运行调度桥" (bridge autostart) card used to sit
            // here. It bound SettingsController.bridgeAutoStart,
            // .bridgeAutoStartStatusText and .bridgeAutoStartStatusIsError -
            // none of which were ever declared on the controller (grep found
            // `bridgeAutoStart` only in this file), so Qt rendered it with empty
            // bindings and printed "Unable to assign [undefined] to bool /
            // QString" warnings on every launch. It could never have done
            // anything, and the headless `--bridge` mode it advertised was
            // removed at the user's request. Deleted rather than left as a
            // second control that also claims to own startup.

            // -- Closing-behavior summary ------------------------------------
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: closingColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: closingColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("关闭设置窗口时")
                        font.pixelSize: tokens.fontSizeTitle
                        font.bold: true
                        color: tokens.textPrimary
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.closingBehaviorText
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeBody
                    }
                }
            }
        }
    }
}