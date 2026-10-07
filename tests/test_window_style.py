"""Windows title bar colours (the DWM calls are faked, so this runs everywhere)."""

import re
from pathlib import Path

from djmanager import window_style


def test_colorref_is_bgr():
    assert window_style.colorref("#0c0b10") == 0x100b0c
    assert window_style.colorref("#ff0000") == 0x0000ff


def test_colours_match_the_app_css():
    css = (Path(window_style.__file__).parent / "static" / "app.css").read_text()
    var = lambda name: re.search(rf"--{name}:\s*(#[0-9a-f]{{6}})", css).group(1)  # noqa: E731
    assert (window_style.CAPTION, window_style.TEXT, window_style.BORDER) == (var("bg"), var("text"), var("line"))


class FakeDwm:
    def __init__(self, supported):
        self.supported, self.set = supported, {}

    def DwmSetWindowAttribute(self, hwnd, attr, data, size):  # noqa: N802 - Win32 name
        import ctypes
        attr = attr.value
        if attr not in self.supported:
            return -2147024809  # E_INVALIDARG
        self.set[attr] = ctypes.cast(data, ctypes.POINTER(ctypes.c_int)).contents.value
        return 0


def test_windows_11_gets_the_app_colours():
    dwm = FakeDwm({20, 34, 35, 36})
    window_style.style_hwnd(1234, dwm)
    assert dwm.set == {20: 1, 35: 0x100b0c, 36: window_style.colorref("#ecebf2"), 34: window_style.colorref("#1f1e27")}


def test_old_windows_10_falls_back_to_the_old_dark_mode_attribute():
    dwm = FakeDwm({19})
    window_style.style_hwnd(1234, dwm)  # colour attributes are unsupported there: no error
    assert dwm.set == {19: 1}
