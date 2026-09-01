# NewClipboard

Windows / macOS 向けの、クリップボード履歴・定型文・連続貼り付け管理アプリです。

## 主な機能

- テキストと画像のクリップボード履歴を自動保存（重複は最新位置へ移動）
- 定型文をグループ分けし、個別のグローバルショートカットから貼り付け
- 設定・編集画面と、履歴／定型文だけを表示するコンパクトな呼び出し画面を分離
- `Ctrl+Space` またはCtrlキー2回で、どのアプリからでも呼び出し画面を表示
- 呼び出し画面の `1〜0、a〜z`、シングルクリック、Enterで元のアプリへ即時貼り付け
- FIFO / LIFOモードで、コピー順または逆順に連続貼り付け
- ストックの追加・編集・削除・貼り付け取り消し・全件連結
- 履歴の編集・複数選択連結・改行ごとの展開
- 定型文とグループの検索・編集・並び替え・CSV入出力（Clibor形式対応）
- 各行挿入、前後挿入、連番、正規表現、大小文字変換などのテキスト整形
- 整形ルールのショートカットとコピー時の自動整形
- クリップボード監視の一時停止
- 全データのZIPバックアップと復元
- OSログイン時の自動起動、常に手前、フォントサイズ、配色設定
- OS非依存のUTF-8 `config.json` を書き出し・読み込み
- 履歴はローカルの `history.json` に分離し、設定移管時に漏らさない
- タスクトレイ常駐

`primary` はWindowsではCtrl、macOSではCommandへ自動変換されます。

## 起動

Python 3.10以降を利用します。

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

macOSでは初回に「システム設定 > プライバシーとセキュリティ > アクセシビリティ」で、アプリ（またはTerminal）に入力監視の許可が必要です。

## 操作

1. 通常どおりテキストや画像をコピーすると「履歴」へ保存されます。
2. `Ctrl+Space`またはCtrlキー2回で、履歴／定型文だけの呼び出し画面を開きます。
3. 候補をシングルクリックするか、行頭のキーを押すと元のアプリへ貼り付けます。上下キーで選び、Enterでも貼り付けられます。Escで閉じます。
4. 「FIFO」または「LIFO」で開始後、複数回コピーします。通常の貼り付けキーを押すたびに順番に貼り付けます。
5. 定型文のショートカットは `primary+alt+1` のように指定できます。

履歴の削除・編集、定型文や各種設定の変更は設定・編集画面で行います。タスクトレイの「設定・編集を開く」から再表示できます。

既定のグローバルショートカットは以下です。

- 画面表示: `Ctrl+Space` またはCtrlキー2回
- FIFO切替: `primary+shift+f`
- LIFO切替: `primary+shift+l`
- 監視切替: `primary+shift+m`
- 貼り付けを1つ戻す: `primary+shift+z`

## テキスト整形

「整形」タブでルールを追加できます。正規表現の置換ではPython互換の `\1` と、Cliborで使われる `$1` の両方を後方参照として利用できます。「コピー時に自動適用」を有効にすると、外部アプリでコピーした直後に変換します。

## バックアップとCSV

- JSON書き出し: 履歴を含めず、Windows / macOSで共有する設定と定型文を保存
- 全バックアップ: 設定、テキスト履歴、画像履歴をZIPへ保存
- 定型文CSV: UTF-8 BOM付きで出力し、UTF-8またはShift_JISを取り込み
- 復元時は、現在のデータを `before-restore.zip` へ自動退避

## 設定の共有

設定タブの「設定を書き出す」で作成したJSONを別端末へコピーし、「設定を読み込む」で選択します。定型文、グループ、整形ルール、配色、ショートカットがそのまま移ります。履歴は含まれません。

クラウド同期フォルダの同一ファイルを直接使う場合は、次のように起動できます。

```powershell
python main.py --config "D:\Sync\NewClipboard\config.json"
```

```bash
python3 main.py --config "$HOME/Sync/NewClipboard/config.json"
```

## テストとWindows実行ファイル

```powershell
python -m unittest discover -v
python -m PyInstaller --clean NewClipboard.spec
```

生成物は `dist/NewClipboard.exe` です。macOS版はmacOS上で同じspecを使ってビルドしてください。

アプリアイコンのソースは `assets/newclipboard.png`、Windows用は `assets/newclipboard.ico` です。

## 保存先

- Windows: `%APPDATA%\NewClipboard\config.json`、`history.json`、`images/`
- macOS: `~/.newclipboard/config.json`、`history.json`、`images/`

設定JSONはスキーマバージョン付きです。読み込み時は現設定を `config.json.bak` に退避してから置換します。
