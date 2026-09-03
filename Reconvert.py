#!/usr/bin/env python3

import subprocess
import sys
import os
import argparse
import fcntl
import re
import unicodedata
from contextlib import contextmanager, nullcontext
from pathlib import Path
from datetime import datetime

DRY_RUN_MODE = False

ORIGINAL_BASES = [
    Path("/mnt/hdd/ts_files").resolve(),
    Path("/mnt/tv-recorder/recorded_files").resolve()
]
CONVERTED_BASE = Path("/mnt/converted_files").resolve()

# TvRecorder.py の Config.lock_file と同じパスにすること。
# 同じロックを取ることで、cron で動く TvRecorder.py との同時実行を防ぐ。
# （同時に動くと、変換結果の削除と転送が競合したり、同じTSが二重に登録される）
LOCK_FILE = Path("/tmp/TvRecorder.lock")

DEFAULT_CLI_PATH = "/home/tv-recorder/Amatsukaze/Amatsukaze/exe_files/AmatsukazeAddTask"
DEFAULT_SERVER_IP = "localhost"
DEFAULT_PROFILE = "QsvEnc"

# AddTask はタスク登録のみなので短くてよい
CLI_TIMEOUT_SEC = 60


@contextmanager
def exclusive_lock(lock_path):
    """TvRecorder.py と共有する排他ロックを取得する。

    取得は「利用者の選択が終わってから」行うこと。選択待ちの間ロックを握って
    いると、その間 cron の TvRecorder.py が何も処理できなくなる。
    """
    try:
        lock_fd = open(lock_path, 'w')
    except OSError as e:
        print(f"[Error] ロックファイルを開けません ({lock_path}): {e}", file=sys.stderr)
        print("        TvRecorder.py との競合を防げないため中止します。", file=sys.stderr)
        sys.exit(1)

    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("TvRecorder.py が実行中です。終了を待機します... (Ctrl-C で中止)")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            print("  -> ロックを取得しました。処理を開始します。")
        yield
    finally:
        lock_fd.close()


def get_candidate_files(base_dirs):
    candidates = []
    
    for base_dir in base_dirs:
        if not base_dir.exists():
            print(f"Warning: Directory not found -> {base_dir}")
            continue

        print(f"Searching for files in {base_dir} ...")
        for ts_path in base_dir.rglob("*.ts"):
            avs_path = ts_path.with_name(ts_path.name + ".trim.avs")

            # 走査中に録画ソフトが削除することがあるため、1件の失敗で
            # スクリプト全体を止めない
            try:
                if not avs_path.exists():
                    continue
                ts_stat = ts_path.stat()
                avs_stat = avs_path.stat()
            except OSError as e:
                print(f"  Warning: 情報を取得できないためスキップします -> {ts_path} ({e})")
                continue

            if abs(ts_stat.st_mtime - avs_stat.st_mtime) > 1.0:
                candidates.append({
                    "path": ts_path,
                    "avs_mtime": avs_stat.st_mtime
                })

    candidates.sort(key=lambda x: x["avs_mtime"])
    return candidates

def sync_avs_timestamp(ts_path):
    avs_path = ts_path.with_name(ts_path.name + ".trim.avs")
    if not avs_path.exists():
        return

    if DRY_RUN_MODE:
        print(f"  [DryRun] Would sync timestamp: {avs_path.name}")
    else:
        try:
            stat = ts_path.stat()
            os.utime(avs_path, (stat.st_atime, stat.st_mtime))
            print("  -> Sync: AVSファイルの更新日時を同期しました")
        except Exception as e:
            print(f"  -> Sync Error: {e}")

# Amatsukazeが出力するファイル名の、元ファイル名に続く部分のパターン
#   ""       : 番組名.mp4        (通常の出力)
#   "-1"     : 番組名-1.mp4      (CM分割などによる分割出力)
#   "-enc"   : 番組名-enc.log    (エンコードログ)
#   "-1-enc" : 分割出力に対応するログ
OUTPUT_SUFFIX_PTN = re.compile(r'^(?:-\d+)?(?:-enc)?$')

def is_output_of(stem, base_stem):
    """stem が base_stem を元に生成された出力ファイルかどうかを判定する。

    単純な前方一致だと、例えば「ニュース」の再変換時に別番組である
    「ニュース7」「ニュース速報」の出力まで削除してしまうため、
    区切り文字（ハイフン）と接尾辞の形を厳密に検査する。
    """
    norm_stem = unicodedata.normalize('NFC', stem)
    norm_base = unicodedata.normalize('NFC', base_stem)
    if not norm_stem.startswith(norm_base):
        return False
    return OUTPUT_SUFFIX_PTN.match(norm_stem[len(norm_base):]) is not None

def delete_existing_converted_files(target_dir, file_stem):
    """target_dir 直下から、file_stem を元に生成された過去の出力のみを削除する。

    変換先ツリー全体を再帰検索すると、フォルダ違いの無関係なファイルまで
    削除対象になるため、対象は出力先ディレクトリ直下に限定する。
    """
    if not target_dir.exists():
        return

    matched_files = sorted(
        f for f in target_dir.iterdir()
        if f.is_file() and is_output_of(f.stem, file_stem)
    )

    for f in matched_files:
        if DRY_RUN_MODE:
            print(f"  [DryRun] Would delete: {f.name} (at {f.parent})")
        else:
            try:
                f.unlink()
                print(f"  -> Deleted: {f.name} (at {f.parent})")
            except Exception as e:
                print(f"  -> Delete Error ({f.name}): {e}")

def convert_single_file(input_file_path, cli_path, server_ip, profile):
    try:
        input_path = input_file_path
        relative_path = None
        
        # どのベースディレクトリに属しているかを判定して相対パスを取得
        for base_dir in ORIGINAL_BASES:
            try:
                relative_path = input_path.relative_to(base_dir)
                break
            except ValueError:
                continue

        if relative_path is None:
            print(f"[Error] パス解決エラー: {input_path} は指定されたベースディレクトリ内にありません。")
            return

        output_dir = CONVERTED_BASE / relative_path.parent

        print(f"\nProcessing: {input_path.name}")

        if not DRY_RUN_MODE:
            output_dir.mkdir(parents=True, exist_ok=True)

        delete_existing_converted_files(output_dir, input_path.stem)

        cmd = [
            cli_path,
            "-ip", server_ip,
            "-f", str(input_path),
            "-o", str(output_dir),
            "-s", profile
        ]

        if DRY_RUN_MODE:
            cmd_str = " ".join(f"'{c}'" if " " in str(c) else str(c) for c in cmd)
            print(f"  [DryRun] Would run: {cmd_str}")
            sync_avs_timestamp(input_path)
        else:
            # 応答が返らないまま止まるとロックを握ったままになるため上限を設ける
            subprocess.run(cmd, check=True, timeout=CLI_TIMEOUT_SEC)
            print("  -> Success: タスク登録完了")
            sync_avs_timestamp(input_path)

    except subprocess.TimeoutExpired:
        print(f"  -> Error: コマンドが{CLI_TIMEOUT_SEC}秒応答しないため中断しました ({cli_path})")
    except subprocess.CalledProcessError as e:
        print(f"  -> Error: コマンド実行失敗 (Code: {e.returncode})")
    except Exception as e:
        print(f"  -> Error: {e}")

def parse_user_selection(user_input, max_len):
    """選択入力を解釈する。戻り値は (選択されたindexのリスト, 解釈できなかった入力のリスト)。

    誤入力を黙って切り捨てると、利用者が意図したものと違う対象を削除・再変換して
    しまうため、解釈できなかった要素も返して呼び出し側が中止できるようにする。
    範囲外の番号も「打ち間違い」とみなして誤入力として扱う。
    """
    if user_input.lower() == 'a':
        return list(range(max_len)), []

    selected = set()
    invalid = []

    for part in user_input.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            range_parts = part.split("-")
            try:
                if len(range_parts) != 2:
                    raise ValueError
                start, end = int(range_parts[0]), int(range_parts[1])
            except ValueError:
                invalid.append(part)
                continue

            s, e = min(start, end), max(start, end)
            if s < 1 or e > max_len:
                invalid.append(part)
                continue
            selected.update(range(s - 1, e))
        else:
            try:
                idx = int(part)
            except ValueError:
                invalid.append(part)
                continue

            if not (1 <= idx <= max_len):
                invalid.append(part)
                continue
            selected.add(idx - 1)
                
    return sorted(list(selected)), invalid

def main():
    global DRY_RUN_MODE

    parser = argparse.ArgumentParser(description="Amatsukaze Interactive Converter")
    parser.add_argument("-a", "--all", action="store_true", help="確認なしですべての候補を変換します")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="実際には変更せず、行われる操作のみ表示します")
    args = parser.parse_args()

    if args.dry_run:
        DRY_RUN_MODE = True

    candidates = get_candidate_files(ORIGINAL_BASES)

    if not candidates:
        print("条件に一致するファイルが見つかりませんでした。")
        return

    print(f"\n--- 変換候補リスト (全{len(candidates)}件 / 日時昇順) ---")
    for i, item in enumerate(candidates, 1):
        print(f"[{i}] {item['path'].name}")
    print("----------------------")

    selected_indices = []

    if args.all:
        print("オプション -a が指定されました: 全ファイルを選択します。")
        selected_indices = list(range(len(candidates)))
    else:
        print(f"選択肢: 番号(1-{len(candidates)}), 全選択(a)")
        try:
            user_input = input("変換対象を選択 > ").strip()
        except EOFError:
            print("\n入力がありません。終了します。")
            return
        except KeyboardInterrupt:
            print("\n中止しました。")
            sys.exit(130)

        if not user_input:
            print("選択なし。終了します。")
            return

        selected_indices, invalid = parse_user_selection(user_input, len(candidates))

        # 一部だけ解釈できた状態で実行すると、意図しない対象を削除・再変換して
        # しまうため、誤入力があれば何もせずに終了する
        if invalid:
            print(f"[Error] 解釈できない入力があります: {', '.join(invalid)}")
            print(f"        1-{len(candidates)} の番号、範囲(例: 1-3)、カンマ区切り、"
                  f"全選択(a) のいずれかで指定してください。")
            print("        安全のため何も実行せずに終了します。")
            sys.exit(1)

    if not selected_indices:
        print("有効な番号が選択されませんでした。")
        return

    mode_text = "【DryRun】" if DRY_RUN_MODE else "【実行】"
    print(f"\n--- {mode_text} 開始 ({len(selected_indices)}件) ---")

    # DryRun は何も変更しないため、ロック待ちで待たせない
    lock = nullcontext() if DRY_RUN_MODE else exclusive_lock(LOCK_FILE)

    try:
        with lock:
            for idx in selected_indices:
                target_file = candidates[idx]["path"]
                convert_single_file(target_file, DEFAULT_CLI_PATH, DEFAULT_SERVER_IP, DEFAULT_PROFILE)
    except KeyboardInterrupt:
        print("\n中止しました。")
        sys.exit(130)

if __name__ == "__main__":
    main()
