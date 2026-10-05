"""OBS Pasteboard のコアロジック(OBS 非依存・外部依存ゼロ)。

OBS の Python 環境には pip パッケージが入っていない前提で、
標準ライブラリ(ctypes / zlib / struct)のみで実装している。
"""

from pasteboard_core.clipboard_win import (
    ClipboardError,
    ClipboardImage,
    ClipboardSnapshot,
    read_clipboard,
    set_clipboard_image,
    set_clipboard_text,
)
from pasteboard_core.dib import DibError, dib_to_rgba, rgba_to_dib, wrap_dib_as_bmp
from pasteboard_core.png_codec import encode_png

__all__ = [
    "ClipboardError",
    "ClipboardImage",
    "ClipboardSnapshot",
    "DibError",
    "dib_to_rgba",
    "encode_png",
    "read_clipboard",
    "rgba_to_dib",
    "set_clipboard_image",
    "set_clipboard_text",
    "wrap_dib_as_bmp",
]
