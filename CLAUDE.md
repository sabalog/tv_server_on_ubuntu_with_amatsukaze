# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## リポジトリの概要

Ubuntu 上の TV 録画サーバ（EPGStation + Amatsukaze）を運用するためのスクリプト集。
セットアップの背景は README.md からリンクされている記事を参照。

| ファイル | 役割 |
|---|---|
| `TvRecorder.py` | 中核。録画TSの変換登録・NAS転送・HDDバックアップ・容量管理を行い、cron から定期実行される |
| `Reconvert.py` | 手動で Trim を編集した録画を選んで再変換するための対話型ツール |
| `Update.sh` | Amatsukaze 本体のバージョンアップ（バックアップ→取得→展開→再起動） |

テストコード・ビルド設定・lint 設定は存在しない。依存は `paramiko` のみ。

## 実行環境と開発環境

**実行環境は Ubuntu 専用**。`fcntl`（排他ロック）、`os.stat('/').st_dev`（マウント判定）、
`/mnt/...` の絶対パスに依存している。設定値は `Config` dataclass に書かれている。

例外は NAS の認証情報で、これだけは外部ファイル
（既定 `/home/tv-recorder/Scripts/nas_TvRecorder.conf`、書式は `nas_TvRecorder.conf.sample`）
へ分離している。リポジトリが公開されているため、パスワードをソースへ書けないという理由。
ファイルが無ければ `Config.nas_config` の値がそのまま使われる。

開発が Windows 上で行われる場合、`TvRecorder.py` はそのままでは import できない
（`fcntl` が無い、`paramiko` が未インストール）。ロジック検証には下記のスタブが必要。

## よく使うコマンド

```bash
# 構文チェック（テストが無いため、これが最低限の検証）
python -m py_compile TvRecorder.py Reconvert.py
bash -n Update.sh

# 本番の挙動確認は Config.dry_run = True で行う
#   dry_run 中は Phase 3 の時間帯制限も無視されるため全フェーズが動く

# Reconvert.py は --dry-run (-n) で変更せずに動作を確認できる
./Reconvert.py -n

# Update.sh は第1引数にバージョン。EXPECTED_SHA256 を渡すと取得物を照合する
./Update.sh 1.0.9.0
```

### ロジック検証用のハーネス

テストフレームワークが無いため、動作確認は使い捨てスクリプトで行う。
Windows から実行する場合は Linux 専用モジュールをスタブ化し、UTF-8 出力を強制する。

```python
# PYTHONIOENCODING=utf-8 python - <<'PY'
import types, sys, importlib.util
fc = types.ModuleType("fcntl"); fc.flock = lambda *a: None; fc.LOCK_EX = 2; fc.LOCK_NB = 4
sys.modules["fcntl"] = fc
sys.modules.setdefault("paramiko", types.ModuleType("paramiko"))

spec = importlib.util.spec_from_file_location("tv", "TvRecorder.py")
tv = importlib.util.module_from_spec(spec); sys.modules["tv"] = tv
spec.loader.exec_module(tv)

# 一時ディレクトリを指す Config を組み立てて各フェーズを直接呼ぶ
cfg = tv.Config(source_dir_ts=..., dest_dir_hdd=..., converted_dir=...,
                verify_mount=False,          # 一時ディレクトリはマウント判定を通らない
                ini_file=..., log_file=..., lock_file=...)
tv.activate_realtime_log = lambda: None      # logging.basicConfig で観測する場合
```

`main()` を通す場合は `tv.Config = lambda: cfg` で差し替える。
SFTP を伴う経路は、`listdir_attr` / `stat` / `put` / `rmdir` を持つ偽オブジェクトを
`_connect_sftp` の戻り値として注入する。

**Windows では再現できないもの**（検証済みと書く前に確認すること）:

- パーミッション（`chmod 600` が反映されず、常に緩い状態として観測される）
- シグナル（`trap "" TERM` が効かず、無視するプロセスを模擬できない）
- `ss` / `netstat`（Windows版は別物。Linux の `ss -ltn` 相当を自作して差し替える）
- `subprocess` のタイムアウト後の挙動（POSIX と Windows で実装が異なる）

これらは stub や `find_server_pids` の差し替えで「制御フローの到達」までは確認できる。
実挙動が確認できていない場合は、その旨を明示すること。

## アーキテクチャ

### 実行フロー（`TvRecorder.py`）

`main()` は flock による排他 → ロガー初期化 → 状態読み込み → 3フェーズを**個別に**実行する。
1つのフェーズが例外で落ちても後続は実行され、失敗があれば終了コード 1 を返す。

1. **Phase 1 `TsConverterPipeline`** — `source_dir_ts` の `.ts` を Amatsukaze の
   `AddTask` CLI に登録する。実際のエンコードは Amatsukaze サーバ側の非同期処理なので、
   **登録に成功した時点で「処理済み」として記録される**（エンコード自体の失敗は検知しない）。
2. **Phase 2 `Mp4UploadPipeline`** — `converted_dir` の `.mp4`（と対の `.ass`）を
   SFTP で NAS へ転送する。`-数字` で終わる分割出力は対象外。
3. **Phase 3 `TsBackupPipeline`** — `copy_window_start_hour` の時間帯のみ動作し、
   `.ts` を `dest_dir_hdd` へコピーする。その後、コピー対象の有無に関わらず
   残存TSの回収（後述のフェイルセーフ）を実行する。

### 主要な構成要素

- **`StateManager`** — 処理済み記録（INI）。一時ファイル + `os.replace` によるアトミック更新。
  読み込みに失敗した場合は空状態で続行せず例外を投げる（全ファイル再処理を防ぐため）。
- **`DiskOperations`（抽象）** — `LocalDiskOperations` / `RemoteDiskOperations`(SFTP) が実装。
  `Cleaner` はこの抽象越しに動くので、掃除ロジックがローカルと NAS で共通化されている。
- **`Cleaner`** — 保持ポリシー（ディレクトリ別の日数・容量）→ 全体の容量上限 →
  空ディレクトリ削除、の順で適用する。一覧取得が不完全だった場合
  （`listing_incomplete`）は容量計算が信用できないため削除処理を見送る。
- **`ConditionalBufferHandler`** — ログは一旦バッファし、`cfg.write_log` が立つか
  WARNING 以上が発生した場合のみ出力する。**何も処理が無かった実行では意図的に無出力**
  （cron の毎回実行でログが肥大しないようにするため）。

### `.trim.avs` を介した再変換の仕組み（`Reconvert.py` との連携）

- Phase 3 は、Amatsukaze の `<番組名>-enc.log` から `Trim(...)` 行を抜き出して
  `<TS名>.trim.avs` を作り、TS と一緒に HDD へ置く。このとき avs の mtime を TS に合わせる。
- `Reconvert.py` は `.ts` と `.trim.avs` の **mtime の差（1秒超）を「利用者が Trim を
  手編集した」印**として扱い、再変換候補に挙げる。再変換後は mtime を同期し直して印を消す。

この mtime の一致／不一致が状態を表しているため、`os.utime` の呼び出しを安易に消さないこと。

### 排他ロック（2つのスクリプトで共有）

`TvRecorder.py` と `Reconvert.py` は同じ `/tmp/TvRecorder.lock` を flock する
（`Config.lock_file` と `Reconvert.LOCK_FILE`。**変更するときは両方揃えること**）。
同時に動くと、変換結果の削除とNAS転送が競合したり、同じTSが二重登録される。

`Reconvert.py` がロックを取るのは**利用者の選択が終わってから**。選択待ちの間
握っていると、プロンプトを開いたまま離席された場合に cron 側が停止するため。

### `Update.sh` の構成

`set -euo pipefail` + ERR トラップ + `die()` で、どこで失敗しても無言終了しない。
処理は 1.バックアップ → 2.ダウンロードと検証 → 3.停止と展開 → 4.起動と起動確認 の順。

- 破壊的操作（プロセス停止・上書き展開）の**前**に、バックアップと書庫の検証を終える
- バックアップ・展開・起動のいずれかに失敗したらそこで `exit 1`。起動は
  ポートの待ち受けを確認するまで成功としない
- サーバは `nohup` で起動しログへ追記する（SSH切断で落ちないため）
- 停止対象は `/proc/*/cmdline` の argv[0] 一致で特定する。`pkill -f` だと
  ログを開いている `tail` を巻き込み、`-f` 無しだとプロセス名15文字の壁で
  `AmatsukazeServerCLI`（19文字）に一致しない

## 意図的な設計（バグに見えるが変更してはいけない箇所）

- **状態管理のキーはファイル名のみ**（相対パスではない）。`delete_after_watch/` や
  `keep/` への移動、フォルダ整理でファイルが動いても処理済み判定を維持するため。
  相対パスに変えると移動のたびに再変換・再転送が走る。各スキャンの `seen_names`
  （同名ファイルを1件に絞る処理）も同じ前提を支えているので、セットで維持すること。
  詳細は `StateManager` のクラス docstring を参照。
- **`ts_delete_days` による録画元TSの削除は、バックアップ済みか判定しない**。
  録画元TSは通常 EPGStation が削除するため、この処理は「何らかの理由で削除されずに
  残ったファイルがディスクを圧迫するのを防ぐ」フェイルセーフである。
  バックアップ未完了を理由に削除を見送ると本来の目的を果たせない。
- **静穏時に何も出力しないログ設計**は仕様。異常を握りつぶしているわけではなく、
  WARNING 以上が出れば経緯ごと必ず出力される。
- **`Update.sh` がサーバを SIGKILL 前提で止めるのは、調査のうえでの判断**。
  実機で **SIGTERM は30秒待っても応答しなかった**（確認済み）。上流の
  `AmatsukazeServerCLI/ServerCLI.cs` には SIGINT / SIGTERM 双方のハンドラが
  あるが、SIGTERM 側（`AssemblyLoadContext.Unloading`）は機能していない。
  **SIGINT が効くかは実機で未確認**。`AmatsukazeAddTask` に停止コマンドは無い
  （確認済み）ため、シグナル以外の手段が無い。
  現在は SIGINT を送って3秒だけ待ち、応答が無ければ SIGKILL する。応答する
  バージョンになればそのまま穏当に停止する。
  サーバは停止時に後始末をしない（`EndServer()` はメッセージループを終わらせる
  だけ）ので、強制終了の影響はエンコード中断時の一時ファイル程度。
  恒久対策を検討するなら systemd サービス化が本命。

## 変更時の注意

- 破壊的操作（ファイル削除・上書き）を伴う変更は、失敗時に既存データが残ることを
  必ず確認する。コピー・INI更新はいずれも「一時ファイル → `os.replace`」で実装済み。
- 外部コマンドや SFTP を呼ぶ箇所には必ずタイムアウトを設ける。応答が返らないまま
  停止すると flock を握ったままになり、以降の cron 実行が全て無言でスキップされる。
- コミットメッセージは日本語で、「何が問題だったか → なぜそう直したか」を書く運用。
- **コミットの author / committer は `sabalog <webmaster@sabalog.com>` に統一する**。
  グローバル設定が別の ID になっている環境があるため、コミット前に必ず確認すること。

  ```bash
  git config user.name  "sabalog"
  git config user.email "webmaster@sabalog.com"   # リポジトリローカルに設定する
  git log -1 --pretty='%an <%ae> / %cn <%ce>'     # コミット後の確認
  ```
