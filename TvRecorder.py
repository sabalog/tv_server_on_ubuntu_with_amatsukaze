#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TV録画統合管理スクリプト (Integrated TV Recorder Manager)

【構成】
  - Config: 設定管理
  - StateManager: 状態管理 (INI)
  - DiskOperations (Abstract): ディスク操作の抽象化
    - LocalDiskOperations: ローカル操作
    - RemoteDiskOperations: SFTP操作
  - Cleaner: ディスクの掃除・容量管理ロジック
  - Pipelines:
    - TsConverterPipeline (Phase 1)
    - Mp4UploadPipeline (Phase 2)
    - TsBackupPipeline (Phase 3)
  - Logger: 条件付きバッファリングロガー
"""

import configparser
import shutil
import subprocess
import logging
import logging.handlers
import sys
import unicodedata
import fcntl
import posixpath
import socket
import stat
import re
import time
import os
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Generator, Optional, Tuple, Any, Union

try:
    import paramiko
except ImportError:
    print("【エラー】'paramiko' ライブラリが必要です: pip install paramiko", file=sys.stderr)
    sys.exit(1)

# =========================================================
# 0. データ構造 & ユーティリティ
# =========================================================

@dataclass
class FileEntry:
    path: Union[Path, str]
    name: str
    size: int
    mtime: datetime

def format_bytes(size: int) -> str:
    for unit in ["Byte", "KB", "MB"]:
        if size < 1024:
            return f"{size:.2f}{unit}" if unit != "Byte" else f"{size}{unit}"
        size /= 1024
    return f"{size:.2f}GB"

def normalize_str(s: str) -> str:
    return unicodedata.normalize('NFC', s) if isinstance(s, str) else s

def check_mounted(path: Path) -> Tuple[bool, str]:
    """path が（ルートFSとは別の）マウント済みボリューム上にあるかを判定する。

    設定値はマウントポイント直下ではなくその配下のことがあるため
    (例: /mnt/hdd/ts_files)、os.path.ismount() ではなくデバイスIDを比較する。
    マウントが外れている場合、マウントポイントの空ディレクトリはルートFSに
    属するので、ルートと同じデバイスIDになることで検出できる。

    未作成のディレクトリ（マウント後に mkdir する想定のもの）も許容するため、
    存在する最も近い親までさかのぼって判定する。
    """
    try:
        root_dev = os.stat('/').st_dev
    except OSError as e:
        return False, f"ルートFSの情報を取得できません ({e})"

    target = path
    while not target.exists():
        parent = target.parent
        if parent == target:
            return False, f"パスが存在しません: {path}"
        target = parent

    try:
        if target.stat().st_dev == root_dev:
            return False, f"マウントされていません(ルートFS上にあります): {path}"
    except OSError as e:
        return False, f"マウント状態を確認できません: {path} ({e})"

    return True, ""

# Amatsukaze が出力するファイル名の、元ファイル名に続く部分のパターン
#   ""       : 番組名.mp4        (通常の出力)
#   "-1"     : 番組名-1.mp4      (CM分割などによる分割出力)
#   "-enc"   : 番組名-enc.log    (エンコードログ)
#   "-1-enc" : 分割出力に対応するログ
_OUTPUT_SUFFIX_PTN = re.compile(r'^(?:-\d+)?(?:-enc)?$')

def is_output_of(stem: str, base_stem: str) -> bool:
    """stem が base_stem を元に生成された出力ファイルかどうかを判定する。

    単純な startswith による前方一致だと、例えば「ニュース」の変換時に
    別番組である「ニュース7」「ニュース速報」の出力まで削除してしまうため、
    区切り文字（ハイフン）と接尾辞の形を厳密に検査する。
    """
    norm_stem = normalize_str(stem)
    norm_base = normalize_str(base_stem)
    if not norm_stem.startswith(norm_base):
        return False
    return _OUTPUT_SUFFIX_PTN.match(norm_stem[len(norm_base):]) is not None

# =========================================================
# 1. 設定管理 (Configuration)
# =========================================================

@dataclass
class Config:
    # --- パス設定 ---
    source_dir_ts: Path = Path("/mnt/tv-recorder/recorded_files")
    dest_dir_hdd: Path = Path("/mnt/hdd/ts_files")
    converted_dir: Path = Path("/mnt/converted_files")
    
    ini_file: Path = Path("/home/tv-recorder/Scripts/status_TvRecorder.ini")
    log_file: Path = Path("/home/tv-recorder/Scripts/process_TvRecorder.log")
    lock_file: Path = Path("/tmp/TvRecorder.lock")

    # --- 動作設定 ---
    dry_run: bool = False
    write_log: bool = False
    scan_threshold_sec: int = 10
    ts_delete_days: int = 30

    # 各ディレクトリがマウント済みかを確認してから処理する。
    # マウントが外れているとマウントポイントの空ディレクトリがルートFS上に見えるため、
    # 気付かずにシステムディスクへコピーしたり、容量0と誤認して掃除処理が
    # 誤動作したりする。すべて同一FS上で運用する場合は False にする。
    verify_mount: bool = True

    # --- HDDコピー実行許可時間帯 ---
    copy_window_start_hour: int = 3

    # --- タイムアウト設定 (秒) ---
    # 応答が返らないまま停止すると、ロックを握ったままになり以降の実行が
    # 全てスキップされ続けるため、外部とのやり取りには必ず上限を設ける。
    amatsukaze_timeout_sec: int = 60    # AddTaskはタスク登録のみなので短くてよい
    sftp_connect_timeout_sec: int = 30  # NASへの接続・認証
    sftp_io_timeout_sec: int = 300      # 転送中の1回の読み書き（ファイル全体ではない）

    # --- NAS接続設定 ---
    nas_config: Dict = field(default_factory=lambda: {
        'host': '192.168.1.2',
        'port': 22,
        'user': 'tv-recorder',
        'password': '********',
        'key_file': None,
        'dest_dir': '/tv_program/converted_files'
    })

    # --- 除外設定 ---
    skip_folders_ts: List[str] = field(default_factory=lambda: ['no_conversion'])
    hdd_exclude_dirs: List[str] = field(default_factory=lambda: ['keep'])
    converted_exclude_dirs: List[str] = field(default_factory=list)
    nas_exclude_dirs: List[str] = field(default_factory=lambda: ['/keep/', '/delete_after_watch/'])

    # --- 容量上限設定 (GB) ---
    max_size_hdd_gb: float = 4500.0
    max_size_converted_gb: float = 150.0
    max_size_nas_gb: float = 4000.0

    # --- 保持ポリシー ---
    retention_policies_hdd: List[Dict] = field(default_factory=lambda: [
        {"dir": "delete_after_watch", "days": 365, "limit_gb": 1500},
        {"dir": "delete", "days": 182, "limit_gb": 3000}
    ])
    retention_policies_nas: List[Dict] = field(default_factory=lambda: [
        {"dir": "delete", "days": 365, "limit_gb": 2000},
    ])

    # --- Amatsukaze設定 ---
    amatsukaze_cmd: str = "/home/tv-recorder/Amatsukaze/Amatsukaze/exe_files/AmatsukazeAddTask"
    amatsukaze_ip: str = "localhost"
    amatsukaze_service: str = "QsvEnc"

    @property
    def is_hdd_copy_time_window(self) -> bool:
        return datetime.now().hour == self.copy_window_start_hour

# =========================================================
# 2. ロガー & 状態管理 & 排他制御
# =========================================================

class ConditionalBufferHandler(logging.Handler):
    def __init__(self, log_file: Path):
        super().__init__()
        self.buffer = []
        self.passthrough = False
        
        if not log_file.parent.exists():
            log_file.parent.mkdir(parents=True, exist_ok=True)
            
        self.stream_handler = logging.StreamHandler(sys.stdout)
        self.file_handler = logging.FileHandler(log_file, encoding='utf-8')

    def setFormatter(self, fmt):
        super().setFormatter(fmt)
        self.stream_handler.setFormatter(fmt)
        self.file_handler.setFormatter(fmt)

    def emit(self, record):
        # WARNING以上が出たら、その時点で出力モードへ切り替える。
        # 「何も処理が無ければ出力しない」仕組みのせいで、警告やエラーが
        # 誰にも見られないまま破棄されるのを防ぐ（それまでの経緯も併せて出す）。
        if not getattr(self, 'passthrough', False) and record.levelno >= logging.WARNING:
            self.flush_all(True)

        if getattr(self, 'passthrough', False):
            self.stream_handler.handle(record)
            self.file_handler.handle(record)
        else:
            if not hasattr(self, 'buffer'):
                self.buffer = []
            self.buffer.append(record)

    def flush_all(self, should_write: bool):
        if should_write:
            self.passthrough = True
            if hasattr(self, 'buffer'):
                for record in self.buffer:
                    self.stream_handler.handle(record)
                    self.file_handler.handle(record)
                self.stream_handler.flush()
                self.file_handler.flush()
        self.buffer = []

    def close(self):
        self.stream_handler.close()
        self.file_handler.close()
        super().close()

def setup_logger(log_path: Path) -> ConditionalBufferHandler:
    root = logging.getLogger()
    if root.hasHandlers(): root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler = ConditionalBufferHandler(log_path)
    handler.setFormatter(fmt)
    root.addHandler(handler)
    return handler

def activate_realtime_log():
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, ConditionalBufferHandler):
            h.flush_all(True)

def acquire_lock(lock_path: Path):
    try:
        lock_fd = open(lock_path, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except IOError:
        return None

class StateManager:
    SECTION_COPY_HDD = 'CopyHDD'
    SECTION_CONVERT = 'Convert'
    SECTION_UPLOAD_NAS = 'UploadNAS'

    def __init__(self, ini_path: Path):
        self.ini_path = ini_path
        self.config = configparser.ConfigParser(delimiters=('\t',))
        self.config.optionxform = str
        self._load()

    def _load(self):
        if self.ini_path.exists():
            # ConfigParser.read() は開けないファイルを黙って無視してしまうため、
            # 明示的に open() して read_file() で読む（権限エラー等を検知するため）
            try:
                with open(self.ini_path, encoding='utf-8') as f:
                    self.config.read_file(f)
            except Exception as e:
                # 状態ファイルが読めない場合に空の状態で続行すると、処理済みの
                # 全ファイルを再変換・再転送してしまい被害が大きい。
                # 破損ファイルは上書きせずに残したまま中断し、運用者の判断を仰ぐ。
                raise RuntimeError(
                    f"状態ファイルの読み込みに失敗しました: {self.ini_path} ({e}) / "
                    f"破損している可能性があります。内容を確認し、退避または削除してから再実行してください"
                ) from e

        for sec in [self.SECTION_COPY_HDD, self.SECTION_CONVERT, self.SECTION_UPLOAD_NAS]:
            if sec not in self.config: self.config[sec] = {}

    def is_recorded(self, section: str, file_name: str, file_size: int) -> bool:
        if section not in self.config: return False
        norm_name = normalize_str(file_name)
        sec_data = {normalize_str(k): v for k, v in self.config[section].items()}
        return sec_data.get(norm_name) == str(file_size)

    def is_key_exists(self, section: str, file_name: str) -> bool:
        if section not in self.config: return False
        norm_name = normalize_str(file_name)
        return norm_name in {normalize_str(k) for k in self.config[section].keys()}

    def update_entry(self, section: str, file_name: str, file_size: int, dry_run: bool = False):
        if dry_run:
            logging.info(f"  [DryRun] INI更新 [{section}]: {file_name}")
            return
        try:
            sec_data = self.config[section]
            if file_name not in sec_data:
                items = list(sec_data.items())
                sec_data.clear()
                sec_data[file_name] = str(file_size)
                for k, v in items: sec_data[k] = v
            else:
                sec_data[file_name] = str(file_size)

            self._save()
        except Exception as e:
            logging.error(f"INI Update Failed: {e}")

    def _save(self):
        """一時ファイルへ書き出してから置き換える（アトミック更新）。

        直接上書きしていると、書き込み中に電源断やkillが発生した場合に
        状態ファイルが中途半端な内容になり、次回起動時に状態を全て失って
        全ファイルの再変換・再転送を引き起こす。
        """
        tmp_path = self.ini_path.with_name(f"{self.ini_path.name}.tmp.{os.getpid()}")
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                self.config.write(f)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, self.ini_path)

            # rename 自体を永続化する（ディレクトリのfsync。Linux以外では失敗するので無視）
            try:
                dir_fd = os.open(str(self.ini_path.parent), os.O_RDONLY)
                try: os.fsync(dir_fd)
                finally: os.close(dir_fd)
            except OSError: pass
        finally:
            # 置き換えに失敗した場合、書きかけの一時ファイルを残さない
            if tmp_path.exists():
                try: tmp_path.unlink()
                except OSError: pass

# =========================================================
# 3. ディスク操作の抽象化 (Disk Operations & Cleaner)
# =========================================================

class DiskOperations(ABC):
    # 直近の list_files_recursive() が途中でエラーになり、一覧が不完全かどうか。
    # 不完全な一覧のまま容量を計算すると掃除処理が誤判断するため、呼び出し側が確認する。
    listing_incomplete = False

    @abstractmethod
    def list_files_recursive(self, root_dir: Union[Path, str], exclude_dirs: List[str] = None) -> Generator[FileEntry, None, None]:
        pass

    @abstractmethod
    def delete_file(self, path: Union[Path, str]) -> bool:
        pass

    @abstractmethod
    def remove_empty_dir(self, path: Union[Path, str]) -> bool:
        pass

    @abstractmethod
    def exists(self, path: Union[Path, str]) -> bool:
        pass

class LocalDiskOperations(DiskOperations):
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def list_files_recursive(self, root_dir: Path, exclude_dirs: List[str] = None) -> Generator[FileEntry, None, None]:
        exclude_dirs = exclude_dirs or []
        excludes = [root_dir / d for d in exclude_dirs]
        self.listing_incomplete = False

        for p in root_dir.rglob('*'):
            try:
                if not p.is_file(): continue
                if any(ex in p.parents for ex in excludes): continue
                st = p.stat()
                entry = FileEntry(path=p, name=p.name, size=st.st_size,
                                  mtime=datetime.fromtimestamp(st.st_mtime))
            except FileNotFoundError:
                # 走査中に録画ソフト等が削除した。一覧から漏れても実害はないので続行する
                continue
            except OSError as e:
                # 権限エラー等は一覧が不完全になるため、容量判定に使わせない
                self.listing_incomplete = True
                logging.warning(f"  ファイル情報を取得できません: {p} ({e})")
                continue

            yield entry

    def delete_file(self, path: Path) -> bool:
        size_str = "Unknown"
        try:
            size_str = format_bytes(path.stat().st_size)
        except Exception: pass

        if self.dry_run:
            logging.info(f"    [DryRun] 削除: {path} ({size_str})")
            return True
        try:
            path.unlink()
            logging.info(f"    削除: {path} ({size_str})")
            return True
        except Exception as e:
            # 無言で失敗すると、容量上限がいつまでも効かないまま満杯になる
            logging.warning(f"    削除失敗: {path} ({e})")
            return False

    def remove_empty_dir(self, path: Path) -> bool:
        if self.dry_run: return True
        try:
            path.rmdir()
            return True
        except OSError as e:
            logging.warning(f"    空ディレクトリの削除失敗: {path} ({e})")
            return False

    def exists(self, path: Path) -> bool:
        return path.exists()

class RemoteDiskOperations(DiskOperations):
    def __init__(self, sftp, dry_run: bool):
        self.sftp = sftp
        self.dry_run = dry_run

    def list_files_recursive(self, root_dir: str, exclude_dirs: List[str] = None) -> Generator[FileEntry, None, None]:
        exclude_markers = exclude_dirs or []
        self.listing_incomplete = False

        for path, attr, is_dir in self._sftp_walk(root_dir):
            if not is_dir:
                if any(m in path for m in exclude_markers): continue
                try:
                    yield FileEntry(
                        path=path, name=posixpath.basename(path), size=attr.st_size,
                        mtime=datetime.fromtimestamp(attr.st_mtime)
                    )
                except Exception as e:
                    self.listing_incomplete = True
                    logging.warning(f"  [NAS] ファイル情報を取得できません: {path} ({e})")

    def delete_file(self, path: str) -> bool:
        size_str = "Unknown"
        try:
            size_str = format_bytes(self.sftp.stat(path).st_size)
        except Exception: pass

        if self.dry_run:
            logging.info(f"    [DryRun] 削除(NAS): {posixpath.basename(path)} ({size_str})")
            return True
        try:
            self.sftp.remove(path)
            logging.info(f"    削除(NAS): {posixpath.basename(path)} ({size_str})")
        except Exception as e:
            logging.warning(f"    削除失敗(NAS): {path} ({e})")
            return False

        # 付随する字幕ファイルの削除に失敗しても、本体の削除自体は成功とみなす
        if path.endswith('.mp4'):
            ass = posixpath.splitext(path)[0] + '.ass'
            if self.exists(ass):
                try:
                    self.sftp.remove(ass)
                except Exception as e:
                    logging.warning(f"    字幕ファイルの削除失敗(NAS): {ass} ({e})")
        return True

    def remove_empty_dir(self, path: str) -> bool:
        # rmdir の失敗で空かどうかを判定していると、本当のエラーを検知できず、
        # dry-run では中身のあるディレクトリまで「削除」と表示されてしまう。
        # 空かどうかは列挙して明示的に判定する。
        try:
            if self.sftp.listdir(path): return False
        except Exception as e:
            logging.warning(f"  [NAS] ディレクトリを確認できません: {path} ({e})")
            return False

        if self.dry_run: return True
        try:
            self.sftp.rmdir(path)
            return True
        except Exception as e:
            logging.warning(f"  [NAS] 空ディレクトリの削除失敗: {path} ({e})")
            return False

    def exists(self, path: str) -> bool:
        try:
            self.sftp.stat(path)
            return True
        except FileNotFoundError: return False

    def _sftp_walk(self, remote_path):
        # 列挙に失敗したディレクトリがあっても、そこだけ諦めて走査は続ける。
        # ただし一覧が不完全になった事実は記録し、呼び出し側が判断できるようにする。
        try:
            entries = self.sftp.listdir_attr(remote_path)
        except Exception as e:
            self.listing_incomplete = True
            logging.warning(f"  [NAS] ディレクトリを列挙できません: {remote_path} ({e})")
            return

        for attr in entries:
            full_path = posixpath.join(remote_path, attr.filename)
            if stat.S_ISDIR(attr.st_mode):
                yield from self._sftp_walk(full_path)
                yield (full_path, attr, True)
            else:
                yield (full_path, attr, False)


class Cleaner:
    def __init__(self, config: Config, ops: DiskOperations, label: str):
        self.cfg = config
        self.ops = ops
        self.label = label

    def _warn(self, message: str):
        """掃除処理の警告。埋もれないよう必ず出力する。"""
        self.cfg.write_log = True
        activate_realtime_log()
        logging.warning(f"  {self.label} {message}")

    def _listing_is_complete(self, target_dir: Union[Path, str]) -> bool:
        """一覧が最後まで取得できたか。不完全なら容量計算が信用できない。"""
        if not getattr(self.ops, 'listing_incomplete', False):
            return True
        self._warn(f"一覧を最後まで取得できなかったため、削除処理を見送ります [{target_dir}]")
        return False

    def _delete_all(self, delete_list: List[FileEntry]) -> int:
        """削除を実行し、失敗があれば警告する。戻り値は削除できた件数。"""
        deleted = 0
        for f in delete_list:
            if self.ops.delete_file(f.path):
                deleted += 1

        failed = len(delete_list) - deleted
        if failed:
            self._warn(f"{failed}ファイルの削除に失敗しました（想定した容量を確保できていません）")
        return deleted

    def enforce_size_limit(self, target_dir: Union[Path, str], limit_gb: float, exclude_dirs: List[str], priority_dirs: List[str] = None):
        if not self.ops.exists(target_dir): return

        all_files = list(self.ops.list_files_recursive(target_dir, exclude_dirs=[]))
        if not self._listing_is_complete(target_dir): return
        total_size = sum(f.size for f in all_files)
        original_total_size = total_size

        deletable_files = []
        for f in all_files:
            is_excluded = False
            if isinstance(target_dir, Path):
                if exclude_dirs and any(ex in f.path.parts for ex in exclude_dirs):
                    is_excluded = True
            else:
                if exclude_dirs and any(ex in f.path for ex in exclude_dirs):
                    is_excluded = True
            
            if not is_excluded:
                deletable_files.append(f)

        # 優先順位付けロジック: priority_dirs のインデックス順 (小さい方が優先度低)
        p_dirs = priority_dirs or []
        def get_priority(f_entry: FileEntry) -> int:
            for i, p_dir in enumerate(p_dirs):
                if isinstance(f_entry.path, Path):
                    if p_dir in f_entry.path.parts:
                        return len(p_dirs) - i
                else:
                    if f"/{p_dir}/" in f_entry.path:
                        return len(p_dirs) - i
            return len(p_dirs) + 1

        deletable_files.sort(key=lambda x: (get_priority(x), x.mtime))
        
        limit_bytes = limit_gb * (1024**3)
        delete_list = []

        if total_size > limit_bytes:
            for f in deletable_files:
                delete_list.append(f)
                total_size -= f.size
                if total_size <= limit_bytes: break

            # 上限を超えているのに減らせない場合、放置すると満杯になるため必ず知らせる
            if not delete_list:
                self._warn(f"容量上限を超えていますが、削除できるファイルがありません "
                           f"[{target_dir}]: {format_bytes(original_total_size)} / 上限 {limit_gb}GB "
                           f"(全て除外対象です)")
            elif total_size > limit_bytes:
                self._warn(f"削除できるファイルを全て削除しても容量上限を下回りません "
                           f"[{target_dir}]: {format_bytes(original_total_size)} -> "
                           f"{format_bytes(total_size)} / 上限 {limit_gb}GB")

        if delete_list:
            self.cfg.write_log = True
            activate_realtime_log()
            logging.info(f"  {self.label} 現在の全体容量 [{target_dir}]: {format_bytes(original_total_size)}")
            logging.info(f"  {self.label} 全体容量制限チェック [{target_dir}]: 上限 {limit_gb}GB -> {len(delete_list)}ファイル削除")
            self._delete_all(delete_list)

    def apply_retention_policy(self, parent_dir: Union[Path, str], dir_name: str, days: int, limit_gb: int):
        if isinstance(parent_dir, Path):
            target = parent_dir / dir_name
        else:
            target = posixpath.join(parent_dir, dir_name)

        if not self.ops.exists(target): return

        files = list(self.ops.list_files_recursive(target))
        if not self._listing_is_complete(target): return

        total_size = sum(f.size for f in files)
        original_total_size = total_size

        cutoff = datetime.now() - timedelta(days=days)
        delete_list = [f for f in files if f.mtime < cutoff]
        keep_list = [f for f in files if f.mtime >= cutoff]

        keep_list.sort(key=lambda x: x.mtime)
        limit_bytes = limit_gb * (1024**3)
        current_keep_size = sum(f.size for f in keep_list)

        while current_keep_size > limit_bytes and keep_list:
            target_f = keep_list.pop(0)
            delete_list.append(target_f)
            current_keep_size -= target_f.size

        if delete_list:
            self.cfg.write_log = True
            activate_realtime_log()
            logging.info(f"  {self.label} 現在の容量 [{target}]: {format_bytes(original_total_size)}")
            logging.info(f"  {self.label} ポリシー適用 [{dir_name}]: {len(delete_list)}ファイル削除")
            self._delete_all(delete_list)

    def delete_old_files_by_pattern(self, target_dir: Union[Path, str], days: int, pattern: str):
        """target_dir 配下の古いファイルを、日数条件のみで無条件に削除する。

        録画元のTSは通常 EPGStation が削除するため、ここでの削除は
        「何らかの理由で削除されずに残り続けたファイルがディスクを圧迫するのを防ぐ」
        ためのフェイルセーフ。バックアップ済みかどうかは判定しない。
        """
        if not self.ops.exists(target_dir): return
        
        all_files = self.ops.list_files_recursive(target_dir)
        cutoff = datetime.now() - timedelta(days=days)
        
        delete_list = []
        for f in all_files:
            if isinstance(target_dir, Path):
                if not f.path.match(pattern): continue
            else:
                if not f.name.endswith('.ts'): continue

            if f.mtime < cutoff:
                delete_list.append(f)
        
        if delete_list:
            self.cfg.write_log = True
            activate_realtime_log()
            logging.info(f"  {self.label} 古いファイルの削除チェック: {target_dir} ({days}日以上, {pattern})")
            
            count = 0
            for f in delete_list:
                if self.ops.delete_file(f.path):
                    count += 1
                    if isinstance(f.path, Path) and f.name.endswith('.ts'):
                        avs_path = f.path.with_name(f.name + ".trim.avs")
                        if self.ops.exists(avs_path):
                            self.ops.delete_file(avs_path)
            logging.info(f"    -> {count}ファイル削除")

            failed = len(delete_list) - count
            if failed:
                self._warn(f"{failed}ファイルの削除に失敗しました（想定した容量を確保できていません）")


# =========================================================
# 4. パイプライン基底クラス
# =========================================================

class BasePipeline(ABC):
    def __init__(self, config: Config, state: StateManager):
        self.cfg = config
        self.state = state
        self.ops = LocalDiskOperations(config.dry_run)
        self.cleaner = Cleaner(config, self.ops, "[Local]")

    @abstractmethod
    def run(self):
        pass

    def _verify_mounts(self, phase_label: str, *dirs: Path) -> bool:
        """処理対象がマウント済みか確認する。1つでもNGならフェーズごとスキップする。"""
        if not self.cfg.verify_mount: return True

        for d in dirs:
            ok, reason = check_mounted(d)
            if not ok:
                self.cfg.write_log = True
                activate_realtime_log()
                logging.error(f"[Mount] {reason} -> {phase_label} をスキップします")
                return False
        return True

    def _cleanup_empty_dirs_local(self, root: Path, excludes: List[str] = None):
        if not root.exists(): return
        excludes = excludes or []
        deleted_count = 0
        all_dirs = sorted([p for p in root.rglob('*') if p.is_dir()], key=lambda p: len(p.parts), reverse=True)
        exclude_paths = [root / e for e in excludes]

        for d in all_dirs:
            if d in exclude_paths or d == root: continue
            try:
                if not any(d.iterdir()):
                    if self.ops.remove_empty_dir(d):
                        logging.info(f"  [Local] 空ディレクトリ削除: {d}")
                        deleted_count += 1
            except OSError: pass
        if deleted_count > 0:
            self.cfg.write_log = True
            activate_realtime_log()

# =========================================================
# 5. 各フェーズの実装
# =========================================================

class TsConverterPipeline(BasePipeline):
    """Phase 1: TS -> MP4 変換"""
    
    def run(self):
        if not self._verify_mounts("Phase 1", self.cfg.source_dir_ts, self.cfg.converted_dir): return

        tasks = list(self._scan())
        self._cleanup()

        if tasks:
            self.cfg.write_log = True
            activate_realtime_log()
            logging.info("=== Phase 1: TS変換 (Converter) ===")
            logging.info(f"変換対象数: {len(tasks)}")
            for task in tasks:
                self._process(task)

    def _scan(self):
        if not self.cfg.source_dir_ts.exists(): return
        threshold = datetime.now() - timedelta(seconds=self.cfg.scan_threshold_sec)
        seen_names = set()
        
        for p in self.cfg.source_dir_ts.rglob('*.ts'):
            if not p.is_file(): continue
            
            # .Trash-* フォルダ内のファイルは除外
            if any(part.startswith('.Trash-') for part in p.parts):
                continue
            
            if p.name in seen_names:
                continue
            seen_names.add(p.name)
            
            try:
                rel = p.relative_to(self.cfg.source_dir_ts)
            except ValueError:
                # 通常は起きないが、ここで握りつぶすと前のループの rel を流用して
                # 誤った場所へ出力してしまうため、確実にスキップする
                logging.warning(f"  相対パスを解決できないためスキップします: {p}")
                continue

            if len(rel.parts) > 1 and rel.parts[0] in self.cfg.skip_folders_ts: continue

            # 走査中に削除される可能性があるため、stat は1回だけ取って使い回す
            try:
                st = p.stat()
            except OSError:
                continue

            if datetime.fromtimestamp(st.st_mtime) >= threshold: continue

            if self.state.is_recorded(StateManager.SECTION_CONVERT, p.name, st.st_size):
                continue

            yield {'src': p, 'rel': rel, 'size': st.st_size}

    def _cleanup(self):
        self.cleaner.enforce_size_limit(self.cfg.converted_dir, self.cfg.max_size_converted_gb, self.cfg.converted_exclude_dirs)
        self._cleanup_empty_dirs_local(self.cfg.converted_dir)

    def _process(self, task):
        src = task['src']
        logging.info(f"[Converter] 登録: {src.name} ({format_bytes(task['size'])})")
        out_dir = self.cfg.converted_dir / task['rel'].parent
        
        if self._prepare_out_dir(out_dir, src.stem) and self._exec_amatsukaze(src, out_dir):
            log_file = out_dir / f"{src.stem}-enc.log"
            try:
                if not self.cfg.dry_run:
                    log_file.touch()
                logging.info(f"  -> エンコードログファイル作成: {log_file.name}")
            except Exception as e:
                logging.warning(f"  -> エンコードログファイル作成失敗: {e}")

            self.state.update_entry(StateManager.SECTION_CONVERT, src.name, task['size'], self.cfg.dry_run)

    def _prepare_out_dir(self, out_dir, stem):
        if self.cfg.dry_run: return True
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            # 再変換に備えて、この TS から生成された過去の出力のみを削除する
            # （別番組を巻き込まないよう is_output_of で厳密に判定する）
            for p in out_dir.iterdir():
                if p.is_file() and is_output_of(p.stem, stem):
                    p.unlink()
                    logging.info(f"  -> 既存の変換結果を削除: {p.name}")
            return True
        except Exception as e:
            logging.error(f"  -> 出力先の準備に失敗したため変換をスキップします: {out_dir} ({e})")
            return False

    def _exec_amatsukaze(self, src, out_dir):
        cmd = [self.cfg.amatsukaze_cmd, "-ip", self.cfg.amatsukaze_ip,
               "-s", self.cfg.amatsukaze_service, "-o", str(out_dir), "-f", str(src)]
        if self.cfg.dry_run:
            logging.info(f"  [DryRun] CMD: {cmd}")
            return True
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True,
                           timeout=self.cfg.amatsukaze_timeout_sec)
            return True
        except subprocess.TimeoutExpired:
            logging.error(f"  -> コマンドが{self.cfg.amatsukaze_timeout_sec}秒応答しないため中断しました: {self.cfg.amatsukaze_cmd}")
            return False
        except subprocess.CalledProcessError as e:
            # 原因究明のため、例外の文字列だけでなくコマンドの出力も残す
            detail = (e.stderr or "").strip() or (e.stdout or "").strip() or "(出力なし)"
            logging.error(f"  -> コマンド失敗 (終了コード {e.returncode}): {detail}")
            return False
        except Exception as e:
            logging.error(f"  -> コマンド失敗: {e}")
            return False


class Mp4UploadPipeline(BasePipeline):
    """Phase 2: MP4 -> NAS 転送"""

    def __init__(self, config: Config, state: StateManager):
        super().__init__(config, state)
        self.sftp = None
        self.transport = None

    def run(self):
        if not self._verify_mounts("Phase 2", self.cfg.converted_dir): return
        if not self.cfg.converted_dir.exists(): return

        candidates = list(self._scan_candidates())
        if not candidates: return

        self.sftp, self.transport = self._connect_sftp()
        if not self.sftp: 
            self.cfg.write_log = True
            activate_realtime_log()
            logging.info("=== Phase 2: MP4転送 (Upload) ===")
            logging.error("[NAS] SFTPサーバーへの接続に失敗しました。転送処理をスキップします。")
            return

        self.ops = RemoteDiskOperations(self.sftp, self.cfg.dry_run)
        self.cleaner = Cleaner(self.cfg, self.ops, "[NAS]")

        try:
            self._cleanup()
            
            # 実際にアップロードするタスクのみを抽出
            tasks_to_process = []
            for task in candidates:
                if not self._connection_is_alive():
                    logging.error("[NAS] 接続が切断されたため、転送要否の確認を中止します")
                    break
                if self._check_remote_status(task):
                    tasks_to_process.append(task)

            # 本当に処理するタスクがある場合のみログを有効化・バナー出力
            if tasks_to_process:
                self.cfg.write_log = True
                activate_realtime_log()
                logging.info("=== Phase 2: MP4転送 (Upload) ===")

                processed = 0
                failed = 0
                for task in tasks_to_process:
                    # 切断後に残りのタスクを延々と失敗させ続けないよう、都度確認する
                    if not self._connection_is_alive():
                        logging.error(f"[NAS] 接続が切断されたため、残り{len(tasks_to_process) - processed - failed}件の転送を中止します")
                        break
                    if self._process(task):
                        processed += 1
                    else:
                        failed += 1

                if processed > 0:
                    logging.info(f"MP4転送完了数: {processed}")
                if failed > 0:
                    logging.warning(f"MP4転送失敗数: {failed}")

        finally:
            if self.transport: self.transport.close()

    def _connect_sftp(self):
        c = self.cfg.nas_config
        t = None
        try:
            # Transportにホスト名を渡すとOS既定(数分)まで待つため、接続は自前で行う
            sock = socket.create_connection((c['host'], c['port']),
                                            timeout=self.cfg.sftp_connect_timeout_sec)
            t = paramiko.Transport(sock)
            t.banner_timeout = self.cfg.sftp_connect_timeout_sec
            t.auth_timeout = self.cfg.sftp_connect_timeout_sec

            if c['key_file']:
                k = paramiko.RSAKey.from_private_key_file(c['key_file'])
                t.connect(username=c['user'], pkey=k)
            else:
                t.connect(username=c['user'], password=c['password'])

            sftp = paramiko.SFTPClient.from_transport(t)
            if sftp is None:
                raise IOError("SFTPセッションを開けませんでした")

            # 転送中に応答が途絶えたまま停止しないようにする
            sftp.get_channel().settimeout(self.cfg.sftp_io_timeout_sec)
            return sftp, t
        except Exception as e:
            # 認証NG・名前解決失敗・タイムアウトを切り分けられるよう理由を残す
            logging.error(f"[NAS] SFTP接続に失敗しました ({c['user']}@{c['host']}:{c['port']}): {e}")
            if t is not None:
                try: t.close()
                except Exception: pass
            return None, None

    def _scan_candidates(self) -> Generator[Dict, None, None]:
        dest_root = self.cfg.nas_config['dest_dir']
        threshold = datetime.now() - timedelta(seconds=self.cfg.scan_threshold_sec)
        exclude_ptn = re.compile(r'-\d+$')
        seen_names = set()

        for p in self.cfg.converted_dir.rglob('*.mp4'):
            if not p.is_file(): continue
            
            # .Trash-* フォルダ内のファイルは除外
            if any(part.startswith('.Trash-') for part in p.parts):
                continue
            
            if p.name in seen_names:
                continue
            seen_names.add(p.name)
            
            if exclude_ptn.search(p.stem): continue

            # 走査中に削除・入れ替えされる可能性があるため、stat は1回だけ取って使い回す
            try:
                st = p.stat()
                rel = p.relative_to(self.cfg.converted_dir).as_posix()
            except OSError:
                continue
            except ValueError:
                logging.warning(f"  相対パスを解決できないためスキップします: {p}")
                continue

            if datetime.fromtimestamp(st.st_mtime) >= threshold: continue

            size = st.st_size
            if self.state.is_recorded(StateManager.SECTION_UPLOAD_NAS, p.name, size): continue

            dest = posixpath.join(dest_root, rel)
            yield {'src': p, 'dest': dest, 'name': p.name, 'size': size, 'type': 'mp4'}

            ass = p.with_suffix('.ass')
            try:
                ass_size = ass.stat().st_size
            except OSError:
                continue   # 字幕が無い/読めない場合は本体のみ転送する

            ass_dest = posixpath.join(dest_root, ass.relative_to(self.cfg.converted_dir).as_posix())
            yield {'src': ass, 'dest': ass_dest, 'size': ass_size, 'type': 'ass'}

    def _check_remote_status(self, task) -> bool:
        try:
            r_size = self.sftp.stat(task['dest']).st_size
            if r_size == task['size']:
                # すでに同じサイズのファイルがNAS上に存在する場合はスキップし、INIのみ更新
                if task['type'] == 'mp4':
                    self.state.update_entry(StateManager.SECTION_UPLOAD_NAS, task['name'], task['size'], self.cfg.dry_run)
                return False
            task['reason'] = "サイズ変更(NAS既存)"
            return True
        except FileNotFoundError:
            task['reason'] = "新規"
            return True
        except Exception as e:
            # 接続断や権限エラーでフェーズごと落とさない。このファイルは次回に回す
            logging.warning(f"[NAS] 転送要否を判定できません: {task['dest']} ({e})")
            return False

    def _connection_is_alive(self) -> bool:
        try:
            return bool(self.transport and self.transport.is_active())
        except Exception:
            return False

    def _cleanup(self):
        root = self.cfg.nas_config['dest_dir']
        nas_excludes = self.cfg.nas_exclude_dirs.copy()
        
        priority_dirs = []
        for pol in self.cfg.retention_policies_nas:
            self.cleaner.apply_retention_policy(root, pol['dir'], pol['days'], pol['limit_gb'])
            priority_dirs.append(pol['dir'])
            
        self.cleaner.enforce_size_limit(root, self.cfg.max_size_nas_gb, nas_excludes, priority_dirs)
        self._cleanup_empty_dirs_remote(root)

    def _cleanup_empty_dirs_remote(self, root: str):
        dirs = []
        try:
            for path, _, is_dir in self.ops._sftp_walk(root):
                if is_dir: dirs.append(path)
        except Exception: return

        dirs.sort(key=lambda s: len(s), reverse=True)
        norm_root = root.rstrip('/')
        
        count = 0
        for d in dirs:
            if posixpath.dirname(d.rstrip('/')) == norm_root: continue
            if self.ops.remove_empty_dir(d):
                logging.info(f"  [NAS] 空ディレクトリ削除: {d}")
                count += 1
        if count > 0:
            self.cfg.write_log = True
            activate_realtime_log()

    def _process(self, task) -> bool:
        dest_dir = posixpath.dirname(task['dest'])
        logging.info(f"[NAS] Upload ({task['reason']}): {task['src'].name} -> {dest_dir}")
        if not self._upload(task['src'], task['dest']):
            return False

        if task['type'] == 'mp4':
            self.state.update_entry(StateManager.SECTION_UPLOAD_NAS, task['name'], task['size'], self.cfg.dry_run)
        return True

    def _upload(self, src, dest):
        if self.cfg.dry_run:
            logging.info("  [DryRun] Uploaded.")
            return True
        try:
            self._mkdir_p(posixpath.dirname(dest))
            
            size_str = format_bytes(src.stat().st_size)
            logging.info(f"  -> Upload開始 ({size_str})")
            start = time.time()
            self.sftp.put(str(src), dest)
            dur = time.time() - start
            logging.info(f"  -> Upload完了 (所要時間: {dur:.1f}秒)")
            return True
        except Exception as e:
            logging.error(f"  -> Upload失敗: {e}")
            return False

    def _mkdir_p(self, remote_dir):
        if remote_dir in ['/', '.']: return
        try: self.sftp.stat(remote_dir)
        except FileNotFoundError:
            self._mkdir_p(posixpath.dirname(remote_dir))
            try: self.sftp.mkdir(remote_dir)
            except OSError: pass


class TsBackupPipeline(BasePipeline):
    """Phase 3: TS -> HDD バックアップ"""

    # コピー中の一時ファイルにつける印
    COPY_TMP_MARK = ".copytmp"

    def run(self):
        if not self._verify_mounts("Phase 3", self.cfg.source_dir_ts): return

        # Phase 3 は負荷が高いため、指定の時間帯のみ実行する
        if not self.cfg.is_hdd_copy_time_window and not self.cfg.dry_run: return

        # コピー先のHDDが外れている場合はコピーのみ見送り、後段の残存TS回収は行う
        if self._verify_mounts("Phase 3のHDDコピー", self.cfg.dest_dir_hdd):
            tasks = list(self._scan())
            self._cleanup()

            if tasks:
                self.cfg.write_log = True
                activate_realtime_log()

                dest_path = self.cfg.dest_dir_hdd
                pre_size_str = self._get_dir_size_str(dest_path)
                logging.info("=== Phase 3: TSバックアップ (Backup) ===")
                logging.info(f"コピー対象数: {len(tasks)} (現在のディレクトリサイズ [{dest_path}]: {pre_size_str})")
                for task in tasks:
                    self._process(task)

                post_size_str = self._get_dir_size_str(dest_path)
                logging.info(f"バックアップ完了後のディレクトリサイズ [{dest_path}]: {post_size_str}")

        # 残存TSの回収はフェイルセーフのため、コピー対象の有無やHDDのマウント状態に
        # 関わらず実行する。（コピー対象があった場合は、そのコピーが終わってから
        # 実行されるよう、この位置に置いている）
        self.cleaner.delete_old_files_by_pattern(self.cfg.source_dir_ts, self.cfg.ts_delete_days, "*.ts")

    def _get_dir_size_str(self, target_path: Path) -> str:
        if not target_path.exists():
            return format_bytes(0)
        all_files = self.ops.list_files_recursive(target_path, exclude_dirs=[])
        return format_bytes(sum(f.size for f in all_files))

    def _scan(self):
        if not self.cfg.source_dir_ts.exists(): return
        threshold = datetime.now() - timedelta(seconds=self.cfg.scan_threshold_sec)
        seen_names = set()

        for p in self.cfg.source_dir_ts.rglob('*.ts'):
            if not p.is_file(): continue
            
            if any(part.startswith('.Trash-') for part in p.parts):
                continue
            
            if p.name in seen_names:
                continue
            seen_names.add(p.name)
            
            try:
                rel = p.relative_to(self.cfg.source_dir_ts)
            except ValueError:
                # 前のループの rel を流用して誤った場所へコピーしないよう、確実にスキップする
                logging.warning(f"  相対パスを解決できないためスキップします: {p}")
                continue

            if len(rel.parts) > 1 and rel.parts[0] in self.cfg.skip_folders_ts: continue

            # 走査中に削除される可能性があるため、stat は1回だけ取って使い回す
            try:
                st = p.stat()
            except OSError:
                continue

            if datetime.fromtimestamp(st.st_mtime) >= threshold: continue

            size = st.st_size
            dest = self.cfg.dest_dir_hdd / rel

            if self.state.is_recorded(StateManager.SECTION_COPY_HDD, p.name, size):
                continue

            try:
                if dest.exists() and dest.stat().st_size == size: continue
            except OSError: pass

            yield {'src': p, 'dest': dest, 'size': size, 'rel': rel}

    def _cleanup(self):
        priority_dirs = []
        for pol in self.cfg.retention_policies_hdd:
            self.cleaner.apply_retention_policy(self.cfg.dest_dir_hdd, pol["dir"], pol["days"], pol["limit_gb"])
            priority_dirs.append(pol["dir"])
            
        self.cleaner.enforce_size_limit(self.cfg.dest_dir_hdd, self.cfg.max_size_hdd_gb, self.cfg.hdd_exclude_dirs, priority_dirs)

        # 中断されたコピーの一時ファイルを掃除する
        self._cleanup_stale_copy_tmp()

        # 孤立した（対応する.tsが存在しない）.trim.avsの削除を追加
        self._cleanup_orphaned_avs()
        
        self._cleanup_empty_dirs_local(self.cfg.source_dir_ts)
        self._cleanup_empty_dirs_local(self.cfg.dest_dir_hdd)

    def _copy_tmp_path(self, dest: Path) -> Path:
        return dest.with_name(f"{dest.name}{self.COPY_TMP_MARK}.{os.getpid()}")

    def _cleanup_stale_copy_tmp(self):
        """強制終了などで取り残されたコピー中の一時ファイルを削除する。

        排他ロックにより同時実行はないため、この時点で残っているものは
        全て中断されたコピーの残骸。放置するとTS1本分の容量を占有し続ける。
        """
        if not self.cfg.dest_dir_hdd.exists(): return

        for p in self.cfg.dest_dir_hdd.rglob(f"*{self.COPY_TMP_MARK}.*"):
            try:
                if not p.is_file(): continue
                size_str = format_bytes(p.stat().st_size)
            except OSError: continue

            self.cfg.write_log = True
            activate_realtime_log()
            logging.warning(f"  [Backup] 中断されたコピーの一時ファイルを削除します: {p.name} ({size_str})")
            self.ops.delete_file(p)

    def _cleanup_orphaned_avs(self):
        if not self.cfg.source_dir_ts.exists(): return
        count = 0
        for avs_path in self.cfg.source_dir_ts.rglob('*.trim.avs'):
            # 対応する .ts ファイルのパスを作成 (末尾の .trim.avs を削除して判定)
            ts_path = avs_path.with_name(avs_path.name.replace('.trim.avs', ''))
            if not ts_path.exists():
                self.cfg.write_log = True
                activate_realtime_log()
                if self.ops.delete_file(avs_path):
                    count += 1
        if count > 0:
            logging.info(f"  [Backup] 対応するTSが存在しない孤立したAVSファイルを削除しました: {count}ファイル")

    def _process(self, task):
        src, dest = task['src'], task['dest']
        logging.info(f"[Backup] コピー: {src.name} ({format_bytes(task['size'])})")
        
        if self._copy(src, dest):
            self.state.update_entry(StateManager.SECTION_COPY_HDD, src.name, task['size'], self.cfg.dry_run)
            
            src_avs = Path(f"{src}.trim.avs")
            dest_avs = Path(f"{dest}.trim.avs")

            if src_avs.exists():
                if self.cfg.dry_run:
                    logging.info(f"  [DryRun] 既存のAVSファイルをコピー: {src_avs.name}")
                else:
                    try:
                        shutil.copy2(src_avs, dest_avs)
                        logging.info(f"  -> 既存のAVSファイルをコピー完了: {dest_avs.name}")
                    except Exception as e:
                        logging.warning(f"  -> AVSファイルコピー失敗: {e}")
            else:
                self._create_trim_avs(task)

    def _create_trim_avs(self, task):
        src_stem = task['src'].stem
        log_file = self.cfg.converted_dir / task['rel'].parent / f"{src_stem}-enc.log"
        
        if not log_file.exists(): return
        
        avs_file = Path(f"{task['dest']}.trim.avs")

        if self.cfg.dry_run:
             logging.info(f"  [DryRun] AVS作成: {avs_file.name}")
             return

        try:
            trim_line = None
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if "Trim(" in line:
                        trim_line = line.strip()
                        break
            
            if trim_line:
                with open(avs_file, 'w', encoding='utf-8') as f:
                    f.write(trim_line)
                
                src_stat = task['src'].stat()
                os.utime(avs_file, (src_stat.st_atime, src_stat.st_mtime))
                
                logging.info(f"  -> AVS作成完了: {avs_file.name}")
        except Exception as e:
            logging.warning(f"  -> AVS作成失敗: {e}")

    def _copy(self, src, dest):
        if self.cfg.dry_run:
            logging.info(f"  [DryRun] Copy to: {dest}")
            return True

        # 一時ファイルへコピーしてから置き換える。
        # 直接 dest へ書くと、コピー途中で失敗した場合に、それまで正常だった
        # 既存のバックアップまで失ってしまう。
        # （既存ファイルはコピー完了まで残るため、一時的に2ファイル分の空きが必要）
        tmp_dest = self._copy_tmp_path(dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)

            file_size = src.stat().st_size
            free_space = shutil.disk_usage(dest.parent).free
            if free_space < file_size:
                logging.error(f"  -> HDD容量不足 (空き: {format_bytes(free_space)}, 必要: {format_bytes(file_size)})")
                return False

            logging.info(f"  -> Copy開始 ({format_bytes(file_size)})")
            start = time.time()
            shutil.copy2(src, tmp_dest)
            os.replace(tmp_dest, dest)
            dur = time.time() - start
            logging.info(f"  -> Copy完了 (所要時間: {dur:.1f}秒)")
            return True
        except Exception as e:
            logging.error(f"  -> Copy失敗: {e}")
            return False
        finally:
            # 失敗時に書きかけの一時ファイルを残さない（HDDを圧迫するため）
            if tmp_dest.exists():
                try:
                    tmp_dest.unlink()
                except OSError as e:
                    logging.warning(f"  -> 一時ファイルを削除できません: {tmp_dest} ({e})")


# =========================================================
# メイン処理
# =========================================================

PIPELINES = [
    ("Phase 1 (Converter)", TsConverterPipeline),
    ("Phase 2 (Upload)", Mp4UploadPipeline),
    ("Phase 3 (Backup)", TsBackupPipeline),
]

def main() -> int:
    """終了コードを返す。0=正常、1=いずれかの処理が異常終了。

    cron や systemd から失敗を検知できるよう、異常時は必ず非0で終了する。
    """
    cfg = Config()

    lock_fd = acquire_lock(cfg.lock_file)
    # 先行プロセスが実行中。異常ではないので正常終了扱いとする
    if lock_fd is None: return 0

    try:
        logger = setup_logger(cfg.log_file)
    except Exception as e:
        print(f"【エラー】ログの初期化に失敗しました: {e}", file=sys.stderr)
        lock_fd.close()
        return 1

    failures = []
    try:
        state = StateManager(cfg.ini_file)
        mode = "DryRun" if cfg.dry_run else "Production"

        # NOTE: 起動と終了のログは cfg.write_log が True になった時（=何らかの処理が行われた時）のみ
        # flush_all() 時に一緒に出力される仕組みになっています。
        logging.info(f"=== TV Recorder Manager Start [{mode}] ===")

        # 1つのフェーズが予期しない例外で落ちても、後続のフェーズは実行する。
        # （例: 変換フェーズの失敗で、NAS転送やバックアップまで止めない）
        for name, pipeline_cls in PIPELINES:
            try:
                pipeline_cls(cfg, state).run()
            except Exception as e:
                failures.append(name)
                cfg.write_log = True
                activate_realtime_log()
                logging.exception(f"[{name}] 予期しないエラーで中断しました: {e}")

        if failures:
            logging.error(f"=== 異常終了: {len(failures)}フェーズが失敗しました ({', '.join(failures)}) ===")
        else:
            logging.info("=== All Finished ===")

    except Exception as e:
        failures.append("初期化")
        cfg.write_log = True
        logging.exception(f"Unexpected Error: {e}")

    finally:
        logger.flush_all(cfg.write_log)
        logger.close()
        lock_fd.close()

    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())