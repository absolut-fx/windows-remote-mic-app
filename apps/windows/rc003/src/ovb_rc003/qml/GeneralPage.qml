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
                            "启用后将在当前用户的\"启动\"文件夹放置一个指向本程序的快捷方式，登录后自动打开设置窗口。"
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

            // -- Bridge auto-start (headless ``--bridge`` on login) ---------
            // Sibling rectangle to the autostart toggle above. The two
            // toggles are independent on disk - one writes
            // ``RemoteMicRC003.lnk`` (settings window), the other writes
            // ``RemoteMicRC003-Bridge.lnk`` (this one). Same Switch
            // binding pattern, same ``when: !pressed`` loop guard.
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: bridgeAutostartColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: bridgeAutostartColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: qsTr("登录后后台运行调度桥")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                            Layout.fillWidth: true
                        }
                        Switch {
                            id: bridgeAutostartSwitch
                            objectName: "bridgeAutoStartSwitch"
                            text: ""
                            checked: SettingsController.bridgeAutoStart
                            onCheckedChanged: {
                                if (checked !== SettingsController.bridgeAutoStart
                                        && !pressed) {
                                    SettingsController.setBridgeAutoStart(checked)
                                }
                            }
                            Accessible.name: qsTr("登录后后台运行调度桥")
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr(
                            "启用后将在当前用户的\"启动\"文件夹放置第二个快捷方式（RemoteMicRC003-Bridge.lnk），登录后无窗口运行调度桥服务。"
                        )
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.bridgeAutoStartStatusText
                        color: SettingsController.bridgeAutoStartStatusIsError
                            ? tokens.errorColor
                            : tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }
            }

            // -- Elevated autostart (Scheduled Task) --------------------------
            // Runs both exes at logon under the Administrators token so
            // Frida TAP can inject WUDFHost.exe (which denies a plain
            // Admins DACL). This is the "just works on every boot"
            // setting; the launch-rmic-elevated.ps1 script does the
            // actual launching. The task itself is registered once via
            // register-rmic-autostart.ps1 - until that has been run the
            // status text tells the user so and the switch is disabled.
            Rectangle {
                Layout.fillWidth: true
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                implicitHeight: elevatedColumn.implicitHeight + tokens.spacingLarge * 2

                ColumnLayout {
                    id: elevatedColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingLarge
                    spacing: tokens.spacingSmall

                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: qsTr("\u767b\u5f55\u540e\u4ee5\u7ba1\u7406\u5458\u8eab\u4efd\u542f\u52a8\u8bbe\u7f6e\u4e0e\u8c03\u5ea6\u6865")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                            Layout.fillWidth: true
                        }
                        Switch {
                            id: elevatedAutostartSwitch
                            objectName: "elevatedAutostartSwitch"
                            text: ""
                            enabled: SettingsController.elevatedAutostartRegistered
                            checked: SettingsController.elevatedAutostart
                            onCheckedChanged: {
                                if (checked !== SettingsController.elevatedAutostart
                                        && !pressed) {
                                    SettingsController.setElevatedAutostart(checked)
                                }
                            }
                            Accessible.name: qsTr("\u767b\u5f55\u540e\u4ee5\u7ba1\u7406\u5458\u8eab\u4efd\u542f\u52a8")
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr(
                            "\u542f\u7528\u540e\u4e0b\u6b21\u767b\u5f55\u4f1a\u540c\u65f6\u542f\u52a8\u8bbe\u7f6e\u7a97\u53e3\u4e0e\u540e\u53f0\u8c03\u5ea6\u6865\uff0c\u4ee5\u7ba1\u7406\u5458\u8eab\u4efd\u8fd0\u884c\u3002\u8fd9\u662f Frida TAP \u63a5\u5165 WUDFHost.exe \u7684\u524d\u63d0\uff0c\u542f\u7528\u540e\u97f3\u91cf\u6309\u94ae\u3001\u8fd4\u56de\u7b49\u6d88\u8d39\u8005\u6309\u94ae\u624d\u80fd\u5230\u8fbe Windows \u3002"
                        )
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: tokens.spacingSmall
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: SettingsController.elevatedAutostartStatusText
                            color: SettingsController.elevatedAutostartStatusIsError
                                ? tokens.errorColor
                                : tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        Button {
                            objectName: "refreshElevatedAutostartButton"
                            text: qsTr("\u91cd\u65b0\u68c0\u6d4b")
                            enabled: SettingsController.elevatedAutostartStatusIsError
                            onClicked: SettingsController.refreshElevatedAutostartStatus()
                        }
                    }
                }
            }

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