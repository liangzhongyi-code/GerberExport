"""
期二進入點：批次匯出（design.md §2.3）。

這支腳本只做三件事——確認可以開始、把任務跑完、把結果寫下來。真正的
判斷全在 lib/ 底下的純函式裡，所以這裡短得可以整段讀完。

啟動順序是刻意的：**任何檢查失敗時，桌面上不會多出任何東西。**
AccuMark 沒開就中止，而使用者不會看到一個空的輸出資料夾以為跑過了。
因此連線與檢查全部排在建立資料夾、寫狀態檔之前。
"""

import argparse
import ctypes
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Tuple

from check_env import configure_stdout
from lib import archival, config as cfg, dryrun, ops as ops_mod, orchestrator as orch
from lib import reporting, runstate, uia

SCRIPTS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPTS_DIR / "config.json"

# model 不能叫這兩個名字：它們是輸出根目錄底下殘留物的位置，撞名會讓
# 產出跟殘留混在同一個資料夾裡。
RESERVED_NAMES = (archival.UNCLASSIFIED_DIRNAME, archival.TIMEOUT_RESIDUE_DIRNAME)


# ── 純函式 ───────────────────────────────────────────────────────────


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(description="AccuMark 批次匯出")
    parser.add_argument("--only", default=None, help="只處理這一個 model")
    parser.add_argument(
        "--format", dest="only_format", default=None, help="只做這一種格式（ZIP／AAMA／ASTM）"
    )
    parser.add_argument(
        "--force", action="store_true", help="忽略上次的進度，全部重跑（舊狀態檔會改名保留）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只檢查介面上的控制項找不找得到，不匯出任何東西",
    )
    return parser.parse_args(argv)


def check_model_names(models: Sequence[str]) -> Optional[str]:
    """model 名稱能不能拿來當資料夾名。不能的話整批都不用開始。"""
    for model in models:
        if model.strip() in RESERVED_NAMES:
            return (
                f"model 名稱 {model!r} 與系統保留的資料夾同名（{list(RESERVED_NAMES)}），"
                "產出會跟殘留物混在一起。請改用明確清單排除它"
            )
        try:
            archival.model_dir(Path("."), model)
        except archival.ArchivalError as exc:
            return str(exc)
    return None


def check_temp_dir(temp_dir: Path, list_fn) -> Optional[str]:
    """
    暫存夾必須是空的（或還不存在）。

    裡面殘留的東西會被下一個任務當成自己的產出，歸到錯的 model 底下——
    所以寧可停下來問人，**絕不自動刪除**：那可能是上次中斷時唯一的一份。
    """
    leftovers = list_fn(temp_dir)
    if not leftovers:
        return None
    names = "、".join(sorted(leftovers)[:5])
    more = "…" if len(leftovers) > 5 else ""
    return (
        f"暫存夾 {temp_dir} 裡還有 {len(leftovers)} 個檔案（{names}{more}）。\n"
        "那是上次執行沒有正常結束留下的，可能是唯一的一份。\n"
        "請先確認它們還需不需要，自行搬走或刪掉之後再跑一次——腳本不會替你刪。"
    )


def list_files(directory: Path) -> Tuple[str, ...]:
    d = Path(directory)
    if not d.is_dir():
        return ()
    return tuple(p.name for p in d.iterdir())


def is_session_locked() -> bool:
    """
    工作階段是不是鎖住了。

    鎖屏之後 Windows 會把整個桌面連同上面的視窗收起來，AccuMark 的視窗
    對程式而言等於不存在，批次會當場停住。開跑前先問一次，比跑到一半
    才失敗好。

    OpenInputDesktop 在鎖定時會失敗——這是不需要提升權限就能問到的做法。
    讀不到就當成沒鎖：這是輔助檢查，不該因為它自己壞掉就擋住整批。
    """
    try:
        user32 = ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not handle:
            return True
        user32.CloseDesktop(handle)
        return False
    except Exception:  # noqa: BLE001
        return False


def resolve_models(config, ops) -> Tuple[Tuple[str, ...], Optional[str]]:
    """
    要處理哪些 model：Explorer 的選取項，或設定檔的明確清單。

    選取模式讀不到任何項目時**中止**，不是「當成全部」——那會把整個
    儲存區的 model 全部匯出一遍，而使用者只是忘了框選。
    """
    if not config.is_selection_mode:
        return config.models, None
    try:
        selected = ops.selected_models()
    except uia.UiaError as exc:
        return (), (
            f"讀不到 AccuMark Explorer 的選取狀態：{exc}\n"
            '若這台機器的清單不支援讀取選取狀態，請把 config.json 的 models '
            '改成明確清單，例如 ["A-1234", "A-1235"]。'
        )
    if not selected:
        return (), (
            "AccuMark Explorer 裡沒有選取任何 model。\n"
            "請先框選要處理的 model 再跑一次。"
        )
    return tuple(selected), None


# ── dry-run ──────────────────────────────────────────────────────────


def run_dry(config, echo) -> int:
    """只定位、不操作（TD-10）。"""
    echo("AccuMark 批次匯出 — 控制項檢查（dry-run）")
    echo("=" * 56)
    echo("")
    echo("只檢查介面上的控制項找不找得到，不會匯出任何東西、不碰任何檔案。")

    groups = ["explorer"] + (["dcu"] if config.dxf_formats else [])
    results = dryrun.check_controls(
        config.controls,
        groups,
        find_window_fn=lambda spec: uia.find_window_by_spec(spec, timeout_sec=5.0),
        resolve_fn=lambda window, spec: uia.resolve(window, spec, timeout_sec=1.0),
    )
    for line in dryrun.format_results(results):
        echo(line)

    # 控制項找得到，不代表讀得出「使用者框選了哪些」——那是另一個 pattern，
    # 而它決定日常操作的形狀（框選再雙擊 vs 每次編輯設定檔）。
    # 少了這一問，使用者會在 2e 全綠之後跑批次才撞到，然後得再跑一趟。
    model_list = next(
        (r for r in results if r.group == "explorer" and r.name == "model_list" and r.found),
        None,
    )
    if model_list is not None:
        try:
            window = uia.find_window_by_spec(
                config.controls.explorer["window"], timeout_sec=5.0
            )
            ctrl = uia.resolve(window, config.controls.explorer["model_list"], 1.0)
            for line in dryrun.format_selection(
                dryrun.check_selection(ctrl, uia.read_selection)
            ):
                echo(line)
        except Exception as exc:  # noqa: BLE001 — 輔助資訊，壞掉不影響上面的結論
            echo("")
            echo(f"（順帶一提：想確認選取狀態時出了狀況——{exc}）")

    return dryrun.exit_code(results)


# ── 主流程 ───────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_stdout()
    args = parse_args(argv)
    echo = print

    try:
        config = cfg.load(CONFIG_PATH)
    except cfg.ConfigError as exc:
        echo("設定檔有問題，還沒開始就停下來了：")
        echo("")
        echo(f"  {exc}")
        echo("")
        echo(f"設定檔位置：{CONFIG_PATH}")
        return 2

    ops = ops_mod.UiaOps(config)

    if args.dry_run:
        return run_dry(config, echo)

    echo("AccuMark 批次匯出")
    echo("=" * 56)
    echo("")

    # ── 開始之前的檢查。任何一項失敗都不會在桌面留下任何東西。──
    if is_session_locked():
        echo("工作階段已鎖定，AccuMark 的視窗現在讀不到。")
        echo("請解鎖後再跑。螢幕可以關，但不能鎖。")
        return 3

    try:
        ops.connect()
    except uia.WindowNotFoundError as exc:
        echo(f"找不到需要的視窗：{exc}")
        echo("")
        echo("請確認 AccuMark Explorer（以及 DXF 需要的 Data Conversion Utility）")
        echo("都開著且沒有最小化，再跑一次。")
        echo("若確定開著卻仍找不到，請雙擊 2e_確認控制項.bat 看是哪一項對不上。")
        return 3

    models, problem = resolve_models(config, ops)
    if problem:
        echo(problem)
        return 3

    problem = check_model_names(models)
    if problem:
        echo(problem)
        return 3

    problem = check_temp_dir(config.paths.temp_dir, list_files)
    if problem:
        echo(problem)
        return 3

    tasks = orch.plan_tasks(models, config.formats, args.only, args.only_format)
    if not tasks:
        echo("沒有任何任務符合條件。")
        echo(f"（讀到的 model：{list(models)}；格式：{list(config.formats)}）")
        return 1

    # ── 狀態與輸出位置 ──
    now = datetime.now()
    run_id = now.strftime("%y%m%d_%H%M%S")
    previous = runstate.load(runstate.state_path(SCRIPTS_DIR))
    output_dir = (
        runstate.resume_output_dir(previous)
        if previous is not None and not args.force
        else None
    ) or archival.resolve_output_dir(
        config.archival.output_dir_pattern, config.paths.output_root, now
    )

    started = runstate.start(SCRIPTS_DIR, run_id, output_dir, models, force=args.force)
    state = started.state
    if started.resumed:
        echo(f"接續上次的批次（{state.run_id}），已完成的會自動跳過。")
    if started.archived_to:
        echo(f"上次的進度已封存到 {started.archived_to}")

    echo(f"要處理的 model：{list(models)}")
    echo(f"格式：{list(config.formats)}　共 {len(tasks)} 個任務")
    echo(f"輸出資料夾：{output_dir}")
    echo("")
    echo("開始執行。這段期間你可以繼續用這台電腦做別的事——")
    echo("腳本不會移動滑鼠，但**不要關掉或最小化 AccuMark**。")
    echo("")

    Path(config.paths.temp_dir).mkdir(parents=True, exist_ok=True)

    ctx = orch.RunContext(
        temp_dir=Path(config.paths.temp_dir),
        output_dir=Path(output_dir),
        ops=ops,
        expected_outputs=config.expected_outputs,
        completion_title_like=config.zip.complete_dialog.title_like,
        dialog_rules=config.dialog_whitelist,
        poll_interval_ms=config.detection.poll_interval_ms,
        stable_samples=config.detection.stable_samples,
        quiet_period_sec=config.detection.quiet_period_sec,
        timeout_sec=config.detection.timeout_sec,
        add_format_suffix=config.archival.add_format_suffix,
        clock_fn=__import__("time").monotonic,
        sleep_fn=__import__("time").sleep,
        now_fn=datetime.now,
    )

    state_file = runstate.state_path(SCRIPTS_DIR)

    def remember(record):
        nonlocal state
        state = runstate.mark(state, record)
        runstate.save(state_file, state)
        echo(f"  {record.status.value:<22}{record.model} / {record.fmt}")

    records = orch.run_batch(
        tasks,
        ctx,
        now_fn=datetime.now,
        should_skip_fn=lambda t: runstate.should_skip(
            state, t.model, t.fmt, lambda p: Path(p).exists()
        ),
        on_result=remember,
    )

    for line in reporting.format_summary(reporting.summarize(records)):
        echo(line)

    log = runstate.log_path(SCRIPTS_DIR, state.run_id)
    code = reporting.write_log(log, records)
    echo("")
    echo(f"完整日誌：{log}")
    echo(f"產出位置：{output_dir}")
    return code


if __name__ == "__main__":
    sys.exit(main())
