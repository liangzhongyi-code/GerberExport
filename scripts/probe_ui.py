"""
期一：AccuMark UI 控制項探測。

在拿到 AccuMark 真實的控制項結構之前，期二的每一行都只能靠猜（TD-2）。
這支腳本走訪 UI 樹、把結果寫成一份可攜回的 JSON 報告。

流程控制與探測本身刻意分開：run() 把探測、列視窗、寫檔、時鐘都當參數收，
因此「AccuMark 沒開」「pywinauto 沒裝」這些情境不用真的製造就能測到腳本
的反應。

最要緊的一條規則：**找不到目標時絕不產生報告檔。** 一份看起來正常、其實
什麼都沒探到的報告，會讓整個期二建立在錯誤的假設上——那比直接失敗糟糕
得多。
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from check_env import configure_stdout
from lib import uia

DEFAULT_TITLE = "AccuMark.*"
OUTPUT_DIRNAME = "probe-output"
MAX_CANDIDATES = 40


# ── 純函式 ───────────────────────────────────────────────────────────


def report_path(out_dir: Path, mode: str, when: datetime) -> Path:
    """
    報告檔名帶模式與時間戳。

    模式要進檔名：主視窗與對話框是分兩次探測的，同名會讓第二次蓋掉第一次，
    而使用者要把兩份都帶回來。
    """
    return Path(out_dir) / ("probe_%s_%s.json" % (mode, when.strftime("%y%m%d_%H%M%S")))


def default_output_dir() -> Path:
    """報告落在腳本旁邊，使用者照著畫面上的路徑就找得到。"""
    return Path(__file__).resolve().parent / OUTPUT_DIRNAME


def format_summary_lines(report) -> list:
    """
    畫面上的摘要。使用者不會打開 JSON 看，關鍵結論要直接講出來——
    尤其 selection_readable，它決定 config.json 的 models 能不能用
    SELECTED 模式。
    """
    s = report.summary
    lines = [
        "探測目標：%s" % (report.target or "(未指定)"),
        "節點數：%d　最大深度：%d" % (s.total_nodes, s.max_depth),
        "定位策略分佈：%s"
        % "　".join("%s=%d" % (k, v) for k, v in sorted(s.strategy_counts.items())),
    ]
    if s.unstable_count:
        lines.append(
            "無法穩定定位的控制項：%d 個（詳見報告的 unstable_paths）" % s.unstable_count
        )
    if s.truncated_count:
        lines.append("因深度上限而未展開的節點：%d 個" % s.truncated_count)
    lines.append(
        "選取狀態可讀取：%s" % ("是" if s.selection_readable else "否")
    )
    if s.selection_hint:
        lines.append("　└ %s" % s.selection_hint)
    return lines


def pick_best_selection(candidates: Sequence[tuple]):
    """
    純函式：從清單類控制項的候選中挑出最可能是 model 清單的那一個。

    候選是 `(路徑, 控制項型別, SelectionProbe)`。排序理由：

      1. 曝光了選取狀態**且真的讀到項目** —— 使用者照指示框選過，這就是它
      2. 曝光了選取狀態但讀到 0 項 —— 可能是他忘了框選，仍然可用
      3. 其餘

    回傳 `(最佳候選的 SelectionProbe, 最佳候選的路徑)`；沒有任何候選時
    回 `(None, "")`，交給 uia.evaluate_selection 產生「未探測」的判讀。
    """
    if not candidates:
        return None, ""

    def rank(item):
        _, _, probe = item
        if probe.supported and probe.items:
            return 0
        if probe.supported:
            return 1
        return 2

    path, _, probe = min(candidates, key=rank)
    return probe, path


def format_selection_candidates(candidates: Sequence[tuple]) -> list:
    """
    列出**每一個**清單類控制項的選取狀態。

    原本只回報一個布林值，而且問錯了對象（頂層視窗）。列出全部候選之後，
    使用者與後續設定都看得到究竟是哪一個清單可讀——那是決定 `models` 能否
    用 SELECTED 模式的依據。
    """
    if not candidates:
        return [
            "清單類控制項：一個都沒找到。",
            "　└ 可能是深度上限太淺，或這個視窗底下真的沒有清單。"
            "可用 --max-depth 調高再試一次。",
        ]
    lines = ["清單類控制項（決定 models 能不能用 SELECTED 模式）："]
    for path, ctype, probe in candidates:
        if probe.supported and probe.items:
            state = "可讀取，目前選了 %d 項：%s" % (
                len(probe.items),
                "、".join(probe.items[:5]) + ("…" if len(probe.items) > 5 else ""),
            )
        elif probe.supported:
            state = "可讀取，但目前沒有選取任何項目"
        else:
            state = "未曝光選取狀態"
        lines.append("　[%s] %s" % (ctype, state))
        lines.append("　　　%s" % path)
    return lines


def matching_titles(titles: Sequence[str], pattern: str) -> tuple:
    """
    純函式：哪些視窗標題會匹配這個條件。

    用 re.match（從開頭比對）以符合 pywinauto 的 title_re 行為。
    pattern 不合法時回空 tuple——使用者可能打錯正規表示式，那不該
    讓整支腳本崩潰，而且這只是輔助檢查。
    """
    try:
        rx = re.compile(pattern)
    except re.error:
        return ()
    return tuple(t for t in titles if rx.match(t))


def format_ambiguity_warning(candidates: Sequence[str]) -> list:
    """
    多個視窗符合同一個條件時，pywinauto 抓到哪一個並不確定。

    這是實測抓到的真實風險：桌面上只要有另一個標題以 AccuMark 開頭的
    視窗（一份說明文件、檔案總管開著的資料夾），探測就可能抓錯對象，
    而報告看起來完全正常。
    """
    if len(candidates) <= 1:
        return []
    lines = [
        "[!] 注意：不只一個視窗符合這個條件，探測抓到的未必是你要的那一個：",
    ]
    for t in candidates:
        lines.append("      %s" % t)
    lines.append("")
    lines.append("    請對照下面的「探測目標」，確認抓到的是 AccuMark 本身。")
    lines.append("    若不是，用更精確的標題重跑，例如：")
    lines.append('        1_執行探測.bat --title "AccuMark Explorer.*"')
    lines.append("")
    return lines


def format_candidates(titles: Sequence[str]) -> list:
    """
    找不到目標時列出畫面上現有的視窗。

    第一次探測時沒人知道 AccuMark 的視窗標題長什麼樣，列出候選，使用者
    才能告訴我們正確的名稱，而不是雙方反覆猜。
    """
    if not titles:
        return ["（目前偵測不到任何有標題的視窗，請確認 AccuMark 沒有最小化）"]
    lines = ["目前畫面上有這些視窗，正確的那個應該在裡面："]
    for t in titles[:MAX_CANDIDATES]:
        lines.append("    %s" % t)
    if len(titles) > MAX_CANDIDATES:
        lines.append("    …另有 %d 個未列出" % (len(titles) - MAX_CANDIDATES))
    lines.append("")
    lines.append("找到正確的名稱後，用它重跑一次，例如：")
    lines.append('    1_執行探測.bat --title "AccuMark.*"')
    return lines


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(
        description="探測 AccuMark 的 UI 控制項結構，產生一份可攜回的報告"
    )
    parser.add_argument(
        "--mode",
        choices=["window", "dialog"],
        default="window",
        help="window＝探測主視窗；dialog＝探測目前開啟的模態對話框",
    )
    parser.add_argument(
        "--title",
        default=DEFAULT_TITLE,
        help="視窗標題的正規表示式。第一次探測時可能要試幾次才對得上",
    )
    parser.add_argument("--class-name", default=None, help="視窗類別名稱（選用）")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=uia.DEFAULT_MAX_DEPTH,
        help="樹的深度上限，避免超大介面掃太久",
    )
    return parser.parse_args(argv)


# ── 流程 ─────────────────────────────────────────────────────────────


def run(
    *,
    mode: str,
    out_dir: Path,
    probe_fn: Callable[[], "uia.ProbeReport"],
    list_windows_fn: Callable[[], Iterable[str]],
    write_fn: Callable[[Path, str], None],
    now: Callable[[], datetime],
    echo: Callable[[str], None],
    title_pattern: str = "",
) -> int:
    """
    跑一次探測，回傳結束碼。

    任何失敗都走同一條路：印出人看得懂的說明、回非零、**不寫任何檔案**。
    """
    try:
        report, selection_candidates = probe_fn()
    except uia.PywinautoMissingError as exc:
        echo("探測無法進行：%s" % exc)
        echo("")
        echo("請先雙擊 0_檢查環境.bat，照畫面指示把 pywinauto 裝起來。")
        return 2
    except uia.WindowNotFoundError as exc:
        echo("探測無法進行：%s" % exc)
        echo("")
        try:
            titles = list(list_windows_fn())
        except Exception:  # noqa: BLE001 — 列候選只是輔助，壞了不該蓋掉原錯誤
            titles = []
        for line in format_candidates(titles):
            echo(line)
        return 3
    except Exception as exc:  # noqa: BLE001 — 不把 traceback 噴在使用者臉上
        echo("探測時發生未預期的問題：%s" % exc)
        echo("")
        echo("請把這整個畫面回報，這通常表示 AccuMark 的介面與預期不同。")
        return 4

    path = report_path(out_dir, mode, now())
    data = uia.report_to_dict(report)
    # 完整候選清單也寫進報告：摘要只印得下重點，但後續要據此填
    # config.controls.model_list，需要每個候選的完整路徑。
    data["selection_candidates"] = [
        {
            "path": cand_path,
            "control_type": ctype,
            "supported": probe.supported,
            "items": list(probe.items),
            "error": probe.error,
        }
        for cand_path, ctype, probe in selection_candidates
    ]
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    write_fn(path, payload + "\n")

    # 警告放在摘要之前：探錯對象時，這是唯一能讓使用者察覺的線索。
    if title_pattern:
        try:
            candidates = matching_titles(list(list_windows_fn()), title_pattern)
        except Exception:  # noqa: BLE001 — 輔助檢查，壞了不影響報告
            candidates = ()
        for line in format_ambiguity_warning(candidates):
            echo(line)

    for line in format_summary_lines(report):
        echo(line)
    echo("")
    for line in format_selection_candidates(selection_candidates):
        echo(line)
    echo("")
    echo("報告已產出：")
    echo("    %s" % path)
    echo("")
    echo("請把這個檔案帶回來（整個 %s 資料夾複製走也可以）。" % OUTPUT_DIRNAME)
    return 0


# ── 進入點 ───────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def _make_probe(args):
    """把命令列參數包成一次探測。實際碰 pywinauto 的只有這裡。"""

    def probe():
        if args.mode == "dialog" and args.title == DEFAULT_TITLE:
            # 對話框模式若沒指定標題，就抓前景視窗。
            # 原本這裡沿用 AccuMark.* ——那會抓到主視窗而不是使用者剛開起來
            # 的匯出對話框，而報告看起來完全正常。
            control = uia.find_foreground_window()
        else:
            control = uia.find_window(
                title_re=args.title,
                class_name=args.class_name,
            )

        # target 記的是「實際抓到誰」而不是「我搜尋了什麼」。
        # 只記搜尋條件的話，探錯對象時報告看起來完全正常。
        label = uia.window_label(control)
        root = uia.walk(control, max_depth=args.max_depth)

        # 對每個清單類控制項各問一次，而不是問頂層視窗。
        # SelectionPattern 是清單才有的東西，問視窗永遠得到「否」。
        candidates = uia.probe_selection_candidates(control, max_depth=args.max_depth)
        selection, _ = pick_best_selection(candidates)

        report = uia.build_report(root, target=label, selection=selection)
        return report, candidates

    return probe


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_stdout()
    args = parse_args(argv)

    print("AccuMark 批次匯出 — UI 控制項探測")
    print("=" * 44)
    print()

    return run(
        mode=args.mode,
        out_dir=default_output_dir(),
        probe_fn=_make_probe(args),
        list_windows_fn=uia.list_top_windows,
        write_fn=_write,
        now=datetime.now,
        echo=print,
        title_pattern=args.title,
    )


if __name__ == "__main__":
    sys.exit(main())
