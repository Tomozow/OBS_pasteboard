"""Win32 クリップボードの読み書き(ctypes のみ、外部依存なし)。

読み取りは「スナップショット」方式: 画像・テキスト・ファイルパスを
一度のオープンでまとめて取得し、どれを使うかは呼び出し側が決める。
クリップボードは他プロセスと競合するため、オープンはリトライ付き。
"""

import ctypes
import time
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass

from pasteboard_core.dib import rgba_to_dib
from pasteboard_core.png_codec import encode_png

CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
CF_DIBV5 = 17
_GMEM_MOVEABLE = 0x0002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# 64bit で HANDLE が切り詰められないよう、使用する API はすべて型を宣言する
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
_shell32.DragQueryFileW.restype = wintypes.UINT


@dataclass(frozen=True)
class ClipboardImage:
    kind: str  # "png" | "dibv5" | "dib"
    data: bytes


@dataclass(frozen=True)
class ClipboardSnapshot:
    image: ClipboardImage | None
    text: str | None
    files: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return self.image is None and self.text is None and not self.files


class ClipboardError(OSError):
    """クリップボードが開けない・読めないことを示す。"""


# "PNG" 系の登録クリップボードフォーマット ID(ブラウザ等が使用)。
# 登録 ID はセッション中不変なので、クリップボードのオープン時間を
# 延ばさないようモジュールロード時に一度だけ解決する。
_PNG_FORMAT_IDS = tuple(
    format_id
    for format_id in (
        _user32.RegisterClipboardFormatW("PNG"),
        _user32.RegisterClipboardFormatW("image/png"),
    )
    if format_id
)


@contextmanager
def _open_clipboard(retries: int = 25, delay: float = 0.02):
    """クリップボードを開く。他プロセス使用中はリトライする。"""
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            break
        time.sleep(delay)
    else:
        raise ClipboardError("クリップボードを開けませんでした(他プロセスが使用中)")
    try:
        yield
    finally:
        _user32.CloseClipboard()


def _read_format_bytes(fmt: int) -> bytes | None:
    handle = _user32.GetClipboardData(fmt)
    if not handle:
        return None
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        size = _kernel32.GlobalSize(handle)
        return ctypes.string_at(ptr, size) if size else None
    finally:
        _kernel32.GlobalUnlock(handle)


def _read_text() -> str | None:
    handle = _user32.GetClipboardData(CF_UNICODETEXT)
    if not handle:
        return None
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        # NUL 終端に依存せず GlobalSize で読み取り範囲を制限する
        # (終端の無い壊れたデータでのバッファ外読み出しを防ぐ)
        max_chars = _kernel32.GlobalSize(handle) // 2
        if max_chars == 0:
            return None
        return ctypes.wstring_at(ptr, max_chars).split("\0", 1)[0]
    finally:
        _kernel32.GlobalUnlock(handle)


def _read_file_list() -> tuple[str, ...]:
    handle = _user32.GetClipboardData(CF_HDROP)
    if not handle:
        return ()
    count = _shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
    paths = []
    for i in range(count):
        length = _shell32.DragQueryFileW(handle, i, None, 0)
        buffer = ctypes.create_unicode_buffer(length + 1)
        if _shell32.DragQueryFileW(handle, i, buffer, length + 1):
            paths.append(buffer.value)
    return tuple(paths)


def read_clipboard() -> ClipboardSnapshot:
    """クリップボードの内容を一括で取得する。

    画像は PNG(ブラウザ系)→ CF_DIBV5(アルファ保持)→ CF_DIB の
    優先順で最初に取得できたものを返す。
    """
    with _open_clipboard():
        image = None
        for fmt in _PNG_FORMAT_IDS:
            data = _read_format_bytes(fmt)
            if data:
                image = ClipboardImage("png", data)
                break
        if image is None:
            for fmt, kind in ((CF_DIBV5, "dibv5"), (CF_DIB, "dib")):
                if _user32.IsClipboardFormatAvailable(fmt):
                    data = _read_format_bytes(fmt)
                    if data:
                        image = ClipboardImage(kind, data)
                        break
        text = _read_text()
        files = _read_file_list()
    return ClipboardSnapshot(image=image, text=text, files=files)


def _alloc_global(data: bytes) -> int:
    handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
    if not handle:
        raise ClipboardError("GlobalAlloc に失敗しました")
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        _kernel32.GlobalFree(handle)
        raise ClipboardError("GlobalLock に失敗しました")
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        _kernel32.GlobalUnlock(handle)
    return handle


def _set_format(fmt: int, data: bytes) -> None:
    handle = _alloc_global(data)
    if not _user32.SetClipboardData(fmt, handle):
        _kernel32.GlobalFree(handle)  # 失敗時のみ所有権が残る
        raise ClipboardError(f"SetClipboardData に失敗しました (format={fmt})")


def set_clipboard_text(text: str) -> None:
    """テキストをクリップボードへ書き込む(テスト用途)。"""
    payload = (text + "\0").encode("utf-16-le")
    with _open_clipboard():
        _user32.EmptyClipboard()
        _set_format(CF_UNICODETEXT, payload)


def set_clipboard_image(rgba: bytes, width: int, height: int) -> None:
    """RGBA 画像をクリップボードへ書き込む(テスト用途)。

    CF_DIB と "PNG" の両フォーマットを載せ、実アプリのコピー動作を再現する。
    """
    dib = rgba_to_dib(rgba, width, height)
    png = encode_png(rgba, width, height)
    with _open_clipboard():
        _user32.EmptyClipboard()
        _set_format(CF_DIB, dib)
        if _PNG_FORMAT_IDS:
            _set_format(_PNG_FORMAT_IDS[0], png)
