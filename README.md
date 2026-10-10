# OBS Pasteboard

[English](README.en.md) | 日本語

クリップボードの**画像・テキストを OBS Studio のシーンへ直接ペースト**できる Python スクリプト拡張です。

- スクリーンショット(`Win+Shift+S` など)やブラウザでコピーした画像 → **PNG 保存 + 画像ソース**として現在のシーン中央に配置
- エクスプローラーでコピーした画像ファイル → そのまま画像ソースに
- コピーしたテキスト → **テキスト (GDI+) ソース**として配置
- ペースト前に**画像プレビュー付きの確認ダイアログ**を表示(OBS の Yami テーマに合わせたダークデザイン。設定でスキップ可能)
- 既定ホットキー: **Ctrl+Shift+V = ペースト**、**Ctrl+Shift+Z = 最後のペーストを削除**(初回起動時に自動割り当て、変更・解除可能)

ランタイム依存ゼロ(OBS 内蔵の Python 標準ライブラリのみで動作)。確認ダイアログも
Qt バインディングを使わず素の Win32 API で実装しており、OBS 本体の Qt ランタイムと
バージョンが異なっていてもクラッシュしない。

## 動作環境

- Windows 10/11
- OBS Studio 28 以降(32.1.0 で E2E テスト済み)
- OBS のスクリプト用 Python(**3.10 以降、64bit**。[ツール] → [スクリプト] → [Python 設定] で
  Python のインストール先フォルダを指定)

## インストール

1. [Releases](../../releases/latest) から `obs_pasteboard-vX.Y.Z.zip` をダウンロードし、任意の場所に展開する
   (`obs_pasteboard.py` と `pasteboard_core/` は同じ階層に置いたままにすること)
2. OBS の **[ツール] → [スクリプト]** で `obs_pasteboard.py` を追加
3. 初回起動時に既定ホットキーが自動で割り当てられる:
   - `Ctrl+Shift+V` = クリップボードをペースト
   - `Ctrl+Shift+Z` = 最後のペーストを削除

   いずれも **[設定] → [ホットキー]** から変更・解除できる。一度解除すれば
   再読み込みしても既定値は再適用されない

## スクリプト設定

| 設定 | 既定値 | 説明 |
|---|---|---|
| 画像の保存先 | `~/Pictures/OBS_Pasteboard` | クリップボード画像の PNG 保存先 |
| 毎回同じソースを上書きする | オフ | オンにすると「ペースト画像」「ペーストテキスト」を使い回す |
| 画像とテキスト両方がある場合は画像を優先 | オン | Excel など両方をコピーするアプリ向けの優先制御 |
| 貼り付け前に確認ダイアログを表示する | オン | OFF にすると即座にペースト(配信中の高速操作向け) |
| 配置サイズ(キャンバス比 %) | 60 | ペーストしたソースを収める枠のサイズ |

スクリプト画面の「今すぐペースト」ボタンでホットキーなしでも動作確認できます。

## 確認ダイアログ

既定でペースト実行前に OBS 本体のウィンドウにモーダルな確認ダイアログを表示する。
配色は OBS の既定テーマ Yami に合わせたダークスタイル(ダークタイトルバー、
Yu Gothic UI フォント、角丸ボタン、OK ボタンはアクセントブルー)。

- 画像: 種類・サイズ・容量に加え、**サムネイルプレビュー**(最大 480x360、アスペクト比維持)を枠線付きで表示
- テキスト: 内容を最大 300 文字までプレビュー表示
- `OK (ペースト)` / `キャンセル` の 2 択(Enter で OK、Esc でキャンセル)

「貼り付け前に確認ダイアログを表示する」をオフにすれば従来どおり即座にペーストされる。

## 対応クリップボード形式

優先順: `PNG`(ブラウザ系)→ `CF_DIBV5` → `CF_DIB` → `CF_HDROP`(画像ファイル)→ `CF_UNICODETEXT`

32/24bpp DIB はスライス代入(C 速度)で PNG 化(4K 実測 約 0.2〜1 秒)。
16bpp・1/4/8bpp パレットは per-pixel 変換が大画像で秒単位かかるため、
標準 BMP としてそのまま保存して OBS に直接読ませる(変換コストほぼゼロ)。
解釈できない特殊形式(RLE 圧縮等)も BMP フォールバックで保存する。

## 開発

```bash
uv sync                       # 依存のセットアップ
uv run pytest                 # ユニットテスト(82 件、OBS 不要)
uv run pytest -m e2e tests/e2e  # E2E テスト(実 OBS を自動起動、OBS は停止しておくこと)
uv run ruff check .           # リント
```

`v*` タグを push すると GitHub Actions がテストを通したうえで配布用 zip を Releases に添付する
(`.github/workflows/ci.yml`)。開発環境は Python 3.12 だが、OBS で読み込める最低バージョンの
3.10 でもユニットテストを実行している。

### 構成

```
obs_pasteboard.py      # OBS スクリプト本体(obspython アダプタ)
pasteboard_core/       # OBS 非依存のコアロジック(依存ゼロ)
  clipboard_win.py     #   Win32 クリップボード読み書き(ctypes)
  dib.py               #   DIB → RGBA 変換(32/24bpp はスライス代入で高速)
  png_codec.py         #   純 Python PNG エンコーダ / デコーダ(zlib)
  imaging.py           #   プレビューサムネイル生成(最近傍縮小)
  content.py           #   ペースト対象の選択ロジック
  confirm_dialog.py    #   確認ダイアログ(Win32 ネイティブ、Qt 不使用)
tests/unit/            # コア + fake obspython によるスクリプトロジックテスト
tests/e2e/             # 実 OBS + obs-websocket による E2E テスト
```

### 確認ダイアログを Qt ではなく Win32 API で実装している理由

OBS 本体は Qt6 を内蔵しているが、pip の PySide6 が同梱する Qt6 とは
**マイナーバージョンが異なる**(このプロジェクトの開発環境では OBS が Qt 6.8.3、
PySide6 が Qt 6.10.0)。`obs_frontend_get_main_window()` の生ポインタを
別ビルドの Qt にキャストして扱うと ABI 不一致でクラッシュしうるため、
配信ツールのプロセスにそのリスクを持ち込まないよう、あえて素の Win32 API
(`RegisterClassExW` / `CreateWindowExW` + 独自メッセージループ)のみで実装した。
画像プレビューは RGBA → 一時 BMP ファイル → `LoadImageW` という、
既存の `dib.py` の実装(単体テスト済み)を再利用する経路で描画している。

### E2E テストの仕組み

1. **OBS が起動していないこと**を確認(起動中は安全のためスキップ。ユーザーのシーンコレクションには一切触れない)
2. E2E 専用シーンコレクション(`OBS_Pasteboard_E2E` / 確認ダイアログ検証用に
   `OBS_Pasteboard_E2E_Confirm` を別途使用)の JSON に本スクリプトを登録
3. `obs64 --collection <名前>` で専用インスタンスを起動(**必ず `bin/64bit` を
   作業ディレクトリにすること**。さもないと `locale/en-US.ini` が見つからず
   起動時エラーになる)
4. obs-websocket 経由で `TriggerHotkeyByName` → ソース生成を確認し、
   `GetSourceScreenshot` で**レンダリング結果をピクセル単位で検証**。
   確認ダイアログの検証では `FindWindowW` でネイティブウィンドウを見つけ、
   `SendMessageW(..., BM_CLICK, ...)` で OK/キャンセルボタンを実際にクリックする
5. OBS を正常終了させ、`user.ini` のコレクション参照を復元

前提: obs-websocket が有効であること([ツール] → [WebSocket サーバー設定])。

## 制限事項

- Windows 専用(クリップボード API・text_gdiplus に依存)
- RLE / JPEG / PNG 圧縮の DIB は RGBA 変換非対応(BMP フォールバックで保存のみ)
- テキストは 5,000 文字で切り詰め

## ライセンス

[MIT](LICENSE)
