"""Windows DIB(CF_DIB / CF_DIBV5)と RGBA の相互変換。

クリップボード画像の実態は BITMAPINFOHEADER(V5)+ ピクセルデータの DIB。
一般的な 32bpp / 24bpp はスライス代入(C 速度)で変換し、
16bpp / パレット形式は汎用パスで対応する。RLE / JPEG / PNG 圧縮 DIB は
非対応として DibError を投げ、呼び出し側が BMP ラップへフォールバックする。
"""

import struct

BI_RGB = 0
BI_BITFIELDS = 3

# 幅・高さの積の上限(1 億ピクセル ≒ 400MB RGBA)。壊れたヘッダ対策の安全弁。
_MAX_PIXELS = 100_000_000

# per-pixel 変換(16bpp の特殊マスク等)の上限。純 Python では約 0.4 秒/百万px
# かかるため、これを超える場合は DibError とし呼び出し側で BMP へフォールバックする。
_SLOW_CONVERT_MAX_PIXELS = 4_000_000


class DibError(ValueError):
    """DIB データが解釈できない(壊れている・非対応形式)ことを示す。"""


def _header_fields(dib: bytes | memoryview) -> tuple[int, int, int, int, int, int]:
    if len(dib) < 40:
        raise DibError(f"DIB ヘッダが短すぎます: {len(dib)} バイト")
    header_size, width, height = struct.unpack_from("<Iii", dib, 0)
    bpp, compression = struct.unpack_from("<HI", dib, 14)
    (clr_used,) = struct.unpack_from("<I", dib, 32)
    if header_size < 40 or header_size > len(dib):
        raise DibError(f"非対応の DIB ヘッダサイズです: {header_size}")
    return header_size, width, height, bpp, compression, clr_used


def _parse_layout(dib: bytes | memoryview) -> tuple[int, int, int, int, int, int, bool, int]:
    """(pixel_offset, palette_offset, width, height, bpp, compression, top_down, stride)"""
    header_size, width, height, bpp, compression, clr_used = _header_fields(dib)

    top_down = height < 0
    height = abs(height)
    if width <= 0 or height == 0:
        raise DibError(f"画像サイズが不正です: {width}x{height}")
    if width * height > _MAX_PIXELS:
        raise DibError(f"画像が大きすぎます: {width}x{height}")
    if compression not in (BI_RGB, BI_BITFIELDS):
        raise DibError(f"非対応の圧縮形式です: compression={compression}")

    # BITMAPINFOHEADER(40 バイト)+ BI_BITFIELDS の場合のみ、
    # ヘッダ直後に 3 つのカラーマスクが並ぶ。V4/V5 はヘッダ内に含まれる。
    extra_mask_bytes = 12 if (header_size == 40 and compression == BI_BITFIELDS) else 0
    palette_entries = _palette_entry_count(bpp, clr_used)
    palette_offset = header_size + extra_mask_bytes
    pixel_offset = palette_offset + palette_entries * 4
    stride = (width * bpp + 31) // 32 * 4

    if pixel_offset + stride * height > len(dib):
        raise DibError(
            f"DIB データが不足しています: 必要 {pixel_offset + stride * height}, 実際 {len(dib)}"
        )
    return pixel_offset, palette_offset, width, height, bpp, compression, top_down, stride


def _palette_entry_count(bpp: int, clr_used: int) -> int:
    """パレットのエントリ数。>8bpp でも「最適化パレット」が置かれることがある。"""
    if bpp <= 8:
        return clr_used if clr_used else (1 << bpp)
    return clr_used


def _color_masks(
    dib: bytes | memoryview, header_size: int, bpp: int, compression: int
) -> tuple[int, int, int, int]:
    """(red, green, blue, alpha) のビットマスクを返す。"""
    if compression == BI_BITFIELDS:
        red, green, blue = struct.unpack_from("<III", dib, 40)
        alpha = struct.unpack_from("<I", dib, 52)[0] if header_size >= 56 else 0
        return red, green, blue, alpha
    if bpp == 16:  # BI_RGB の 16bpp は 555 固定
        return 0x7C00, 0x03E0, 0x001F, 0
    if bpp == 32:  # BI_RGB の 32bpp は BGRX 固定
        return 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000
    raise DibError(f"マスクを決定できません: bpp={bpp}, compression={compression}")


def _mask_params(mask: int) -> tuple[int, int]:
    """マスクから (右シフト量, 最大値) を求める。"""
    if mask == 0:
        return 0, 0
    shift = (mask & -mask).bit_length() - 1
    return shift, mask >> shift


def _join_rows(
    pixels: memoryview, height: int, stride: int, row_bytes: int, top_down: bool
) -> bytes:
    """パディングを除去し、トップダウン順に連結したピクセル列を返す。"""
    if top_down and stride == row_bytes:
        return bytes(pixels[: row_bytes * height])  # 既に連続領域なので一括コピー
    order = range(height) if top_down else range(height - 1, -1, -1)
    return b"".join(pixels[y * stride : y * stride + row_bytes] for y in order)


def dib_to_rgba(dib: bytes) -> tuple[bytes, int, int]:
    """DIB バイト列をトップダウン RGBA に変換し (rgba, width, height) を返す。"""
    pixel_offset, palette_offset, width, height, bpp, compression, top_down, stride = (
        _parse_layout(dib)
    )
    header_size = struct.unpack_from("<I", dib, 0)[0]

    view = memoryview(dib)
    pixels = view[pixel_offset:]
    pixel_count = width * height

    if bpp == 32:
        red, green, blue, _alpha = _color_masks(dib, header_size, bpp, compression)
        if (red, green, blue) == (0x00FF0000, 0x0000FF00, 0x000000FF):
            data = _join_rows(pixels, height, stride, width * 4, top_down)
            out = bytearray(pixel_count * 4)
            out[0::4] = data[2::4]
            out[1::4] = data[1::4]
            out[2::4] = data[0::4]
            alpha_bytes = data[3::4]
            # アルファ全 0 の DIB はアルファ未使用とみなして不透明にする(慣例)
            if alpha_bytes.count(0) == pixel_count:
                out[3::4] = b"\xff" * pixel_count
            else:
                out[3::4] = alpha_bytes
            return bytes(out), width, height
        return _generic_mask_convert(pixels, dib, header_size, bpp, compression,
                                     width, height, stride, top_down)

    if bpp == 24:
        data = _join_rows(pixels, height, stride, width * 3, top_down)
        out = bytearray(pixel_count * 4)
        out[0::4] = data[2::3]
        out[1::4] = data[1::3]
        out[2::4] = data[0::3]
        out[3::4] = b"\xff" * pixel_count
        return bytes(out), width, height

    if bpp == 16:
        return _generic_mask_convert(pixels, dib, header_size, bpp, compression,
                                     width, height, stride, top_down)

    if bpp in (1, 4, 8):
        return _palette_convert(view, palette_offset, pixels, width, height,
                                bpp, stride, top_down)

    raise DibError(f"非対応のビット深度です: {bpp}bpp")


def _generic_mask_convert(
    pixels: memoryview,
    dib: bytes | memoryview,
    header_size: int,
    bpp: int,
    compression: int,
    width: int,
    height: int,
    stride: int,
    top_down: bool,
) -> tuple[bytes, int, int]:
    """任意ビットマスクの 16/32bpp を per-pixel で変換する(稀な形式向け)。"""
    if width * height > _SLOW_CONVERT_MAX_PIXELS:
        raise DibError(f"per-pixel 変換には大きすぎます: {width}x{height} ({bpp}bpp)")
    red, green, blue, alpha = _color_masks(dib, header_size, bpp, compression)
    if red == 0 or green == 0 or blue == 0:
        raise DibError(f"カラーマスクが不正です: R={red:#x} G={green:#x} B={blue:#x}")
    r_shift, r_max = _mask_params(red)
    g_shift, g_max = _mask_params(green)
    b_shift, b_max = _mask_params(blue)
    a_shift, a_max = _mask_params(alpha)

    bytes_per_pixel = bpp // 8
    out = bytearray(width * height * 4)
    order = range(height) if top_down else range(height - 1, -1, -1)
    pos = 0
    for y in order:
        row = pixels[y * stride : y * stride + width * bytes_per_pixel]
        for x in range(width):
            value = int.from_bytes(row[x * bytes_per_pixel : (x + 1) * bytes_per_pixel], "little")
            out[pos] = ((value >> r_shift) & r_max) * 255 // r_max
            out[pos + 1] = ((value >> g_shift) & g_max) * 255 // g_max
            out[pos + 2] = ((value >> b_shift) & b_max) * 255 // b_max
            out[pos + 3] = ((value >> a_shift) & a_max) * 255 // a_max if a_max else 255
            pos += 4
    return bytes(out), width, height


def _palette_convert(
    view: memoryview,
    palette_offset: int,
    pixels: memoryview,
    width: int,
    height: int,
    bpp: int,
    stride: int,
    top_down: bool,
) -> tuple[bytes, int, int]:
    """1/4/8bpp のパレット画像を変換する。"""
    if width * height > _SLOW_CONVERT_MAX_PIXELS:
        raise DibError(f"per-pixel 変換には大きすぎます: {width}x{height} ({bpp}bpp)")
    entry_count = min((len(view) - palette_offset) // 4, 256)
    palette = []
    for i in range(entry_count):
        b, g, r = view[palette_offset + i * 4 : palette_offset + i * 4 + 3]
        palette.append(bytes((r, g, b, 255)))
    if not palette:
        raise DibError("パレットがありません")
    fallback = palette[0]

    rows: list[bytes] = []
    order = range(height) if top_down else range(height - 1, -1, -1)
    for y in order:
        row = pixels[y * stride : (y + 1) * stride]
        if bpp == 8:
            indices = row[:width]
        elif bpp == 4:
            indices = bytearray()
            for byte in row[: (width + 1) // 2]:
                indices.append(byte >> 4)
                indices.append(byte & 0x0F)
            indices = indices[:width]
        else:  # 1bpp
            indices = bytearray()
            for byte in row[: (width + 7) // 8]:
                for bit in range(7, -1, -1):
                    indices.append((byte >> bit) & 1)
            indices = indices[:width]
        rows.append(b"".join(palette[i] if i < entry_count else fallback for i in indices))
    return b"".join(rows), width, height


def dib_bits_per_pixel(dib: bytes) -> int:
    """DIB のビット深度を返す(ヘッダのみ参照するため高速)。"""
    return _header_fields(dib)[3]


def wrap_dib_as_bmp(dib: bytes) -> bytes:
    """DIB に BITMAPFILEHEADER を付けて BMP ファイルにする。

    dib_to_rgba() が非対応の形式(RLE 圧縮など)でも、BMP としてなら
    OBS の画像ローダが読める可能性があるためのフォールバック。
    """
    header_size, _, _, bpp, compression, clr_used = _header_fields(dib)
    extra_mask_bytes = 12 if (header_size == 40 and compression == BI_BITFIELDS) else 0
    palette_entries = _palette_entry_count(bpp, clr_used)
    pixel_offset = 14 + header_size + extra_mask_bytes + palette_entries * 4
    file_header = struct.pack("<2sIHHI", b"BM", 14 + len(dib), 0, 0, pixel_offset)
    return file_header + dib


def rgba_to_dib(rgba: bytes, width: int, height: int) -> bytes:
    """トップダウン RGBA から CF_DIB(32bpp BI_RGB ボトムアップ)を生成する。

    テストおよび set_clipboard_image() 用。
    """
    if len(rgba) != width * height * 4:
        raise ValueError("RGBA データ長が width/height と一致しません")
    header = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 32, BI_RGB, width * height * 4, 2835, 2835, 0, 0
    )
    bgra = bytearray(len(rgba))
    bgra[0::4] = rgba[2::4]
    bgra[1::4] = rgba[1::4]
    bgra[2::4] = rgba[0::4]
    bgra[3::4] = rgba[3::4]
    row_bytes = width * 4
    bottom_up = b"".join(
        bytes(memoryview(bgra)[y * row_bytes : (y + 1) * row_bytes])
        for y in range(height - 1, -1, -1)
    )
    return header + bottom_up
