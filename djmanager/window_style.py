"""Windows title bar and frame in DJ Manager's colours instead of the white system default.

pywebview only darkens the title bar when Windows itself is in dark mode and switches it
back on every theme change. DJ Manager is always dark, so its window is too:
- Windows 11: title bar, title text and border get the exact colours of the top bar.
- Windows 10 (20H1+): the dark title bar (custom colours are not supported there).
Other systems keep their window manager's frame.
"""

from __future__ import annotations

import sys

# Colours of app.css (--bg is the top bar right below the title bar, --text its text)
CAPTION = "#0c0b10"
TEXT = "#ecebf2"
BORDER = "#1f1e27"  # --line

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19  # Windows 10 before 20H1
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def colorref(color: str) -> int:
    """'#rrggbb' -> Win32 COLORREF (0x00bbggrr)."""
    r, g, b = (int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return r | (g << 8) | (b << 16)


def style_hwnd(hwnd: int, dwm=None) -> None:
    """Apply the colours to a native window. Unsupported attributes fail harmlessly."""
    import ctypes
    from ctypes import wintypes

    dwm = dwm or ctypes.windll.dwmapi

    def set_attr(attr: int, value: int) -> bool:
        data = ctypes.c_int(value)
        return dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), wintypes.DWORD(attr),
                                         ctypes.byref(data), ctypes.sizeof(data)) == 0

    if not set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE, 1):
        set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, 1)
    set_attr(DWMWA_CAPTION_COLOR, colorref(CAPTION))
    set_attr(DWMWA_TEXT_COLOR, colorref(TEXT))
    set_attr(DWMWA_BORDER_COLOR, colorref(BORDER))


def install(window) -> None:
    """Hook into a pywebview window (call before webview.start())."""
    if not sys.platform.startswith("win"):
        return

    def before_show(window) -> None:
        try:
            form = window.native  # the WinForms form; its handle exists before it is shown
            hwnd = form.Handle.ToInt32()
            style_hwnd(hwnd)
            # pywebview resets the title bar to the system theme when that changes; this
            # handler is registered after pywebview's, so it runs last and wins.
            from Microsoft.Win32 import SystemEvents  # pythonnet, loaded by pywebview

            SystemEvents.UserPreferenceChanged += lambda *_: style_hwnd(hwnd)
        except Exception as exc:  # cosmetic only - never keep the window from opening
            print(f"Window colours not applied: {exc}", flush=True)

    window.events.before_show += before_show
