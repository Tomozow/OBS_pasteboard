"""ホットキー・ソース名の定数。

OBS スクリプト本体と E2E テストの両方から参照される
(obs_pasteboard.py は obspython に依存するため OBS 外から import できず、
定数だけをここに切り出して重複定義による乖離を防ぐ)。
"""

HOTKEY_PASTE = "obs_pasteboard.paste"
HOTKEY_REMOVE_LAST = "obs_pasteboard.remove_last"
SOURCE_NAME_IMAGE = "ペースト画像"
SOURCE_NAME_TEXT = "ペーストテキスト"
