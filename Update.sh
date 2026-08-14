#!/bin/bash

# ==============================================================================
# 環境設定 (必要に応じて変更してください)
# ==============================================================================

# デフォルトのダウンロード先ディレクトリ（第2引数で上書き可能）
# ※ パス展開の問題を防ぐため、`~` の代わりに `$HOME` を使用しています
DEFAULT_DOWNLOAD_DIR="${HOME}/Amatsukaze/Download"

# Amatsukazeの展開先ディレクトリ
TARGET_DIR="${HOME}/Amatsukaze/Amatsukaze"

# バックアップファイルの保存先ディレクトリとファイル名
BACKUP_DIR="${HOME}/Amatsukaze/Download"
BACKUP_FILENAME="Amatsukaze_backup_$(date +%Y%m%d).tar.xz"

# ダウンロードファイル名のプレフィックス（OS環境等に合わせて変更）
FILE_PREFIX="Amatsukaze_Ubuntu24.04_"

# GitHubのリポジトリURLベース
GITHUB_RELEASES_URL="https://github.com/rigaya/Amatsukaze/releases/download"

# ==============================================================================
# システム設定 (固定値)
# ==============================================================================

# AmatsukazeServerCLIの起動ポート
PORT=32768

# プロセス名および実行ファイル名
PROCESS_NAME="AmatsukazeServerCLI"

# ==============================================================================

# 引数が指定されているかチェック
if [ -z "$1" ]; then
    echo "エラー: 第1引数にバージョン（例: 0.9.5.4）を指定してください。"
    echo "使用法: $0 <Version> [ダウンロード先ディレクトリ(省略可)]"
    exit 1
fi

VERSION="$1"
# 第2引数が指定されていればそれを、なければデフォルト値を使用する
DOWNLOAD_DIR="${2:-$DEFAULT_DOWNLOAD_DIR}"
FILENAME="${FILE_PREFIX}${VERSION}.tar.xz"
DOWNLOAD_URL="${GITHUB_RELEASES_URL}/${VERSION}/${FILENAME}"

# スクリプトが配置されているディレクトリを取得し、カレントディレクトリにする
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# ==============================================================================
# 1. 既存フォルダのバックアップ
# ==============================================================================
# 既存の対象ディレクトリが存在する場合、確認プロンプトを表示
if [ -d "$TARGET_DIR" ]; then
    read -p "既存の ${PROCESS_NAME} フォルダをバックアップしますか？ (y/N): " yn
    case "$yn" in
        [yY]*)
            # バックアップ保存先ディレクトリが存在しない場合は作成
            mkdir -p "$BACKUP_DIR"
            # 出力先を絶対パスに変換（コマンド実行時のパスズレを防ぐため）
            BACKUP_DIR_ABS="$(cd "$BACKUP_DIR" && pwd)"
            BACKUP_FILE="${BACKUP_DIR_ABS}/${BACKUP_FILENAME}"
            
            echo "既存のフォルダをバックアップしています (${BACKUP_FILE})..."
            
            # TARGET_DIRの親ディレクトリと、ディレクトリ名本体を動的に取得して圧縮
            TARGET_PARENT="$(dirname "$TARGET_DIR")"
            TARGET_BASENAME="$(basename "$TARGET_DIR")"
            tar -c -C "$TARGET_PARENT" "$TARGET_BASENAME" | xz -v > "$BACKUP_FILE"
            ;;
        *)
            echo "バックアップをスキップしました。"
            ;;
    esac
else
    echo "既存の展開先フォルダが見つからないため、バックアップ処理をスキップします。"
fi

# ==============================================================================
# 2. ダウンロード処理
# ==============================================================================
# ダウンロード先ディレクトリが存在しない場合は作成
mkdir -p "$DOWNLOAD_DIR"

# ダウンロード先ディレクトリを絶対パスに変換（展開時のパスズレを防ぐため）
DOWNLOAD_DIR_ABS="$(cd "$DOWNLOAD_DIR" && pwd)"
DOWNLOAD_PATH="${DOWNLOAD_DIR_ABS}/${FILENAME}"

echo "${PROCESS_NAME} バージョン ${VERSION} をダウンロードしています..."
echo "ダウンロード先: ${DOWNLOAD_PATH}"

# curlを使用してダウンロード (-L: リダイレクト追従, -o: 出力先ファイル名, -f: エラー時に失敗させる)
if ! curl -f -L -o "$DOWNLOAD_PATH" "$DOWNLOAD_URL"; then
    echo "エラー: ダウンロードに失敗しました。バージョン名やネットワーク接続を確認してください。"
    exit 1
fi

# ==============================================================================
# 3. プロセス停止・展開・再起動
# ==============================================================================
# プロセスを全てkill
echo "${PROCESS_NAME} プロセスを終了しています..."
pkill -9 -f "$PROCESS_NAME" || true

# フォルダ内に上書きで展開
echo "アーカイブを展開しています..."
mkdir -p "$TARGET_DIR"
# ダウンロードした絶対パスを指定して展開
tar -xf "$DOWNLOAD_PATH" -C "$TARGET_DIR"

# 展開先に移動
cd "$TARGET_DIR" || exit 1

# バックグラウンドで起動
echo "${PROCESS_NAME} をポート ${PORT} で起動します..."
./exe_files/${PROCESS_NAME} -p "$PORT" &

echo "処理が完了しました。"