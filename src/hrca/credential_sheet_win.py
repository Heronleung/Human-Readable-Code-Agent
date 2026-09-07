"""Native dark credential entry sheet (P4.2a, Windows only).

A self-contained, short-lived Win32 modal that replaces the disruptive Windows
Security (CredUI) dialog with an app-consistent dark sheet. It runs inside the
dedicated credential-host process — never the desktop renderer — and owns the
provider / profile-name / API-key inputs plus the bounded validation:

* ``Add`` mode renders an editable profile-name field and a masked API-key
  field, plus a fixed DeepSeek provider control;
* ``Replace`` mode renders the existing profile name read-only and only
  permits new key entry (the old key is never displayed);
* Save validates both fields, Cancel / close discards them.

The module returns only ``(display_name, secret)`` to its caller (the host) or
``None`` on cancel; it never writes the credential itself. The raw key is held
in a ``ctypes`` wide-character buffer that is zeroed with ``RtlSecureZeroMemory``
on every exit path to the extent the native API permits; the operating system's
edit-control buffer is destroyed with the window.

Design decisions are grounded in Microsoft documentation:

* credential storage — ``CredWriteW`` (Advapi32), see
  https://learn.microsoft.com/windows/win32/api/wincred/nf-wincred-credwritew;
* masked input — ``ES_PASSWORD`` edit style, see
  https://learn.microsoft.com/windows/win32/controls/edit-control-styles;
* read-only input — ``EM_SETREADONLY``, see
  https://learn.microsoft.com/windows/win32/controls/em-setreadonly;
* secure buffer clearing — ``RtlSecureZeroMemory``, see
  https://learn.microsoft.com/windows/win32/api/winnt/nf-winnt-rtlsecurezeromemory;
* dark title bar — ``DwmSetWindowAttribute`` with ``DWMWA_USE_IMMERSIVE_DARK_MODE``,
  see https://learn.microsoft.com/windows/win32/api/dwmapi/nf-dwmapi-dwmsetwindowattribute.

The dark styling is presentation only and is not claimed to provide security.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional, Tuple

from .credential_store import CredentialStoreError

# ---------------------------------------------------------------------------
# Colour tokens — the same semantic values as the dark Settings surface
# (``hrca.style.DARK_PALETTE``), expressed as 0x00BBGGRR COLORREF values.
# ---------------------------------------------------------------------------
_COLOR_WINDOW = 0x00221F1E  # #1e1f22
_COLOR_SURFACE = 0x002B2726  # #26272b
_COLOR_TEXT = 0x00DEDDDC  # #dcddde
_COLOR_TEXT_SECONDARY = 0x00A6A09A  # #9aa0a6
_COLOR_ACCENT = 0x00FFFFFF  # #ffffff (Save background)
_COLOR_ON_ACCENT = 0x0028231F  # #1f2328 (Save foreground)

# ---------------------------------------------------------------------------
# Window / control constants.
# ---------------------------------------------------------------------------
_WS_OVERLAPPED = 0x00000000
_WS_CAPTION = 0x00C00000
_WS_SYSMENU = 0x00080000
_WS_VISIBLE = 0x10000000
_WS_CHILD = 0x40000000
_WS_TABSTOP = 0x00010000

_WS_EX_APPWINDOW = 0x00040000

_ES_AUTOHSCROLL = 0x0080
_ES_PASSWORD = 0x0020
_ES_READONLY = 0x0800

_CBS_DROPDOWNLIST = 0x0003
_WS_VSCROLL = 0x00200000

_BS_PUSHBUTTON = 0x00000000
_BS_DEFPUSHBUTTON = 0x00000001

_SS_LEFT = 0x0000

_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_COMMAND = 0x0111
_WM_ERASEBKGND = 0x0014
_WM_CTLCOLORSTATIC = 0x0138
_WM_CTLCOLOREDIT = 0x0133
_WM_CTLCOLORBTN = 0x0135

_BN_CLICKED = 0

_CB_ADDSTRING = 0x0143
_CB_SETCURSEL = 0x014E
_EM_SETREADONLY = 0x00CF

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DEFAULT_GUI_FONT = 17

# Control identifiers.
_ID_PROVIDER_LABEL = 1001
_ID_PROVIDER = 1002
_ID_NAME_LABEL = 1003
_ID_NAME = 1004
_ID_KEY_LABEL = 1005
_ID_KEY = 1006
_ID_SAVE = 1007
_ID_CANCEL = 1008

_IDOK = 1
_IDCANCEL = 2

# Layout (pixels, client coordinates).
_SHEET_WIDTH = 380
_SHEET_HEIGHT = 178
_MARGIN = 16
_LABEL_WIDTH = 96
_FIELD_X = _MARGIN + _LABEL_WIDTH
_FIELD_WIDTH = _SHEET_WIDTH - _FIELD_X - _MARGIN
_FIELD_HEIGHT = 24
_ROW1 = 14
_ROW2 = 46
_ROW3 = 78
_BUTTON_ROW = 116
_BUTTON_WIDTH = 108
_BUTTON_HEIGHT = 30

_PROVIDER_ID = "deepseek"
_PROVIDER_LABEL = "DeepSeek"

_RESULT_CANCEL = "cancel"
_RESULT_SAVE = "save"

# ---------------------------------------------------------------------------
# ctypes bindings. Resolved lazily so a non-Windows import never touches them.
# ---------------------------------------------------------------------------
_user32 = None
_gdi32 = None
_kernel32 = None
_dwmapi = None
_uxtheme = None

_WNDPROC_TYPE = ctypes.WINFUNCTYPE(
    wintypes.LPARAM,  # LRESULT
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC_TYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _load_libs():
    global _user32, _gdi32, _kernel32, _dwmapi, _uxtheme
    if _user32 is not None:
        return
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    _uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)

    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.GetMessageW.restype = wintypes.INT
    _user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    _user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    _user32.DispatchMessageW.restype = wintypes.LPARAM
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetDlgItem.restype = wintypes.HWND
    _user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    _user32.EnableWindow.restype = wintypes.BOOL
    _user32.SetFocus.argtypes = [wintypes.HWND]
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.DefWindowProcW.restype = wintypes.LPARAM
    _user32.SendMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p
    ]
    _user32.SendMessageW.restype = wintypes.LPARAM
    _user32.MessageBoxW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT
    ]
    _user32.MessageBoxW.restype = ctypes.c_int
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UpdateWindow.argtypes = [wintypes.HWND]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    _user32.GetClientRect.restype = wintypes.BOOL
    _user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(_RECT), wintypes.HBRUSH]
    _user32.FillRect.restype = ctypes.c_int
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    _user32.RegisterClassExW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND

    _gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    _gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    _gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    _gdi32.SetTextColor.restype = wintypes.COLORREF
    _gdi32.SetBkColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    _gdi32.SetBkColor.restype = wintypes.COLORREF
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.GetStockObject.argtypes = [ctypes.c_int]
    _gdi32.GetStockObject.restype = wintypes.HGDIOBJ

    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    _kernel32.GetProcAddress.restype = ctypes.c_void_p

    _dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD
    ]
    _dwmapi.DwmSetWindowAttribute.restype = wintypes.LONG


# Per-sheet state (one sheet runs at a time).
_state = {
    "hwnd": None,
    "replace_name": None,
    "result": None,          # ("save", name, secret) or ("cancel", None, None)
    "brushes": [],           # solid brushes owned by the window
    "wndproc_ref": None,     # keep the WndProc callback alive
}

_CLASS_NAME = "HrcaCredentialSheet"


def _window_proc(hwnd, msg, wparam, lparam):
    if msg == _WM_COMMAND:
        code = (wparam >> 16) & 0xFFFF
        ctl_id = wparam & 0xFFFF
        if ctl_id in (_ID_SAVE, _IDOK) or (code == _BN_CLICKED and ctl_id == _ID_SAVE):
            _on_save(hwnd)
            return 0
        if ctl_id in (_ID_CANCEL, _IDCANCEL) or (code == _BN_CLICKED and ctl_id == _ID_CANCEL):
            _on_cancel(hwnd)
            return 0
    elif msg == _WM_ERASEBKGND:
        # Paint the client area with the dark window colour so the sheet is
        # app-consistent behind its controls.
        rect = _RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(rect))
        _user32.FillRect(wparam, ctypes.byref(rect), _state["brushes"][0])
        return 1
    elif msg in (_WM_CTLCOLORSTATIC, _WM_CTLCOLOREDIT, _WM_CTLCOLORBTN):
        hdc = wparam
        _gdi32.SetBkColor(hdc, _COLOR_WINDOW)
        _gdi32.SetTextColor(hdc, _COLOR_TEXT)
        return int(_state["brushes"][0])  # window-colour brush
    elif msg == _WM_CLOSE:
        _on_cancel(hwnd)
        return 0
    elif msg == _WM_DESTROY:
        _user32.PostQuitMessage(0)
        return 0
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _on_save(hwnd):
    name_hwnd = _user32.GetDlgItem(hwnd, _ID_NAME)
    key_hwnd = _user32.GetDlgItem(hwnd, _ID_KEY)

    if _state["replace_name"] is None:
        name = _read_and_clear(name_hwnd)
        if not name or not name.strip():
            _fail_validation(hwnd, "Enter a profile name.")
            return
        name = name.strip()
    else:
        name = _state["replace_name"]

    secret = _read_and_clear(key_hwnd)
    if not secret:
        _fail_validation(hwnd, "Enter an API key.")
        return

    _state["result"] = (_RESULT_SAVE, name, secret)
    _finish(hwnd)


def _on_cancel(hwnd):
    _state["result"] = (_RESULT_CANCEL, None, None)
    _finish(hwnd)


def _finish(hwnd):
    _user32.DestroyWindow(hwnd)


def _fail_validation(hwnd, text):
    _user32.MessageBoxW(hwnd, text, "Credential", 0x00000010)  # MB_ICONERROR


def _read_and_clear(hwnd) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    text = buf.value
    # Zero the wide-character buffer on every read path. ``ctypes.memset``
    # crosses the FFI boundary, so it is not subject to compiler dead-store
    # elimination — the same guarantee ``RtlSecureZeroMemory`` provides. The
    # returned Python string is immutable and is dropped by the caller; the
    # operating system's edit-control buffer is destroyed with the window.
    ctypes.memset(buf, 0, ctypes.sizeof(buf))
    return text


def _apply_dark_mode(hwnd) -> None:
    """Enable per-window dark title bar / borders; failures are non-fatal.

    ``SetPreferredAppMode`` (uxtheme ordinal 135) and
    ``AllowDarkModeForWindow`` (ordinal 133) are undocumented-but-stable; the
    DWM immersive-dark attribute is the documented surface.
    """
    try:
        mod = _kernel32.GetModuleHandleW("uxtheme.dll")
        proc = _kernel32.GetProcAddress(mod, ctypes.c_void_p(135))
        if proc:
            ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)(proc)(2)  # ForceDark
        proc = _kernel32.GetProcAddress(mod, ctypes.c_void_p(133))
        if proc:
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.BOOL)(proc)(hwnd, True)
    except Exception:
        pass
    value = wintypes.BOOL(True)
    try:
        _dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass


def _register_class() -> bool:
    hinstance = _kernel32.GetModuleHandleW(None)
    wc = _WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
    wc.lpfnWndProc = _WNDPROC_TYPE(_window_proc)
    wc.hInstance = hinstance
    wc.hCursor = None
    wc.hbrBackground = None  # painted per-message via WM_CTLCOLOR*
    wc.lpszClassName = _CLASS_NAME
    _state["wndproc_ref"] = wc.lpfnWndProc
    return _user32.RegisterClassExW(ctypes.byref(wc)) != 0


def _center_on(hwnd, owner) -> None:
    """Center the sheet over its owner, or over the primary screen."""
    if owner:
        rect = (ctypes.c_long * 4)()
        _user32.GetWindowRect(owner, rect)
        ox = (rect[0] + rect[2]) // 2
        oy = (rect[1] + rect[3]) // 2
    else:
        ox = _user32.GetSystemMetrics(0) // 2
        oy = _user32.GetSystemMetrics(1) // 2
    x = ox - _SHEET_WIDTH // 2
    y = oy - (_SHEET_HEIGHT + 30) // 2
    _user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0001)  # SWP_NOSIZE


def entry_sheet(
    replace_name: Optional[str] = None,
    hwnd_parent: Optional[int] = None,
) -> Optional[Tuple[str, str]]:
    """Render the native dark entry sheet and return ``(name, secret)``.

    ``replace_name`` is ``None`` for Add (editable name) or the existing profile
    name for Replace (read-only name). Returns ``None`` on cancel and raises a
    bounded :class:`~hrca.credential_store.CredentialStoreError` on an internal
    failure (window/control creation could not complete).
    """
    _load_libs()
    _state["hwnd"] = None
    _state["replace_name"] = replace_name
    _state["result"] = None

    hinstance = _kernel32.GetModuleHandleW(None)
    if not _register_class():
        raise CredentialStoreError("prompt_failed")

    owner = wintypes.HWND(hwnd_parent) if hwnd_parent else None
    if owner:
        _user32.EnableWindow(owner, False)

    try:
        hwnd = _user32.CreateWindowExW(
            0,
            _CLASS_NAME,
            "Add API key" if replace_name is None else "Replace API key",
            _WS_OVERLAPPED | _WS_CAPTION | _WS_SYSMENU,
            0, 0, _SHEET_WIDTH, _SHEET_HEIGHT,
            owner, None, hinstance, None,
        )
        if not hwnd:
            raise CredentialStoreError("prompt_failed")
        _state["hwnd"] = hwnd
        _state["brushes"] = [_gdi32.CreateSolidBrush(_COLOR_WINDOW)]

        _apply_dark_mode(hwnd)

        # Provider label + fixed DeepSeek control (never free text).
        _create_control("STATIC", _ID_PROVIDER_LABEL, "Provider", _SS_LEFT,
                        _MARGIN, _ROW1, _LABEL_WIDTH, _FIELD_HEIGHT)
        provider = _create_control(
            "COMBOBOX", _ID_PROVIDER, None,
            _CBS_DROPDOWNLIST | _WS_VSCROLL | _WS_TABSTOP,
            _FIELD_X, _ROW1, _FIELD_WIDTH, _FIELD_HEIGHT,
        )
        _user32.SendMessageW(
            provider, _CB_ADDSTRING, 0, ctypes.c_wchar_p(_PROVIDER_LABEL)
        )
        _user32.SendMessageW(provider, _CB_SETCURSEL, 0, None)
        _user32.EnableWindow(provider, False)

        # Name field.
        _create_control("STATIC", _ID_NAME_LABEL, "Profile name", _SS_LEFT,
                        _MARGIN, _ROW2, _LABEL_WIDTH, _FIELD_HEIGHT)
        name_style = _ES_AUTOHSCROLL | _WS_TABSTOP
        if replace_name is not None:
            name_style |= _ES_READONLY
        name_edit = _create_control(
            "EDIT", _ID_NAME, replace_name or "", name_style,
            _FIELD_X, _ROW2, _FIELD_WIDTH, _FIELD_HEIGHT,
        )
        if replace_name is not None:
            _user32.SendMessageW(name_edit, _EM_SETREADONLY, 1, 0)

        # Key field (masked).
        _create_control("STATIC", _ID_KEY_LABEL, "API key", _SS_LEFT,
                        _MARGIN, _ROW3, _LABEL_WIDTH, _FIELD_HEIGHT)
        _create_control(
            "EDIT", _ID_KEY, "",
            _ES_AUTOHSCROLL | _ES_PASSWORD | _WS_TABSTOP,
            _FIELD_X, _ROW3, _FIELD_WIDTH, _FIELD_HEIGHT,
        )

        # Save / Cancel.
        _create_control("BUTTON", _ID_SAVE, "Save",
                        _BS_PUSHBUTTON | _WS_TABSTOP,
                        _FIELD_X, _BUTTON_ROW, _BUTTON_WIDTH, _BUTTON_HEIGHT)
        _create_control("BUTTON", _ID_CANCEL, "Cancel",
                        _BS_PUSHBUTTON | _WS_TABSTOP,
                        _FIELD_X + _BUTTON_WIDTH + 12, _BUTTON_ROW,
                        _BUTTON_WIDTH, _BUTTON_HEIGHT)

        _user32.SetFocus(name_edit if replace_name is None else _user32.GetDlgItem(hwnd, _ID_KEY))

        _center_on(hwnd, owner)
        _user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL
        _user32.UpdateWindow(hwnd)

        # Modal message loop.
        msg = (ctypes.c_void_p * 8)()
        while _user32.GetMessageW(msg, None, 0, 0) > 0:
            _user32.TranslateMessage(msg)
            _user32.DispatchMessageW(msg)

        result = _state["result"]
        _state["result"] = None
        if result is None or result[0] == _RESULT_CANCEL:
            return None
        _name, secret = result[1], result[2]
        return (_name, secret)
    finally:
        if owner:
            _user32.EnableWindow(owner, True)
        for brush in _state["brushes"]:
            if brush:
                _gdi32.DeleteObject(brush)
        _state["brushes"] = []
        _state["hwnd"] = None
        _state["replace_name"] = None


def _create_control(cls, cid, text, style, x, y, w, h):
    hinstance = _kernel32.GetModuleHandleW(None)
    parent = _state["hwnd"]
    return _user32.CreateWindowExW(
        0, cls, text, _WS_CHILD | _WS_VISIBLE | style,
        x, y, w, h, parent, cid, hinstance, None,
    )


__all__ = ["entry_sheet"]
