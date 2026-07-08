"""Hotkey command/file generation. Pure functions, no DE needed."""

from skreenshot import hotkey


class TestDetectDe:
    def test_xfce(self):
        assert hotkey.detect_de({"XDG_CURRENT_DESKTOP": "XFCE"}) == "xfce"

    def test_kde(self):
        assert hotkey.detect_de({"XDG_CURRENT_DESKTOP": "KDE"}) == "kde"

    def test_kde_colon_list(self):
        # Plasma sometimes reports e.g. "KDE" inside a colon-separated list.
        assert hotkey.detect_de({"XDG_CURRENT_DESKTOP": "plasma:KDE"}) == "kde"

    def test_unknown(self):
        assert hotkey.detect_de({"XDG_CURRENT_DESKTOP": "GNOME"}) is None
        assert hotkey.detect_de({}) is None


class TestXfceCommands:
    def test_install_command(self):
        cmd = hotkey.xfce_install_cmd("/home/user/.local/bin/skreenshot")
        assert cmd[0] == "xfconf-query"
        assert "-c" in cmd and "xfce4-keyboard-shortcuts" in cmd
        # -n -t string are required on first property creation.
        assert "-n" in cmd
        assert "string" in cmd
        i = cmd.index("-p")
        assert cmd[i + 1] == "/commands/custom/<Shift><Super>s"
        i = cmd.index("-s")
        assert cmd[i + 1] == "/home/user/.local/bin/skreenshot"

    def test_property_uses_lowercase_keysym(self):
        assert hotkey.XFCE_PROPERTY.endswith("s")
        assert not hotkey.XFCE_PROPERTY.endswith("S")

    def test_uninstall_command(self):
        cmd = hotkey.xfce_uninstall_cmd()
        assert "-r" in cmd
        assert "/commands/custom/<Shift><Super>s" in cmd

    def test_does_not_touch_print_bindings(self):
        # Kali's default Print/Shift+Print run xfce4-screenshooter and must
        # be left alone.
        for cmd in (hotkey.xfce_install_cmd("/x"), hotkey.xfce_uninstall_cmd()):
            assert not any("Print" in part for part in cmd)


class TestKdeDesktopFile:
    def test_content_has_required_keys(self):
        content = hotkey.kde_desktop_file_content("/usr/local/bin/skreenshot")
        assert content.startswith("[Desktop Entry]\n")
        assert "Type=Application\n" in content
        assert "Name=skreenshot\n" in content
        assert "Exec=/usr/local/bin/skreenshot\n" in content
        assert "Icon=skreenshot\n" in content
        assert "StartupNotify=false\n" in content
        assert "X-KDE-GlobalAccel-CommandShortcut=true\n" in content
        # X-KDE-Shortcuts IS the default binding, no config write needed.
        assert "X-KDE-Shortcuts=Meta+Shift+S\n" in content

    def test_no_nodisplay(self):
        # NoDisplay=true stops kglobalacceld's startup scan from loading
        # the file; it must never be set.
        content = hotkey.kde_desktop_file_content("/x")
        assert "NoDisplay" not in content

    def test_path_is_user_kglobalaccel_dir(self):
        path = hotkey.kde_desktop_file_path(home="/home/user")
        assert path == "/home/user/.local/share/kglobalaccel/skreenshot.desktop"


class TestKdeDbusCommands:
    def test_register_call(self):
        cmd = hotkey.kde_register_cmd()
        assert "org.kde.kglobalaccel" in cmd
        assert "/kglobalaccel" in cmd
        assert "org.kde.KGlobalAccel.doRegister" in cmd
        # actionId order per KGlobalAccel::actionIdFields: ComponentUnique,
        # ActionUnique, ComponentFriendly, ActionFriendly.
        assert cmd[-1] == "['skreenshot.desktop', '', 'skreenshot', '']"

    def test_unregister_call(self):
        cmd = hotkey.kde_unregister_cmd()
        assert "org.kde.KGlobalAccel.unregister" in cmd
        assert cmd[-2:] == ["skreenshot.desktop", ""]


class TestExecPath:
    def test_absolute(self):
        assert hotkey.exec_path().startswith("/")
