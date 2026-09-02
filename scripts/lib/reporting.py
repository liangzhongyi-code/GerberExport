"""
任務結果的分類、統計與日誌輸出。

時間一律由呼叫端傳入字串，這個模組不看時鐘——把「現在幾點」擋在門外之後，
剩下的全是可以直接測的純運算。

狀態的兩個屬性（is_problem / aborts_batch）是整套流程的分歧點：
前者決定結束碼，後者決定要不要停下整批。把它們掛在狀態本身而不是散在
主流程的 if 判斷裡，是為了讓「新增一種狀態」時不會漏掉某個分支。
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence, Tuple


class Status(Enum):
    """
    任務結束的八種可能。對應 design.md §4.3。
    """

    SUCCESS = "SUCCESS"
    SKIPPED_ALREADY_DONE = "SKIPPED_ALREADY_DONE"
    SKIPPED_NOT_FOUND = "SKIPPED_NOT_FOUND"
    # TD-9：DCU 觸發前讀回選取，不是恰好該一個 model（例如上次的選取殘留）。
    # 未執行，所以暫存夾沒有東西、也沒有任何檔案被動到。
    FAILED_SELECTION = "FAILED_SELECTION"
    FAILED_TIMEOUT = "FAILED_TIMEOUT"
    FAILED_TARGET_EXISTS = "FAILED_TARGET_EXISTS"
    FAILED_MOVE = "FAILED_MOVE"
    HALTED_UNKNOWN_DIALOG = "HALTED_UNKNOWN_DIALOG"

    @property
    def is_problem(self) -> bool:
        """
        算不算「有問題」，決定結束碼是否非零。

        SKIPPED_NOT_FOUND 刻意算問題：使用者指名要處理的 model 找不到，
        靜默跳過會讓他以為東西都出好了。
        """
        return self not in (Status.SUCCESS, Status.SKIPPED_ALREADY_DONE)

    @property
    def aborts_batch(self) -> bool:
        """
        要不要停下整批。只有兩種：

        FAILED_MOVE           磁碟或權限有問題，後面的任務照樣會失敗
        HALTED_UNKNOWN_DIALOG TD-5：不確定畫面上是什麼，就絕不繼續亂按

        FAILED_SELECTION 刻意不在內：它發生在觸發之前、什麼都沒動，
        下一個 model 重選一次多半就正常，為一次選取殘留停掉整批太浪費。
        """
        return self in (Status.FAILED_MOVE, Status.HALTED_UNKNOWN_DIALOG)

    @property
    def is_skip(self) -> bool:
        return self in (Status.SKIPPED_ALREADY_DONE, Status.SKIPPED_NOT_FOUND)


DESCRIPTIONS = {
    Status.SUCCESS: "匯出並歸檔完成",
    Status.SKIPPED_ALREADY_DONE: "上次已完成，跳過",
    Status.SKIPPED_NOT_FOUND: "在 AccuMark Explorer 中找不到這個 model",
    Status.FAILED_SELECTION: "DCU 選取的不是恰好這一個 model，未執行",
    Status.FAILED_TIMEOUT: "等待逾時，沒有產生檔案",
    Status.FAILED_TARGET_EXISTS: "目的地已有同名檔，為避免覆蓋而取消",
    Status.FAILED_MOVE: "歸檔搬移失敗",
    Status.HALTED_UNKNOWN_DIALOG: "出現白名單以外的視窗，已停止",
}


@dataclass(frozen=True)
class TaskRecord:
    model: str
    fmt: str
    status: Status
    started_at: str
    finished_at: str
    outputs: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class Summary:
    total: int
    succeeded: int
    skipped: int
    failed: int
    aborted: bool
    problems: Tuple[TaskRecord, ...] = ()

    @property
    def exit_code(self) -> int:
        # 一個任務都沒跑通常代表出了問題（例如沒選取任何 model）。
        # 回 0 會讓使用者以為都好了。
        if self.total == 0:
            return 1
        return 1 if self.failed else 0


def summarize(records: Sequence[TaskRecord]) -> Summary:
    """純函式：把逐筆記錄壓成統計。"""
    problems = tuple(r for r in records if r.status.is_problem)
    return Summary(
        total=len(records),
        succeeded=sum(1 for r in records if r.status is Status.SUCCESS),
        skipped=sum(1 for r in records if r.status is Status.SKIPPED_ALREADY_DONE),
        failed=len(problems),
        aborted=any(r.status.aborts_batch for r in records),
        problems=problems,
    )


def format_record(record: TaskRecord) -> str:
    """
    一筆一行，方便用記事本或 grep 掃過去。
    detail 裡的換行會被壓平，否則單行的承諾就破了。
    """
    parts = [
        record.started_at,
        record.finished_at,
        record.status.value,
        record.model,
        record.fmt,
    ]
    if record.outputs:
        parts.append("產出=" + "|".join(record.outputs))
    if record.detail:
        parts.append("說明=" + " ".join(record.detail.split()))
    return "  ".join(parts)


def format_summary(summary: Summary) -> list:
    """純函式：把統計轉成要印給使用者看的文字。"""
    lines = ["", "-" * 56]

    if summary.total == 0:
        lines.append("沒有任何任務被執行。")
        lines.append(
            "若 models 設為 SELECTED，請先在 AccuMark Explorer 中"
            "選取要處理的 model 再重跑。"
        )
        lines.append("-" * 56)
        return lines

    head = "成功 %d / 失敗 %d" % (summary.succeeded, summary.failed)
    if summary.skipped:
        head += "（另有 %d 項上次已完成，本次跳過）" % summary.skipped
    lines.append(head)

    if summary.problems:
        lines.append("")
        lines.append("需要處理的項目：")
        for r in summary.problems:
            detail = " — " + " ".join(r.detail.split()) if r.detail else ""
            lines.append(
                "  %s / %s：%s%s"
                % (r.model, r.fmt, DESCRIPTIONS.get(r.status, r.status.value), detail)
            )

    if summary.aborted:
        lines.append("")
        lines.append("整批已中止，後面的任務沒有執行。")
        lines.append("排除問題之後直接重跑即可，已完成的項目會自動跳過。")

    lines.append("-" * 56)
    return lines


def build_report(records: Sequence[TaskRecord]) -> list:
    """逐筆記錄 + 摘要，日誌與主控台共用同一份內容。"""
    lines = [format_record(r) for r in records]
    lines.extend(format_summary(summarize(records)))
    return lines


def write_log(path, records: Sequence[TaskRecord]) -> int:
    """
    寫出日誌，回傳結束碼（呼叫端直接拿去用，不用自己再算一次）。

    一律 UTF-8：中文 model 名稱與錯誤訊息必須能被記事本正確開啟。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(build_report(records)) + "\n", encoding="utf-8")
    return summarize(records).exit_code
