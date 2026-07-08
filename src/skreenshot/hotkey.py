"""Hotkey install/uninstall. The app itself registers NO global hotkeys
(no XGrabKey, no portal): the desktop environment's own shortcut daemon
does the binding and simply runs the `skreenshot` command, which is both
more reliable (the DE daemon already owns the keyboard grab) and free of a
resident process.

XFCE: one xfconf property on the xfce4-keyboard-shortcuts channel.
xfsettingsd watches the channel and re-grabs live, so the binding works
immediately, no restart (Xfce 4.20).

KDE Plasma (5.27/6.x, X11 and Wayland): a desktop file with
X-KDE-GlobalAccel-CommandShortcut=true dropped into
~/.local/share/kglobalaccel/. X-KDE-Shortcuts IS the default binding.
kglobalacceld only scans at startup, so activation without relogin uses the
same D-Bus pair the KDE Shortcuts KCM uses: doRegister with a dummy actionId
(triggers parsing of the desktop file) followed by unregister of the dummy.
Follows kglobalaccel.h (actionId order: ComponentUnique, ActionUnique,
ComponentFriendly, ActionFriendly) and plasma-desktop
kcms/keys/globalaccelmodel.cpp (buildActionId + removeComponent).

The functions that build commands and file contents are pure so unit tests
can check them without a desktop environment.
"""

import logging
import os
import shutil
import subprocess

log = logging.getLogger("skreenshot")

XFCE_CHANNEL = "xfce4-keyboard-shortcuts"
# Lowercase keysym; the literal angle brackets are part of the property path.
XFCE_PROPERTY = "/commands/custom/<Shift><Super>s"
KDE_SHORTCUT = "Meta+Shift+S"
KDE_DESKTOP_NAME = "skreenshot.desktop"

SPECTACLE_CONFLICT_NOTE = (
    "note: some KDE setups bind Meta+Shift+S to Spectacle's region capture. "
    "If the key does nothing or launches Spectacle, resolve the conflict in "
    "System Settings > Keyboard > Shortcuts."
)


class HotkeyError(Exception):
    pass


def detect_de(environ=None):
    """'xfce', 'kde', or None from XDG_CURRENT_DESKTOP."""
    env = os.environ if environ is None else environ
    desktop = env.get("XDG_CURRENT_DESKTOP", "").upper()
    if "XFCE" in desktop:
        return "xfce"
    if "KDE" in desktop:
        return "kde"
    return None


def exec_path():
    """Absolute path the hotkey should run: the installed command when it
    exists, else this checkout's launcher."""
    installed = shutil.which("skreenshot")
    if installed:
        return os.path.abspath(installed)
    # package lives at <repo>/src/skreenshot, launcher at <repo>/skreenshot
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(os.path.dirname(src), "skreenshot")


# -- XFCE ----------------------------------------------------------------


def xfce_query_cmd():
    return ["xfconf-query", "-c", XFCE_CHANNEL, "-p", XFCE_PROPERTY]


def xfce_install_cmd(command_path):
    # -n -t string are required the first time the property is created.
    return [
        "xfconf-query",
        "-c",
        XFCE_CHANNEL,
        "-n",
        "-t",
        "string",
        "-p",
        XFCE_PROPERTY,
        "-s",
        command_path,
    ]


def xfce_uninstall_cmd():
    return ["xfconf-query", "-c", XFCE_CHANNEL, "-p", XFCE_PROPERTY, "-r"]


def _xfce_current_value():
    """Current binding for the property, or None when unset."""
    try:
        result = subprocess.run(
            xfce_query_cmd(), capture_output=True, text=True, timeout=10
        )
    except OSError as exc:
        raise HotkeyError(f"xfconf-query not usable: {exc}") from exc
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def install_xfce():
    path = exec_path()
    current = _xfce_current_value()
    if current == path:
        print(f"hotkey already installed: Shift+Super+S -> {path}")
        return 0
    if current:
        # Never silently steal a key the user bound to something else.
        raise HotkeyError(
            f"Shift+Super+S is already bound to {current!r}; "
            "remove that binding first (xfce4-keyboard-settings)"
        )
    subprocess.run(xfce_install_cmd(path), check=True, timeout=10)
    verify = _xfce_current_value()
    if verify != path:
        raise HotkeyError("xfconf property did not stick; hotkey not installed")
    print(f"hotkey installed: Shift+Super+S -> {path} (active immediately)")
    return 0


def uninstall_xfce():
    current = _xfce_current_value()
    if current is None:
        print("hotkey not installed; nothing to do")
        return 0
    if current != exec_path():
        raise HotkeyError(
            f"Shift+Super+S is bound to {current!r}, not to skreenshot; "
            "refusing to remove someone else's binding"
        )
    subprocess.run(xfce_uninstall_cmd(), check=True, timeout=10)
    print("hotkey removed: Shift+Super+S is unbound again")
    return 0


# -- KDE -----------------------------------------------------------------


def kde_desktop_file_content(command_path, icon="skreenshot"):
    # X-KDE-Shortcuts IS the default binding; no config write is needed.
    # NoDisplay=true must NOT be set: it stops kglobalacceld's startup scan
    # from loading the file.
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=skreenshot\n"
        f"Exec={command_path}\n"
        f"Icon={icon}\n"
        "StartupNotify=false\n"
        "X-KDE-GlobalAccel-CommandShortcut=true\n"
        f"X-KDE-Shortcuts={KDE_SHORTCUT}\n"
    )


def kde_desktop_file_path(home=None):
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".local", "share", "kglobalaccel", KDE_DESKTOP_NAME)


def kde_register_cmd():
    """The KCM's dummy-register call: makes kglobalacceld parse the desktop
    file now instead of at next login. actionId field order per
    KGlobalAccel::actionIdFields: [ComponentUnique, ActionUnique,
    ComponentFriendly, ActionFriendly]."""
    return [
        "gdbus",
        "call",
        "--session",
        "--dest",
        "org.kde.kglobalaccel",
        "--object-path",
        "/kglobalaccel",
        "--method",
        "org.kde.KGlobalAccel.doRegister",
        f"['{KDE_DESKTOP_NAME}', '', 'skreenshot', '']",
    ]


def kde_unregister_cmd():
    """Removes the dummy action registered above (KCM does the same), and on
    uninstall stops the active grab."""
    return [
        "gdbus",
        "call",
        "--session",
        "--dest",
        "org.kde.kglobalaccel",
        "--object-path",
        "/kglobalaccel",
        "--method",
        "org.kde.KGlobalAccel.unregister",
        KDE_DESKTOP_NAME,
        "",
    ]


def _run_quiet(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except OSError:
        return False


def install_kde():
    path = exec_path()
    desktop_path = kde_desktop_file_path()
    os.makedirs(os.path.dirname(desktop_path), mode=0o755, exist_ok=True)
    with open(desktop_path, "w") as fh:
        fh.write(kde_desktop_file_content(path))
    # Poke kglobalacceld so the binding works without relogin.
    registered = _run_quiet(kde_register_cmd()) and _run_quiet(kde_unregister_cmd())
    print(f"hotkey desktop file installed: {desktop_path} ({KDE_SHORTCUT})")
    if not registered:
        print(
            "could not reach kglobalacceld over D-Bus; the binding will be "
            "picked up at next login"
        )
    print(SPECTACLE_CONFLICT_NOTE)
    return 0


def uninstall_kde():
    desktop_path = kde_desktop_file_path()
    if not os.path.exists(desktop_path):
        print("hotkey not installed; nothing to do")
        return 0
    # Stop the grab, then remove the file and clean the component up the way
    # the KDE Shortcuts KCM does (getComponent + cleanUp). Best effort: on a
    # dead session bus the file removal alone takes effect at next login.
    _run_quiet(kde_unregister_cmd())
    os.unlink(desktop_path)
    _kde_cleanup_component()
    print("hotkey desktop file removed")
    return 0


def _kde_cleanup_component():
    try:
        result = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.kde.kglobalaccel",
                "--object-path",
                "/kglobalaccel",
                "--method",
                "org.kde.KGlobalAccel.getComponent",
                KDE_DESKTOP_NAME,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
        return
    if result.returncode != 0:
        return
    # gdbus prints: (objectpath '/component/...',)
    out = result.stdout
    start = out.find("'")
    end = out.rfind("'")
    if start < 0 or end <= start:
        return
    component_path = out[start + 1 : end]
    _run_quiet(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.kde.kglobalaccel",
            "--object-path",
            component_path,
            "--method",
            "org.kde.kglobalaccel.Component.cleanUp",
        ]
    )


# -- entry points --------------------------------------------------------


def install(de=None):
    de = de or detect_de()
    if de == "xfce":
        return install_xfce()
    if de == "kde":
        return install_kde()
    raise HotkeyError(
        "could not detect a supported desktop (XFCE or KDE); "
        "pass --de xfce or --de kde"
    )


def uninstall(de=None):
    de = de or detect_de()
    if de == "xfce":
        return uninstall_xfce()
    if de == "kde":
        return uninstall_kde()
    raise HotkeyError(
        "could not detect a supported desktop (XFCE or KDE); "
        "pass --de xfce or --de kde"
    )
