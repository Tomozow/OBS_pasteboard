"""純 Python の PNG エンコーダ / デコーダ(zlib + struct のみ、依存ゼロ)。

エンコードは RGBA 8bit 固定・フィルタ None で高速化(OBS が一度読むだけの
一時ファイル用途のため)。デコードは確認ダイアログのプレビュー生成専用の
簡易実装で、8bit・非インターレースのみ対応する(クリップボード PNG は
実務上ほぼ全てこの条件を満たす)。
"""

import struct
import zlib

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_COLOR_TYPE_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class PngError(ValueError):
    """PNG が解釈できない(非対応の bit depth・インターレース等)ことを示す。"""


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(payload, crc)
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def encode_png(rgba: bytes | bytearray | memoryview, width: int, height: int) -> bytes:
    """トップダウンの RGBA バイト列を PNG バイナリへ変換する。"""
    expected = width * height * 4
    if len(rgba) != expected:
        raise ValueError(f"RGBA データ長が不正です: {len(rgba)} != {expected} ({width}x{height})")
    if width <= 0 or height <= 0:
        raise ValueError(f"画像サイズが不正です: {width}x{height}")

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8bit RGBA

    # 事前確保したバッファへ 1 回だけコピーする(フィルタバイト 0x00 は
    # ゼロ初期化で自動的に入る)。圧縮レベルは 1(Z_BEST_SPEED): この用途は
    # OBS が一度読むだけの一時ファイルで、レベル 6 との実測サイズ差は
    # 約 1.5% に対し、高エントロピー画像で 25〜35% 速い。
    row_bytes = width * 4
    stride = row_bytes + 1
    scanlines = bytearray(stride * height)
    view_out = memoryview(scanlines)
    view_in = memoryview(rgba)
    for y in range(height):
        offset = y * stride
        view_out[offset + 1 : offset + stride] = view_in[y * row_bytes : (y + 1) * row_bytes]
    idat = zlib.compress(scanlines, 1)

    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter_scanlines(raw: bytes, width: int, height: int, channels: int) -> bytearray:
    row_bytes = width * channels
    stride = row_bytes + 1
    recon = bytearray(row_bytes * height)
    prior = bytes(row_bytes)
    for y in range(height):
        filter_type = raw[y * stride]
        row = raw[y * stride + 1 : y * stride + stride]
        out_row = memoryview(recon)[y * row_bytes : (y + 1) * row_bytes]
        if filter_type == 0:
            out_row[:] = row
        elif filter_type == 1:
            for x in range(row_bytes):
                left = out_row[x - channels] if x >= channels else 0
                out_row[x] = (row[x] + left) & 0xFF
        elif filter_type == 2:
            for x in range(row_bytes):
                out_row[x] = (row[x] + prior[x]) & 0xFF
        elif filter_type == 3:
            for x in range(row_bytes):
                left = out_row[x - channels] if x >= channels else 0
                out_row[x] = (row[x] + ((left + prior[x]) >> 1)) & 0xFF
        elif filter_type == 4:
            for x in range(row_bytes):
                left = out_row[x - channels] if x >= channels else 0
                up_left = prior[x - channels] if x >= channels else 0
                out_row[x] = (row[x] + _paeth_predictor(left, prior[x], up_left)) & 0xFF
        else:
            raise PngError(f"非対応のフィルタタイプです: {filter_type}")
        prior = bytes(out_row)
    return recon


def _expand_to_rgba(
    recon: bytes, width: int, height: int, color_type: int, palette: bytes | None
) -> bytes:
    pixel_count = width * height
    out = bytearray(pixel_count * 4)
    if color_type == 6:  # RGBA
        out[:] = recon
    elif color_type == 2:  # RGB
        out[0::4] = recon[0::3]
        out[1::4] = recon[1::3]
        out[2::4] = recon[2::3]
        out[3::4] = b"\xff" * pixel_count
    elif color_type == 0:  # グレースケール
        out[0::4] = recon
        out[1::4] = recon
        out[2::4] = recon
        out[3::4] = b"\xff" * pixel_count
    elif color_type == 4:  # グレースケール + アルファ
        out[0::4] = recon[0::2]
        out[1::4] = recon[0::2]
        out[2::4] = recon[0::2]
        out[3::4] = recon[1::2]
    elif color_type == 3:  # パレット
        if palette is None:
            raise PngError("パレット PNG に PLTE チャンクがありません")
        for i, index in enumerate(recon):
            r, g, b = palette[index * 3 : index * 3 + 3]
            out[i * 4 : i * 4 + 4] = bytes((r, g, b, 255))
    return bytes(out)


def decode_png(data: bytes) -> tuple[bytes, int, int]:
    """PNG(8bit・非インターレース)を RGBA へ変換する(プレビュー生成専用の簡易実装)。"""
    if data[:8] != _PNG_SIGNATURE:
        raise PngError("PNG シグネチャが不正です")

    width = height = bit_depth = color_type = interlace = None
    palette: bytes | None = None
    idat = bytearray()
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif chunk_type == b"PLTE":
            palette = payload
        elif chunk_type == b"IDAT":
            idat += payload
        elif chunk_type == b"IEND":
            break
        offset += 8 + length + 4  # チャンクデータ + CRC

    if width is None:
        raise PngError("IHDR チャンクがありません")
    if bit_depth != 8:
        raise PngError(f"非対応のビット深度です: {bit_depth}")
    if interlace != 0:
        raise PngError("インターレース PNG には対応していません")
    if color_type not in _COLOR_TYPE_CHANNELS:
        raise PngError(f"非対応のカラータイプです: {color_type}")

    channels = _COLOR_TYPE_CHANNELS[color_type]
    raw = zlib.decompress(bytes(idat))
    recon = _unfilter_scanlines(raw, width, height, channels)
    rgba = _expand_to_rgba(recon, width, height, color_type, palette)
    return rgba, width, height
