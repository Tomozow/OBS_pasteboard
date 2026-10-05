"""プレビューサムネイル生成用の純ロジック(依存ゼロ)。"""


def fit_within(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    """アスペクト比を保ったまま max_width x max_height に収まるサイズを返す(拡大はしない)。"""
    if width <= max_width and height <= max_height:
        return width, height
    scale = min(max_width / width, max_height / height)
    new_width = max(1, round(width * scale))
    new_height = max(1, round(height * scale))
    return new_width, new_height


def resize_rgba_nearest(
    rgba: bytes, width: int, height: int, new_width: int, new_height: int
) -> bytes:
    """最近傍補間で RGBA 画像を縮小する(プレビューサムネイル用)。"""
    if (new_width, new_height) == (width, height):
        return rgba

    x_ratio = width / new_width
    y_ratio = height / new_height
    x_map = [min(width - 1, int(x * x_ratio)) for x in range(new_width)]

    view = memoryview(rgba)
    out = bytearray(new_width * new_height * 4)
    out_view = memoryview(out)
    row_bytes = width * 4
    dst_row_bytes = new_width * 4
    for dst_y in range(new_height):
        src_y = min(height - 1, int(dst_y * y_ratio))
        src_row = view[src_y * row_bytes : (src_y + 1) * row_bytes]
        dst_row = out_view[dst_y * dst_row_bytes : (dst_y + 1) * dst_row_bytes]
        for dst_x, src_x in enumerate(x_map):
            dst_row[dst_x * 4 : dst_x * 4 + 4] = src_row[src_x * 4 : src_x * 4 + 4]
    return bytes(out)
