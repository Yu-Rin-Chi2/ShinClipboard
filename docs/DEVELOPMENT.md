# 開発者向け情報

ShinClipboard をソースから起動・テスト・ビルドするための手順です。アプリの使い方は
[README](../README.md) を参照してください。

## 目次

- [ソースから起動](#ソースから起動)
- [テスト](#テスト)
- [実行ファイルのビルド](#実行ファイルのビルド)
- [macOSの署名](#macosの署名)
- [アイコン](#アイコン)

## ソースから起動

Python 3.10以降を利用します。

Windows:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

配布版のmacOSアプリ（`ShinClipboard.app`）は、ソースをそのまま実行しているのではなく、下記の「実行ファイルのビルド」の手順でPyInstallerを使ってビルドしたものです。

## テスト

テストは両OSで同じです。

```bash
python -m unittest discover -v
```

## 実行ファイルのビルド

実行ファイルも同じspecからビルドします。ビルドするOSの実行ファイルだけが作られるので、Windows版はWindows上で、macOS版はmacOS上でビルドしてください。

```bash
python -m pip install pyinstaller
python -m PyInstaller --clean ShinClipboard.spec
```

| OS | 生成物 | 形式 |
|----|--------|------|
| Windows | `dist/ShinClipboard.exe` | onefile（単体のexe） |
| macOS | `dist/ShinClipboard.app` | アプリバンドル |

macOSではonefileではなくバンドルにしています。起動が速いのに加えて、アクセシビリティと画面収録の許可はパスに紐づくため、
起動のたびに自己展開する形式では毎回許可を求められてしまうからです。

### macOSの署名

macOSはアクセシビリティと画面収録の許可を**コード署名に対して**記録します。署名なし（ad-hoc）でビルドすると署名がバイナリのハッシュになるため、
ビルドし直すたびに別のアプリ扱いになり、システム設定のトグルはONのままなのに許可が効かなくなります。

そのためspecはビルド時にキーチェーンの署名用証明書を探し、見つかればそれで署名します。Apple Developer の
「Apple Development」証明書があればそのまま使われ、再ビルドしても許可が維持されます。

```bash
security find-identity -v -p codesigning        # 使える証明書の一覧
SHINCLIPBOARD_CODESIGN_IDENTITY="Apple Development: 名前 (TEAMID)" python -m PyInstaller --clean ShinClipboard.spec   # 明示する場合
SHINCLIPBOARD_CODESIGN_IDENTITY=- python -m PyInstaller --clean ShinClipboard.spec   # ad-hoc に戻す場合
```

証明書がないときはad-hoc署名になり、ビルドのたびに許可の付け直し（トグルをOFF→ON）が必要です。
署名の種類を切り替えた直後も一度だけ付け直してください。

配布するときは `.app` をそのままzipせず、シンボリックリンクを保てる `ditto` を使ってください。

```bash
ditto -c -k --keepParent dist/ShinClipboard.app dist/ShinClipboard-mac.zip
```

## アイコン

アプリアイコンのソースは `assets/shinclipboard.png` です。Windows用は `assets/shinclipboard.ico`、macOS用は
`assets/shinclipboard.icns` で、後者は `python tools/make_icns.py` で作り直せます（macOSのアイコングリッドに合わせて余白を入れます）。
