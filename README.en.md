# OBS Pasteboard

English | [日本語](README.md)

An OBS Studio Python script that **pastes images and text from the clipboard straight into the current scene**.

- Screenshots (`Win+Shift+S`, etc.) and images copied from a browser → saved as **PNG and added as an Image source**, centered in the current scene
- Image files copied in Explorer → added as an Image source as-is
- Copied text → added as a **Text (GDI+) source**
- Optional **confirmation dialog with an image preview** before pasting (dark design matching OBS's Yami theme; can be turned off)
- Default hotkeys: **Ctrl+Shift+V = paste**, **Ctrl+Shift+Z = remove the last paste** (assigned automatically on first load; can be changed or cleared)

Zero runtime dependencies — it runs on the Python standard library only. The confirmation dialog
is plain Win32 API rather than a Qt binding, so it cannot crash OBS through a Qt version mismatch.

> The script's settings and dialog text are currently in Japanese.

## Requirements

- Windows 10/11
- OBS Studio 28 or later (E2E-tested on 32.1.0)
- Python **3.10 or later, 64-bit**, set in OBS under **Tools → Scripts → Python Settings**
  (point it at the Python install folder)

## Installation

1. Download `obs_pasteboard-vX.Y.Z.zip` from [Releases](../../releases/latest) and extract it anywhere
   (keep `obs_pasteboard.py` and `pasteboard_core/` side by side)
2. In OBS, open **Tools → Scripts** and add `obs_pasteboard.py`
3. The default hotkeys are assigned on first load:
   - `Ctrl+Shift+V` = paste the clipboard
   - `Ctrl+Shift+Z` = remove the last paste

   Change or clear them under **Settings → Hotkeys**. Once cleared, they are not re-applied on reload.

## Script settings

| Setting | Default | Description |
|---|---|---|
| Image save folder | `~/Pictures/OBS_Pasteboard` | Where clipboard images are saved as PNG |
| Reuse the same source every time | Off | When on, reuses the "ペースト画像" / "ペーストテキスト" sources |
| Prefer image when both image and text exist | On | For apps such as Excel that copy both |
| Show a confirmation dialog before pasting | On | Turn off to paste instantly (for fast use while live) |
| Placement size (% of canvas) | 60 | Size of the box the pasted source is fitted into |

The **Paste now** button in the script panel lets you test without a hotkey.

## Confirmation dialog

By default a modal dialog is shown over the OBS main window before pasting.

- Image: type, dimensions and size, plus a **thumbnail preview** (up to 480×360, aspect ratio kept)
- Text: the first 300 characters
- OK (paste) / Cancel — Enter confirms, Esc cancels

## Supported clipboard formats

Priority: `PNG` (browsers) → `CF_DIBV5` → `CF_DIB` → `CF_HDROP` (image files) → `CF_UNICODETEXT`

32/24bpp DIBs are converted to PNG with slice assignment (about 0.2–1 s for 4K).
16bpp and 1/4/8bpp palette DIBs would be slow to convert per pixel, so they are saved as
a standard BMP that OBS reads directly. Unrecognised formats (RLE, etc.) also fall back to BMP.

## Development

```bash
uv sync                         # set up dependencies
uv run pytest                   # unit tests (no OBS needed)
uv run pytest -m e2e tests/e2e  # E2E tests (launches a real OBS; close OBS first)
uv run ruff check .             # lint
```

Pushing a `v*` tag makes GitHub Actions run the tests and attach the release zip to Releases
(`.github/workflows/ci.yml`). Development uses Python 3.12, but unit tests also run on 3.10,
the minimum version OBS can load the script with.

### Layout

```
obs_pasteboard.py      # OBS script (obspython adapter)
pasteboard_core/       # OBS-independent core logic (no dependencies)
  clipboard_win.py     #   Win32 clipboard access (ctypes)
  dib.py               #   DIB → RGBA conversion
  png_codec.py         #   pure-Python PNG encoder/decoder (zlib)
  imaging.py           #   preview thumbnails (nearest-neighbour)
  content.py           #   choosing what to paste
  confirm_dialog.py    #   confirmation dialog (native Win32, no Qt)
tests/unit/            # core + script logic tests with a fake obspython
tests/e2e/             # E2E tests against a real OBS via obs-websocket
```

Design notes (why Win32 instead of Qt, how the E2E harness isolates your OBS profile) are in
[README.ja.md](README.ja.md).

## Limitations

- Windows only (relies on the Win32 clipboard API and text_gdiplus)
- RLE / JPEG / PNG-compressed DIBs are not converted to RGBA (saved as BMP only)
- Text is truncated at 5,000 characters

## License

[MIT](LICENSE)
