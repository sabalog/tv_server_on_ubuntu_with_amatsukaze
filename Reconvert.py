#!/usr/bin/env python3

import subprocess
import sys
import os
import argparse
import glob
from pathlib import Path
from datetime import datetime

DRY_RUN_MODE = False

ORIGINAL_BASES = [
    Path("/mnt/hdd/ts_files").resolve(),
    Path("/mnt/tv-recorder/recorded_files").resolve()
]
CONVERTED_BASE = Path("/mnt/converted_files").resolve()

DEFAULT_CLI_PATH = "/home/tv-recorder/Amatsukaze/Amatsukaze/exe_files/AmatsukazeAddTask"
DEFAULT_SERVER_IP = "localhost"
DEFAULT_PROFILE = "QsvEnc"


def get_candidate_files(base_dirs):
    candidates = []
    
    for base_dir in base_dirs:
        if not base_dir.exists():
            print(f"Warning: Directory not found -> {base_dir}")
            continue

        print(f"Searching for files in {base_dir} ...")
        for ts_path in base_dir.rglob("*.ts"):
            avs_path = ts_path.with_name(ts_path.name + ".trim.avs")
            
            if avs_path.exists():
                ts_stat = ts_path.stat()
                avs_stat = avs_path.stat()

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

def delete_existing_converted_files(target_dir, file_stem):
    escaped_stem = glob.escape(file_stem)
    pattern = f"{escaped_stem}*.*"
    
    if not target_dir.exists():
        return

    matched_files = list(target_dir.rglob(pattern))
    
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

        delete_existing_converted_files(CONVERTED_BASE, input_path.stem)

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
            subprocess.run(cmd, check=True)
            print("  -> Success: タスク登録完了")
            sync_avs_timestamp(input_path)

    except subprocess.CalledProcessError as e:
        print(f"  -> Error: コマンド実行失敗 (Code: {e.returncode})")
    except Exception as e:
        print(f"  -> Error: {e}")

def parse_user_selection(user_input, max_len):
    if user_input.lower() == 'a':
        return list(range(max_len))

    selected = set()
    parts = user_input.split(",")
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        if "-" in part:
            try:
                range_parts = part.split("-")
                if len(range_parts) == 2:
                    start = int(range_parts[0])
                    end = int(range_parts[1])
                    s, e = min(start, end), max(start, end)
                    
                    for i in range(s, e + 1):
                        if 1 <= i <= max_len:
                            selected.add(i - 1)
            except ValueError:
                pass
        else:
            try:
                idx = int(part)
                if 1 <= idx <= max_len:
                    selected.add(idx - 1)
            except ValueError:
                pass
                
    return sorted(list(selected))

def main():
    parser = argparse.ArgumentParser(description="Amatsukaze Interactive Converter")
    parser.add_argument("-a", "--all", action="store_true", help="確認なしですべての候補を変換します")
    args = parser.parse_args()

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
        user_input = input(f"変換対象を選択 > ").strip()

        if not user_input:
            print("選択なし。終了します。")
            return

        selected_indices = parse_user_selection(user_input, len(candidates))

    if not selected_indices:
        print("有効な番号が選択されませんでした。")
        return

    mode_text = "【DryRun】" if DRY_RUN_MODE else "【実行】"
    print(f"\n--- {mode_text} 開始 ({len(selected_indices)}件) ---")
    
    for idx in selected_indices:
        target_file = candidates[idx]["path"]
        convert_single_file(target_file, DEFAULT_CLI_PATH, DEFAULT_SERVER_IP, DEFAULT_PROFILE)

if __name__ == "__main__":
    main()
