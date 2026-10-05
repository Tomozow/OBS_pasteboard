from pasteboard_core.imaging import fit_within, resize_rgba_nearest


def test_fit_within_no_upscale_when_already_small():
    assert fit_within(100, 50, 480, 360) == (100, 50)


def test_fit_within_scales_down_preserving_aspect_ratio():
    width, height = fit_within(3840, 2160, 480, 360)
    assert width <= 480
    assert height <= 360
    assert round(width / height, 2) == round(3840 / 2160, 2)


def test_fit_within_tall_image():
    width, height = fit_within(200, 4000, 480, 360)
    assert height == 360
    assert width <= 480


def test_resize_rgba_nearest_noop_when_same_size():
    rgba = bytes(range(16))
    assert resize_rgba_nearest(rgba, 2, 2, 2, 2) == rgba


def test_resize_rgba_nearest_output_size():
    rgba = bytes([i % 256 for i in range(40 * 30 * 4)])
    resized = resize_rgba_nearest(rgba, 40, 30, 10, 6)
    assert len(resized) == 10 * 6 * 4


def test_resize_rgba_nearest_preserves_corner_colors():
    # 2x2 の四隅色付き画像を 4x4 に拡大しても、四隅の色は保たれる
    red, green, blue, white = (255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255,) * 4
    rgba = bytes(red) + bytes(green) + bytes(blue) + bytes(white)
    resized = resize_rgba_nearest(rgba, 2, 2, 4, 4)
    assert resized[0:4] == bytes(red)  # 左上
    assert resized[-4:] == bytes(white)  # 右下
