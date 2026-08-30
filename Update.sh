#!/bin/bash

# エラー検知の設定
#   -e          : コマンドが失敗した時点で即座に中断する
#   -u          : 未定義変数の参照をエラーにする
#   -o pipefail : パイプライン中のいずれかが失敗したら全体を失敗扱いにする
#                 （tar | xz のように「前段の失敗」を見逃さないために必須）
set -euo pipefail

# 個別のメッセージを持たないコマンドが set -e で中断した場合でも、
# どこで何が失敗したのか分かるようにする（無言終了の防止）
trap 'echo "エラー: 処理を中断しました ($(basename "${BASH_SOURCE[0]}") ${LINENO}行目: ${BASH_COMMAND})" >&2' ERR

# エラーメッセージを表示して終了する
die() {
    echo "エラー: $1" >&2
    exit 1
}

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

# サーバの標準出力・標準エラーの記録先（起動失敗の原因調査に使う）
SERVER_LOG="${TARGET_DIR}/${PROCESS_NAME}.log"

# 起動確認のタイムアウト（秒）
STARTUP_TIMEOUT_SEC=30

# ==============================================================================

# 引数が指定されているかチェック（set -u 環境では ${1:-} と書かないとエラーになる）
if [ -z "${1:-}" ]; then
    echo "エラー: 第1引数にバージョン（例: 0.9.5.4）を指定してください。" >&2
    echo "使用法: $0 <Version> [ダウンロード先ディレクトリ(省略可)]" >&2
    exit 1
fi

VERSION="$1"
# 第2引数が指定されていればそれを、なければデフォルト値を使用する
DOWNLOAD_DIR="${2:-$DEFAULT_DOWNLOAD_DIR}"
FILENAME="${FILE_PREFIX}${VERSION}.tar.xz"
DOWNLOAD_URL="${GITHUB_RELEASES_URL}/${VERSION}/${FILENAME}"

# スクリプトが配置されているディレクトリを取得し、カレントディレクトリにする
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || die "スクリプトのディレクトリへ移動できません: ${SCRIPT_DIR}"

# ==============================================================================
# 1. 既存フォルダのバックアップ
# ==============================================================================
# 作成したバックアップのパス（未作成なら空文字）。展開失敗時の案内で参照する
BACKUP_FILE=""

# 既存の対象ディレクトリが存在する場合、確認プロンプトを表示
if [ -d "$TARGET_DIR" ]; then
    # 非対話実行（cron/パイプ等）では read が EOF で失敗するため、|| true で中断を防ぐ
    yn=""
    read -p "既存の ${PROCESS_NAME} フォルダをバックアップしますか？ (y/N): " yn || true
    case "$yn" in
        [yY]*)
            # バックアップ保存先ディレクトリが存在しない場合は作成
            mkdir -p "$BACKUP_DIR" \
                || die "バックアップ先ディレクトリを作成できません: ${BACKUP_DIR}"
            # 出力先を絶対パスに変換（コマンド実行時のパスズレを防ぐため）
            BACKUP_DIR_ABS="$(cd "$BACKUP_DIR" && pwd)" \
                || die "バックアップ先ディレクトリへ移動できません: ${BACKUP_DIR}"
            BACKUP_FILE="${BACKUP_DIR_ABS}/${BACKUP_FILENAME}"
            # 一時ファイルに書き出してから差し替える
            # （途中で失敗した場合に、既存の同名バックアップを壊さないため）
            BACKUP_TMP="${BACKUP_FILE}.tmp.$$"

            echo "既存のフォルダをバックアップしています (${BACKUP_FILE})..."

            # TARGET_DIRの親ディレクトリと、ディレクトリ名本体を動的に取得して圧縮
            TARGET_PARENT="$(dirname "$TARGET_DIR")"
            TARGET_BASENAME="$(basename "$TARGET_DIR")"

            # pipefail により tar / xz のどちらが失敗しても検知できる
            # （ディスク満杯・読み取り不可などでバックアップが不完全なまま
            #   後続のプロセス停止・上書き展開へ進むと復旧不能になるため、ここで必ず中断する）
            if ! tar -c -C "$TARGET_PARENT" "$TARGET_BASENAME" | xz -v > "$BACKUP_TMP"; then
                echo "エラー: バックアップの作成に失敗しました (${BACKUP_TMP})。" >&2
                echo "       ディスクの空き容量やファイルの読み取り権限を確認してください。" >&2
                echo "       安全のため、更新処理を中止します。" >&2
                rm -f "$BACKUP_TMP"
                exit 1
            fi

            mv -f "$BACKUP_TMP" "$BACKUP_FILE" \
                || die "バックアップファイルを配置できません: ${BACKUP_FILE}"
            echo "バックアップが完了しました: ${BACKUP_FILE} ($(du -h "$BACKUP_FILE" | cut -f1))"
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
mkdir -p "$DOWNLOAD_DIR" \
    || die "ダウンロード先ディレクトリを作成できません: ${DOWNLOAD_DIR}"

# ダウンロード先ディレクトリを絶対パスに変換（展開時のパスズレを防ぐため）
DOWNLOAD_DIR_ABS="$(cd "$DOWNLOAD_DIR" && pwd)" \
    || die "ダウンロード先ディレクトリへ移動できません: ${DOWNLOAD_DIR}"
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
mkdir -p "$TARGET_DIR" \
    || die "展開先ディレクトリを作成できません: ${TARGET_DIR}"
# ダウンロードした絶対パスを指定して展開
# 展開に失敗した状態（中途半端なファイル構成）でサーバを起動しないよう、ここで中断する
if ! tar -xf "$DOWNLOAD_PATH" -C "$TARGET_DIR"; then
    echo "エラー: アーカイブの展開に失敗しました (${DOWNLOAD_PATH})。" >&2
    echo "       ダウンロードファイルが破損しているか、ディスクの空き容量が不足している可能性があります。" >&2
    echo "       ${TARGET_DIR} は不完全な状態です。サーバは起動しません。" >&2
    if [ -n "$BACKUP_FILE" ] && [ -f "$BACKUP_FILE" ]; then
        echo "       復元する場合: tar -xf \"${BACKUP_FILE}\" -C \"$(dirname "$TARGET_DIR")\"" >&2
    fi
    exit 1
fi

# 展開先に移動
cd "$TARGET_DIR" || die "展開先ディレクトリへ移動できません: ${TARGET_DIR}"

# ==============================================================================
# 4. 起動と起動確認
# ==============================================================================
EXE_PATH="./exe_files/${PROCESS_NAME}"
if [ ! -x "$EXE_PATH" ]; then
    die "実行ファイルが見つからないか実行権限がありません: ${TARGET_DIR}/${EXE_PATH#./}"
fi

echo "${PROCESS_NAME} をポート ${PORT} で起動します..."

# nohup + disown で端末から切り離す。
# これが無いと、SSH を切断した時に SIGHUP でサーバまで終了してしまう。
# 出力を捨てると起動失敗の原因を追えないため、ログファイルへ追記する。
nohup "$EXE_PATH" -p "$PORT" >> "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
disown "$SERVER_PID" 2>/dev/null || true

# ポートの待ち受けを確認する手段があるか調べる
port_check_cmd=""
if command -v ss > /dev/null 2>&1; then
    port_check_cmd="ss"
elif command -v netstat > /dev/null 2>&1; then
    port_check_cmd="netstat"
fi

is_listening() {
    case "$port_check_cmd" in
        ss)      ss -ltn 2>/dev/null | grep -q ":${PORT} " ;;
        netstat) netstat -ltn 2>/dev/null | grep -q ":${PORT} " ;;
        *)       return 1 ;;
    esac
}

report_failure() {
    echo "エラー: $1" >&2
    echo "        ログの末尾を表示します (${SERVER_LOG}):" >&2
    tail -n 20 "$SERVER_LOG" >&2 || true
    exit 1
}

# バックグラウンド起動は成否が終了コードに現れないため、明示的に確認する
printf "起動を確認しています"
started=false
for _ in $(seq 1 "$STARTUP_TIMEOUT_SEC"); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo
        report_failure "${PROCESS_NAME} が起動直後に終了しました。"
    fi

    if [ -z "$port_check_cmd" ]; then
        # ss も netstat も無い環境では、プロセスの生存のみを確認する
        sleep 3
        echo
        echo "警告: ポートの確認手段(ss/netstat)が無いため、待ち受け開始は未確認です。" >&2
        started=true
        break
    fi

    if is_listening; then
        echo
        started=true
        break
    fi

    sleep 1
    printf "."
done

if [ "$started" != true ]; then
    echo
    report_failure "${STARTUP_TIMEOUT_SEC}秒以内にポート ${PORT} の待ち受けを開始しませんでした。"
fi

echo "${PROCESS_NAME} が起動しました (PID: ${SERVER_PID}, ポート: ${PORT})"
echo "ログ: ${SERVER_LOG}"
echo "処理が完了しました。"