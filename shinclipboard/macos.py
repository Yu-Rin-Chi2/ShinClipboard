"""macOS integration: the permissions this app needs and how to ask for them.

Two of the app's headline features are privileged on macOS. Watching for the
double Ctrl tap and replaying Cmd+V need Accessibility; freezing the screen for a
screenshot needs Screen Recording. Neither can be granted from inside the app, so
everything here is about reporting the current state and opening the right pane
of System Settings.

Every function is safe to call on any platform and returns a neutral answer when
the frameworks are missing, so callers never need to guard.
"""

from __future__ import annotations

import subprocess

from .platform_support import IS_MAC


# kVK_ANSI_*: where a key sits on the board, which is what macOS reports
# alongside the character. The character is not enough on its own - the layout
# turns Option+S into "ß" - so shortcuts are matched on these instead.
KEY_CODES = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
    "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C,
    "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
    "=": 0x18, "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D,
    "]": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A,
    ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32,
}

PASTE_KEY_CODE = KEY_CODES["v"]

ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
SCREEN_RECORDING_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"

ACCESSIBILITY_HINT = (
    "「システム設定 > プライバシーとセキュリティ > アクセシビリティ」で ShinClipboard を許可してください。"
    "許可するとアプリがそれに気づき、ショートカットを自動で有効にします。"
)
SCREEN_RECORDING_HINT = (
    "「システム設定 > プライバシーとセキュリティ > 画面収録」で ShinClipboard を許可してください。"
    "許可のあとはアプリを再起動してください。"
)


def warm_up_input_monitoring_api() -> None:
    """Resolve `AXIsProcessTrusted` on the main thread, before listeners race for it.

    pynput reads that name from every listener thread it starts. pyobjc resolves
    such names lazily by loading them and then removing them from a shared table,
    and those two steps are not atomic: when two listeners start together, the
    second one can find the entry already gone and die with a KeyError, silently
    taking every global shortcut with it. Touching the name once from here caches
    it in the module's globals, so the threads never take that path at all.
    """
    if not IS_MAC:
        return
    accessibility_trusted()  # the listeners ask this too, from those same threads
    try:
        import HIServices

        HIServices.AXIsProcessTrusted()
    except ImportError:
        return
    except (AttributeError, KeyError):
        # The lazy lookup is already broken; bind the name from the umbrella
        # framework instead so pynput's own call finds something to call.
        try:
            import ApplicationServices

            HIServices.AXIsProcessTrusted = ApplicationServices.AXIsProcessTrusted
        except (ImportError, AttributeError):
            pass


def accessibility_trusted() -> bool:
    """Whether macOS lets this process watch and post keystrokes."""
    if not IS_MAC:
        return True
    try:
        import ApplicationServices

        return bool(ApplicationServices.AXIsProcessTrusted())
    except (ImportError, AttributeError, KeyError):
        return True  # unknown: let the feature try and report its own failure


def request_accessibility() -> bool:
    """Show the system's own "grant Accessibility" prompt.

    macOS only shows it once per app, so the caller must still be able to point
    the user at System Settings afterwards.
    """
    if not IS_MAC:
        return True
    try:
        import ApplicationServices

        options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        return bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))
    except (ImportError, AttributeError, KeyError):
        return False


def screen_recording_allowed() -> bool:
    """Whether this process may read the screen."""
    if not IS_MAC:
        return True
    try:
        import Quartz

        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except (ImportError, AttributeError):
        return True  # too old to have the check, which means it is not enforced


def request_screen_recording() -> bool:
    """Show the system's own "grant Screen Recording" prompt."""
    if not IS_MAC:
        return True
    try:
        import Quartz

        return bool(Quartz.CGRequestScreenCaptureAccess())
    except (ImportError, AttributeError):
        return False


def menu_bar_height() -> int:
    """How much of the main display's top edge macOS keeps for its menu bar.

    The menu bar is painted over everything, a topmost borderless window
    included, so anything the overlay puts along the top has to start below it.
    Returns 0 where there is no menu bar to avoid.
    """
    if not IS_MAC:
        return 0
    try:
        import AppKit

        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return 0
        frame, visible = screen.frame(), screen.visibleFrame()
        # Cocoa measures from the bottom-left, so the menu bar is what the
        # visible frame gives up at its top edge.
        inset = frame.size.height - (visible.origin.y - frame.origin.y) - visible.size.height
        return max(0, int(round(inset)))
    except (ImportError, AttributeError):
        return 0


def send_paste_keystroke() -> bool:
    """Press Cmd+V in whatever application is in front now.

    Quartz posts the event directly rather than going through pynput's keyboard
    controller. That controller builds a unicode-to-keycode table from the
    current input source the first time it is created, and on current macOS that
    reaches into Carbon through an untyped ctypes call and takes the whole
    process down with SIGILL. Nothing here needs the table: `PASTE_KEY_CODE` is a
    physical key position, so it means "the V key" whatever the layout says.
    """
    if not IS_MAC:
        return False
    try:
        import Quartz

        for pressed in (True, False):
            # A null source keeps the keys the user is physically holding - the
            # second Ctrl of the tap that opened the popup - out of the event.
            event = Quartz.CGEventCreateKeyboardEvent(None, PASTE_KEY_CODE, pressed)
            if event is None:
                return False
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        return True
    except (ImportError, AttributeError, ValueError):
        return False


def activate_app() -> bool:
    """Bring this process to the front so a window it raises gets the keyboard.

    macOS hands the keyboard to the active *application*, not to whichever window
    asks loudest, so a menu bar app summoning its popup has to say so explicitly
    or the keystrokes keep going to whatever the user was typing in.

    It has to be this deprecated call. Both of the newer ways to ask - `activate`
    on the application, `activateWithOptions:` on our own `NSRunningApplication` -
    go through the cooperative activation macOS 14 introduced, which declines to
    move the keyboard to an app the user did not click on. That is the whole
    situation here: the request comes from a global shortcut, and the popup that
    follows is unusable if it cannot read the arrow keys.
    """
    if not IS_MAC:
        return False
    try:
        import AppKit

        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        return True
    except (ImportError, AttributeError):
        return False


def send_window_to_back(title: str) -> bool:
    """Drop one of this app's own windows behind everything else on screen.

    Activating the app raises the windows it owns, and the settings window is
    usually one of them: a screenful of it in front of the work the popup is
    meant to sit on top of. Ordering it back undoes exactly that, and touches
    neither which window has the keyboard nor which app is active.
    """
    if not IS_MAC:
        return False
    try:
        import AppKit

        for window in AppKit.NSApplication.sharedApplication().windows():
            if window.title() == title and window.isVisible():
                window.orderBack_(None)
                return True
        return False
    except (ImportError, AttributeError):
        return False


def frontmost_app():
    """The application in front right now, to hand the keyboard back to later.

    Returns None when that application is this one, or when the answer cannot be
    had, so a caller can simply skip the hand-back.
    """
    if not IS_MAC:
        return None
    try:
        import AppKit

        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        own = AppKit.NSRunningApplication.currentApplication()
        if app is None or app.processIdentifier() == own.processIdentifier():
            return None
        return app
    except (ImportError, AttributeError):
        return None


def activate_running_app(app) -> bool:
    """Give the keyboard back to the application the popup interrupted.

    This is what makes the popup feel like it opened *over* the user's work
    rather than instead of it: the window it covered is active again the moment
    the popup closes, and it is also the window the replayed Cmd+V lands in.
    """
    if not IS_MAC or app is None:
        return False
    try:
        import AppKit

        return bool(app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps))
    except (ImportError, AttributeError):
        return False


def hide_app() -> bool:
    """Step out of the way and give the previous application its focus back.

    Withdrawing the windows is not enough: the app stays active and the paste
    would land in nothing. Hiding is what makes macOS activate whoever was in
    front before, which is exactly the window the text is meant for.
    """
    if not IS_MAC:
        return False
    try:
        import AppKit

        AppKit.NSApplication.sharedApplication().hide_(None)
        return True
    except (ImportError, AttributeError):
        return False


def open_settings_pane(url: str) -> bool:
    """Open a pane of System Settings by its URL."""
    if not IS_MAC:
        return False
    try:
        return subprocess.run(["open", url], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
