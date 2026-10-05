"""ペースト前の確認ダイアログ(OBS メインウィンドウにモーダルな Win32 ネイティブ実装)。

Qt(PySide6 等)は OBS 本体が使う Qt6 ランタイムとバージョンが異なる可能性があり
(このマシンでは OBS が Qt 6.8.3、pip の PySide6 が Qt 6.10.0)、同一プロセス内で
2 つの Qt ビルドが混在すると ABI 不一致でクラッシュしうる。配信ツールのプロセスに
そのリスクを持ち込まないため、あえて素の Win32 API のみで実装している。

`describe_content()` / 画像プレビュー生成は純ロジックとしてユニットテスト可能。
`confirm_paste()` のみが実際にウィンドウを作成するブロッキング呼び出しで、
実 OBS プロセス内でしか安全に検証できない(E2E テストで検証する)。
"""

import ctypes
import os
import struct
import tempfile
import zlib
from ctypes import wintypes

from pasteboard_core.content import ImageBytesContent, ImageFileContent, PasteContent, TextContent
from pasteboard_core.dib import DibError, dib_to_rgba, rgba_to_dib, wrap_dib_as_bmp
from pasteboard_core.imaging import fit_within, resize_rgba_nearest
from pasteboard_core.png_codec import PngError, decode_png

DIALOG_TITLE = "OBS Pasteboard - ペースト確認"
_TEXT_PREVIEW_LENGTH = 300
_MAX_PREVIEW_SOURCE_PIXELS = 4_000_000
_PREVIEW_MAX_WIDTH = 480
_PREVIEW_MAX_HEIGHT = 360

_IDOK = 1
_IDCANCEL = 2


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _bmp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 26 or data[:2] != b"BM":
        return None
    width, height = struct.unpack("<ii", data[18:26])
    return width, abs(height)


def describe_content(content: PasteContent) -> str:
    """確認ダイアログ本文を組み立てる(純ロジック)。"""
    if isinstance(content, ImageBytesContent):
        size = _png_size(content.data) if content.extension == ".png" else _bmp_size(content.data)
        dimensions = f"{size[0]} x {size[1]} px" if size else "不明なサイズ"
        kilobytes = len(content.data) / 1024
        return (
            "クリップボードの画像をペーストします。\n\n"
            f"形式: {content.extension.lstrip('.').upper()}\n"
            f"サイズ: {dimensions}\n"
            f"容量: {kilobytes:.1f} KB"
        )
    if isinstance(content, ImageFileContent):
        return f"クリップボードの画像ファイルをペーストします。\n\nパス: {content.path}"
    if isinstance(content, TextContent):
        preview = content.text[:_TEXT_PREVIEW_LENGTH]
        if len(content.text) > _TEXT_PREVIEW_LENGTH:
            preview += "…"
        return f"クリップボードのテキストをペーストします。\n\n{preview}"
    return "クリップボードの内容をペーストします。"


def preview_rgba(content: PasteContent) -> tuple[bytes, int, int] | None:
    """プレビュー用サムネイルの (RGBA, 幅, 高さ) を返す。生成できなければ None。

    純ロジック(Win32 描画は行わない)。テキストや、デコードできない
    画像形式(大きすぎる低 bpp DIB・非対応 PNG 等)では None を返し、
    呼び出し側はテキスト説明のみのフォールバック表示にする。
    """
    if not isinstance(content, ImageBytesContent):
        return None
    try:
        if content.extension == ".png":
            rgba, width, height = decode_png(content.data)
        else:  # .bmp: BMP パススルー保存(低 bpp・非対応 DIB)。14 バイトのファイルヘッダを除く
            rgba, width, height = dib_to_rgba(content.data[14:])
    except (PngError, DibError, ValueError, struct.error, zlib.error):
        return None
    if width * height > _MAX_PREVIEW_SOURCE_PIXELS:
        return None
    new_width, new_height = fit_within(width, height, _PREVIEW_MAX_WIDTH, _PREVIEW_MAX_HEIGHT)
    if (new_width, new_height) != (width, height):
        rgba = resize_rgba_nearest(rgba, width, height, new_width, new_height)
    return rgba, new_width, new_height


# --- ここから Win32 API 定義 -------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)


def _rgb(red: int, green: int, blue: int) -> int:
    """COLORREF(0x00BBGGRR)を生成する。"""
    return red | (green << 8) | (blue << 16)


# OBS の既定テーマ Yami に合わせた配色
_COLOR_WINDOW_BG = _rgb(24, 24, 25)
_COLOR_TEXT = _rgb(225, 225, 227)
_COLOR_BUTTON_BG = _rgb(45, 48, 54)
_COLOR_BUTTON_PRESSED = _rgb(34, 36, 41)
_COLOR_BUTTON_BORDER = _rgb(70, 74, 82)
_COLOR_BUTTON_FOCUS_BORDER = _rgb(120, 160, 240)
_COLOR_ACCENT_BG = _rgb(48, 86, 190)
_COLOR_ACCENT_PRESSED = _rgb(38, 70, 156)
_COLOR_ACCENT_TEXT = _rgb(255, 255, 255)
_COLOR_PREVIEW_BORDER = _rgb(70, 74, 82)

WNDPROC_TYPE = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC_TYPE),
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


_user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_longlong
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UpdateWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.SetFocus.argtypes = [wintypes.HWND]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.AdjustWindowRectEx.argtypes = [
    ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
]
_user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.IsDialogMessageW.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.MSG)]
_user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.restype = ctypes.c_longlong
_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
_user32.LoadCursorW.restype = wintypes.HANDLE
_user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
_user32.LoadImageW.restype = wintypes.HANDLE
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.DrawTextW.argtypes = [
    wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT,
]
_user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
_user32.FrameRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
_gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
_gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
_gdi32.CreatePen.restype = wintypes.HANDLE
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
_gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
_gdi32.RoundRect.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int,
]
_gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
]
_gdi32.CreateFontW.restype = wintypes.HANDLE
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
_dwmapi.DwmSetWindowAttribute.argtypes = [
    wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
]


class _DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemAction", wintypes.UINT),
        ("itemState", wintypes.UINT),
        ("hwndItem", wintypes.HWND),
        ("hDC", wintypes.HDC),
        ("rcItem", wintypes.RECT),
        ("itemData", ctypes.c_size_t),
    ]

WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_BORDER = 0x00800000
WS_TABSTOP = 0x00010000
WS_EX_CONTROLPARENT = 0x00010000
WS_EX_DLGMODALFRAME = 0x00000001
SS_LEFT = 0x00000000
SS_BITMAP = 0x0000000E
BS_OWNERDRAW = 0x0000000B
IMAGE_BITMAP = 0
LR_LOADFROMFILE = 0x00000010
SW_SHOW = 5
SM_CXSCREEN = 0
SM_CYSCREEN = 1
WM_DESTROY = 0x0002
WM_SETFONT = 0x0030
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_COMMAND = 0x0111
WM_DRAWITEM = 0x002B
WM_CTLCOLORSTATIC = 0x0138
STM_SETIMAGE = 0x0172
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
ODS_SELECTED = 0x0001
ODS_FOCUS = 0x0010
TRANSPARENT = 1
PS_SOLID = 0
DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
IDC_ARROW = 32512

class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


_user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_PAINTSTRUCT)]
_user32.BeginPaint.restype = wintypes.HDC
_user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_PAINTSTRUCT)]

WM_PAINT = 0x000F

_WINDOW_CLASS_NAME = "ObsPasteboardConfirmDialog"
_pending_result: dict[str, bool | None] = {"value": None}
_preview_frame: dict[str, wintypes.RECT | None] = {"rect": None}
# ダイアログ終了フラグ。WM_QUIT をホストスレッド(OBS の scripting スレッド)の
# キューに注入すると他のメッセージ処理と干渉しうるため、PostQuitMessage は使わない。
_dialog_done: dict[str, bool] = {"value": False}
# 表示中ダイアログの hwnd(cleanup() からのクローズ要求用)
_active_hwnd: dict[str, int | None] = {"value": None}

# モジュール生存中は保持する GDI オブジェクト(ウィンドウクラス・描画で共用)。
# cleanup()(アンロード・再読み込み時)で解放する。
_BG_BRUSH = _gdi32.CreateSolidBrush(_COLOR_WINDOW_BG)
_PREVIEW_BORDER_BRUSH = _gdi32.CreateSolidBrush(_COLOR_PREVIEW_BORDER)
_UI_FONT = _gdi32.CreateFontW(
    -14, 0, 0, 0, 400, 0, 0, 0,
    1,  # DEFAULT_CHARSET
    0, 0,
    5,  # CLEARTYPE_QUALITY
    0, "Yu Gothic UI",
)


def _draw_owner_button(item: "_DRAWITEMSTRUCT") -> None:
    """OK / キャンセルボタンを OBS 風(角丸・ダーク・アクセント)に描画する。"""
    hdc = item.hDC
    rect = item.rcItem
    is_accent = item.CtlID == _IDOK
    pressed = bool(item.itemState & ODS_SELECTED)
    focused = bool(item.itemState & ODS_FOCUS)

    if is_accent:
        fill = _COLOR_ACCENT_PRESSED if pressed else _COLOR_ACCENT_BG
        text_color = _COLOR_ACCENT_TEXT
    else:
        fill = _COLOR_BUTTON_PRESSED if pressed else _COLOR_BUTTON_BG
        text_color = _COLOR_TEXT
    border = _COLOR_BUTTON_FOCUS_BORDER if focused else _COLOR_BUTTON_BORDER

    # 背景(親と同色)で塗ってから角丸ボタンを描く
    _user32.FillRect(hdc, ctypes.byref(rect), _BG_BRUSH)
    brush = _gdi32.CreateSolidBrush(fill)
    pen = _gdi32.CreatePen(PS_SOLID, 1, border)
    old_brush = _gdi32.SelectObject(hdc, brush)
    old_pen = _gdi32.SelectObject(hdc, pen)
    try:
        _gdi32.RoundRect(hdc, rect.left, rect.top, rect.right, rect.bottom, 8, 8)
    finally:
        _gdi32.SelectObject(hdc, old_brush)
        _gdi32.SelectObject(hdc, old_pen)
        _gdi32.DeleteObject(brush)
        _gdi32.DeleteObject(pen)

    buffer = ctypes.create_unicode_buffer(64)
    _user32.GetWindowTextW(item.hwndItem, buffer, 64)
    _gdi32.SetBkMode(hdc, TRANSPARENT)
    _gdi32.SetTextColor(hdc, text_color)
    old_font = _gdi32.SelectObject(hdc, _UI_FONT)
    try:
        text_rect = wintypes.RECT(rect.left, rect.top, rect.right, rect.bottom)
        _user32.DrawTextW(
            hdc, buffer.value, -1, ctypes.byref(text_rect),
            DT_CENTER | DT_VCENTER | DT_SINGLELINE,
        )
    finally:
        _gdi32.SelectObject(hdc, old_font)


def _wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_COMMAND:
        control_id = wparam & 0xFFFF
        if control_id in (_IDOK, _IDCANCEL):
            _pending_result["value"] = control_id == _IDOK
            _user32.DestroyWindow(hwnd)
            return 0
    elif msg == WM_CTLCOLORSTATIC:
        # 説明テキスト・プレビューの背景をウィンドウと同じダーク色にする
        _gdi32.SetTextColor(wparam, _COLOR_TEXT)
        _gdi32.SetBkMode(wparam, TRANSPARENT)
        return _BG_BRUSH
    elif msg == WM_DRAWITEM:
        item = _DRAWITEMSTRUCT.from_address(lparam)
        if item.CtlID in (_IDOK, _IDCANCEL):
            _draw_owner_button(item)
            return 1
    elif msg == WM_PAINT:
        paint = _PAINTSTRUCT()
        hdc = _user32.BeginPaint(hwnd, ctypes.byref(paint))
        frame = _preview_frame["rect"]
        if frame is not None:
            _user32.FrameRect(hdc, ctypes.byref(frame), _PREVIEW_BORDER_BRUSH)
        _user32.EndPaint(hwnd, ctypes.byref(paint))
        return 0
    elif msg == WM_CLOSE:
        _pending_result["value"] = False
        _user32.DestroyWindow(hwnd)
        return 0
    elif msg == WM_DESTROY:
        _dialog_done["value"] = True
        return 0
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_WNDPROC_REF = WNDPROC_TYPE(_wndproc)  # GC 対策で参照を保持し続ける


def _register_window_class() -> None:
    # スクリプト再読み込み(importlib.reload)後は、登録済みクラスの lpfnWndProc が
    # 旧モジュールの解放済み ctypes トランポリンを指したままになる。そのまま使うと
    # 次のダイアログ表示で解放済みメモリを実行してクラッシュするため、必ず
    # 登録解除してから現行モジュールのトランポリンで登録し直す。
    _user32.UnregisterClassW(_WINDOW_CLASS_NAME, _kernel32.GetModuleHandleW(None))
    wc = _WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
    wc.style = 0
    wc.lpfnWndProc = _WNDPROC_REF
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = _kernel32.GetModuleHandleW(None)
    wc.hIcon = None
    wc.hCursor = _user32.LoadCursorW(None, ctypes.cast(IDC_ARROW, wintypes.LPCWSTR))
    wc.hbrBackground = _BG_BRUSH
    wc.lpszMenuName = None
    wc.lpszClassName = _WINDOW_CLASS_NAME
    wc.hIconSm = None
    if not _user32.RegisterClassExW(ctypes.byref(wc)):
        # 1410(ERROR_CLASS_ALREADY_EXISTS)も含めて失敗は致命的として扱う。
        # ここで握りつぶすと古い wndproc が残り解放済みメモリ実行につながる。
        raise ctypes.WinError(ctypes.get_last_error())


_register_window_class()


def cleanup() -> None:
    """ウィンドウクラス登録と GDI オブジェクトを解放する。

    スクリプトのアンロード時・再読み込みの直前に呼ぶ。表示中のダイアログが
    あればクローズを依頼して戻る(モーダルループは scripting スレッド上で
    動いているため、別スレッドから DestroyWindow はできない)。
    """
    hwnd = _active_hwnd["value"]
    if hwnd:
        _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        # ウィンドウ生存中はクラス登録解除も GDI 解放もできない。
        # クラスは次回 _register_window_class() の登録解除→再登録で回収される。
        return
    _user32.UnregisterClassW(_WINDOW_CLASS_NAME, _kernel32.GetModuleHandleW(None))
    for handle in (_BG_BRUSH, _PREVIEW_BORDER_BRUSH, _UI_FONT):
        if handle:
            _gdi32.DeleteObject(handle)


def _find_own_top_level_window() -> int:
    """自プロセス(OBS 本体)の可視トップレベルウィンドウを 1 つ探す。見つからなければ 0。"""
    own_pid = os.getpid()
    found = 0

    def callback(hwnd, _lparam):
        nonlocal found
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        is_candidate = (
            pid.value == own_pid
            and _user32.IsWindowVisible(hwnd)
            and _user32.GetWindowTextLengthW(hwnd) > 0
        )
        if is_candidate:
            found = hwnd
            return False  # 列挙を打ち切る
        return True

    _user32.EnumWindows(_ENUM_WINDOWS_PROC(callback), 0)
    return found


def _force_foreground(hwnd: int) -> None:
    """フォアグラウンドロックによる SetForegroundWindow の抑止を回避する。"""
    foreground = _user32.GetForegroundWindow()
    current_thread = _kernel32.GetCurrentThreadId()
    target_thread = 0
    if foreground:
        target_thread = _user32.GetWindowThreadProcessId(foreground, None)
    attached = False
    if target_thread and target_thread != current_thread:
        attached = bool(_user32.AttachThreadInput(current_thread, target_thread, True))
    try:
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(current_thread, target_thread, False)


def _outer_size(
    client_width: int, client_height: int, style: int, ex_style: int
) -> tuple[int, int]:
    rect = wintypes.RECT(0, 0, client_width, client_height)
    _user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, ex_style)
    return rect.right - rect.left, rect.bottom - rect.top


def _center_over(owner_hwnd: int, width: int, height: int) -> tuple[int, int]:
    if owner_hwnd:
        rect = wintypes.RECT()
        _user32.GetWindowRect(owner_hwnd, ctypes.byref(rect))
        cx = rect.left + (rect.right - rect.left - width) // 2
        cy = rect.top + (rect.bottom - rect.top - height) // 2
        return cx, cy
    screen_w = _user32.GetSystemMetrics(SM_CXSCREEN)
    screen_h = _user32.GetSystemMetrics(SM_CYSCREEN)
    return (screen_w - width) // 2, (screen_h - height) // 2


def _write_preview_bmp(rgba: bytes, width: int, height: int) -> str:
    dib = rgba_to_dib(rgba, width, height)
    bmp = wrap_dib_as_bmp(dib)
    fd, path = tempfile.mkstemp(suffix=".bmp", prefix="obs_pasteboard_preview_")
    with os.fdopen(fd, "wb") as file:
        file.write(bmp)
    return path


def confirm_paste(content: PasteContent) -> bool:
    """OBS メインウィンドウにモーダルな確認ダイアログを表示し、[OK] なら True を返す。

    ブロッキング呼び出し。ホットキーコールバックのスレッドで完結する
    (Qt の UI スレッドを必要としない)。
    """
    message = describe_content(content)
    preview = preview_rgba(content)

    owner = _find_own_top_level_window()

    margin = 20
    button_width, button_height, button_gap = 110, 32, 12
    desc_height = 96
    preview_width, preview_height = (preview[1], preview[2]) if preview else (0, 0)

    client_width = max(440, preview_width + margin * 2)
    client_height = margin + desc_height + (preview_height + margin if preview else 0)
    client_height += button_height + margin

    style = WS_POPUP | WS_CAPTION | WS_SYSMENU
    ex_style = WS_EX_CONTROLPARENT | WS_EX_DLGMODALFRAME
    outer_width, outer_height = _outer_size(client_width, client_height, style, ex_style)
    pos_x, pos_y = _center_over(owner, outer_width, outer_height)

    _pending_result["value"] = None
    _preview_frame["rect"] = None
    _dialog_done["value"] = False
    bitmap_handle = None
    temp_path = None
    hwnd = None
    try:
        hwnd = _user32.CreateWindowExW(
            ex_style, _WINDOW_CLASS_NAME, DIALOG_TITLE, style,
            pos_x, pos_y, outer_width, outer_height,
            owner or None, None, _kernel32.GetModuleHandleW(None), None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        _active_hwnd["value"] = hwnd

        # タイトルバーも OBS と同じダークにする(Windows 10 20H1+)
        dark = ctypes.c_int(1)
        _dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), ctypes.sizeof(dark)
        )

        text_ctrl = _user32.CreateWindowExW(
            0, "STATIC", message, WS_CHILD | WS_VISIBLE | SS_LEFT,
            margin, margin, client_width - margin * 2, desc_height,
            hwnd, None, _kernel32.GetModuleHandleW(None), None,
        )
        _user32.SendMessageW(text_ctrl, WM_SETFONT, _UI_FONT, 1)

        preview_y = margin + desc_height
        if preview is not None:
            rgba, preview_width, preview_height = preview
            temp_path = _write_preview_bmp(rgba, preview_width, preview_height)
            bitmap_handle = _user32.LoadImageW(
                None, temp_path, IMAGE_BITMAP, preview_width, preview_height, LR_LOADFROMFILE
            )
            preview_x = (client_width - preview_width) // 2
            image_ctrl = _user32.CreateWindowExW(
                0, "STATIC", None, WS_CHILD | WS_VISIBLE | SS_BITMAP,
                preview_x, preview_y, preview_width, preview_height,
                hwnd, None, _kernel32.GetModuleHandleW(None), None,
            )
            if bitmap_handle:
                _user32.SendMessageW(image_ctrl, STM_SETIMAGE, IMAGE_BITMAP, bitmap_handle)
            # プレビューの周囲 1px に枠線を描く(WM_PAINT で描画)
            _preview_frame["rect"] = wintypes.RECT(
                preview_x - 1, preview_y - 1,
                preview_x + preview_width + 1, preview_y + preview_height + 1,
            )
            preview_y += preview_height + margin

        buttons_top = client_height - margin - button_height
        buttons_left = client_width - margin - button_width * 2 - button_gap
        ok_button = _user32.CreateWindowExW(
            0, "BUTTON", "OK (ペースト)",
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_OWNERDRAW,
            buttons_left, buttons_top, button_width, button_height,
            hwnd, _IDOK, _kernel32.GetModuleHandleW(None), None,
        )
        _user32.CreateWindowExW(
            0, "BUTTON", "キャンセル",
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_OWNERDRAW,
            buttons_left + button_width + button_gap, buttons_top, button_width, button_height,
            hwnd, _IDCANCEL, _kernel32.GetModuleHandleW(None), None,
        )

        if owner:
            _user32.EnableWindow(owner, False)
        _user32.ShowWindow(hwnd, SW_SHOW)
        _user32.UpdateWindow(hwnd)
        _user32.SetFocus(ok_button)
        _force_foreground(hwnd)

        # WM_DESTROY で _dialog_done が立つまで回す。DestroyWindow は WM_COMMAND /
        # WM_CLOSE のディスパッチ内で同期的に WM_DESTROY を処理するため、
        # 各ディスパッチ後のフラグ確認で確実にループを抜けられる。
        msg = wintypes.MSG()
        while not _dialog_done["value"]:
            if _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
                break  # WM_QUIT(外部要因)またはエラー
            # オーナードローでは BS_DEFPUSHBUTTON が使えないため Enter/Esc を自前処理
            if msg.message == WM_KEYDOWN and msg.wParam == VK_RETURN:
                _user32.SendMessageW(hwnd, WM_COMMAND, _IDOK, 0)
                continue
            if msg.message == WM_KEYDOWN and msg.wParam == VK_ESCAPE:
                _user32.SendMessageW(hwnd, WM_COMMAND, _IDCANCEL, 0)
                continue
            if not _user32.IsDialogMessageW(hwnd, ctypes.byref(msg)):
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        _active_hwnd["value"] = None
        _preview_frame["rect"] = None
        if owner:
            _user32.EnableWindow(owner, True)
            _force_foreground(owner)
        if bitmap_handle:
            _gdi32.DeleteObject(bitmap_handle)
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    return bool(_pending_result["value"])
