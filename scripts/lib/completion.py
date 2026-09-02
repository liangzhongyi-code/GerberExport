"""
判斷一次匯出「真的」結束了——先等 AccuMark 說做完，再看檔案不動。

stability 只回答一個問題：檔案寫完了沒。它是啟發式的——沒有任何有限的
觀察期能對抗任意長的停頓。審查實測抓到的兩個提前判定缺陷（`.dxf` 穩定後
`.rul` 才出現、寫入暫停跨越取樣窗）根源都一樣：**太早開始算**。

TD-4 修訂後改成兩道關卡（選項 D）：
  1. 先等 UI 完成訊號——ZIP 是「Process Complete」對話框，DXF 是 Results
     窗格有結果或暫存夾檔案數達預期。這一步回答「該從什麼時候開始算」。
  2. 訊號到了才呼叫 stability.wait_for_stable。這一步回答「訊號出現與 flush
     之間的縫」，那段很短，但搬到半個檔案的代價太高。
  3. 穩定之後若檔案數仍少於預期（expected_count），代表 AccuMark 還沒開始寫
     下一個，回到第 2 步繼續等。

三道關卡共用**同一份**逾時預算 timeout_sec。設定檔寫 5 分鐘就是 5 分鐘，
不會因為訊號晚到而變成 10 分鐘。

訊號、取樣、睡眠、時鐘、中止全部注入，這一層不碰 pywinauto、不碰檔案系統，
所以「訊號在第 250 秒才到」可以在測試裡幾毫秒內確定性地驗完。
"""

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from . import stability

Snapshot = stability.Snapshot

COMPLETE = "complete"
TIMEOUT = "timeout"
ABORTED = "aborted"


@dataclass(frozen=True)
class CompletionResult:
    """
    status：COMPLETE / TIMEOUT / ABORTED。
    signal_seen：訊號到底來了沒——逾時時用來分辨「訊號沒來」與「來了但檔案沒齊」。
    files：COMPLETE 時是可歸檔的產出；TIMEOUT 時是暫存夾殘留（呼叫端要搬走，
           不是刪）；ABORTED 時一律為空，中止代表沒有可信的產出。
    elapsed_sec：從開始等訊號起算的總時間。
    reason：給人看的一句話。
    """

    status: str
    signal_seen: bool
    files: Tuple[str, ...]
    elapsed_sec: float
    reason: str


def count_signal(
    sample_fn: Callable[[], Snapshot], expected_count: int
) -> Callable[[], bool]:
    """
    DXF `completion: "files"` 模式的訊號：暫存夾檔案數達預期就算 AccuMark 說做完了。

    多於預期也算到——多出來的檔案是歸檔層主檔名防線（`_未歸類\\`）的事，
    這裡只管「齊了沒」。每次呼叫都重新取樣，訊號是活的。
    """
    if expected_count < 1:
        raise ValueError(
            f"expected_count 至少要 1，目前是 {expected_count}。"
            "預期 0 個檔案等於什麼都不用等，那是設定填錯"
        )

    def signal_fn() -> bool:
        return len(sample_fn()) >= expected_count

    return signal_fn


def _validate(
    *,
    poll_interval_ms: int,
    stable_samples: int,
    quiet_period_sec: float,
    timeout_sec: float,
    expected_count: Optional[int],
) -> None:
    """
    開跑前就把明顯不合理的值擋掉。

    stable_samples 的下限 stability 自己也會檢查，但它要到第二階段才被呼叫——
    訊號還沒來之前可能已經白等了五分鐘，才發現參數根本不合法。
    """
    if poll_interval_ms <= 0:
        raise ValueError(
            f"poll_interval_ms 必須大於 0，目前是 {poll_interval_ms}。"
            "沒有間隔的輪詢是緊迴圈"
        )
    if stable_samples < stability.MIN_REQUIRED_SAMPLES:
        raise ValueError(
            f"stable_samples 至少要 {stability.MIN_REQUIRED_SAMPLES}，"
            f"目前是 {stable_samples}。只比對一次等於沒有穩定判定"
        )
    if quiet_period_sec < 0:
        raise ValueError(f"quiet_period_sec 不能是負數，目前是 {quiet_period_sec}")
    if timeout_sec <= 0:
        raise ValueError(f"timeout_sec 必須大於 0，目前是 {timeout_sec}")
    if expected_count is not None and expected_count < 1:
        raise ValueError(
            f"expected_count 至少要 1（或 None 表示不檢查數量），目前是 {expected_count}"
        )


def wait_for_completion(
    *,
    signal_fn: Callable[[], bool],
    sample_fn: Callable[[], Snapshot],
    sleep_fn: Callable[[float], None],
    clock_fn: Callable[[], float],
    abort_fn: Optional[Callable[[], bool]] = None,
    expected_count: Optional[int] = None,
    poll_interval_ms: int,
    stable_samples: int,
    quiet_period_sec: float = 0.0,
    timeout_sec: float,
) -> CompletionResult:
    """
    先等訊號，訊號到了才算穩定，穩定了還要數量夠。

    ## 第一階段：等 signal_fn() 為 True

    每 poll_interval_ms 問一次。**在這之前絕對不碰 stability**——這是 TD-4
    修訂的全部意義，提前開始算就會把 C1 修好的洞重新打開。

    ## 第二階段：stability.wait_for_stable

    傳給它的 timeout_sec 是**剩餘**預算，不是完整的 timeout_sec。

    ## 第三階段：數量檢查

    expected_count 不是 None 且穩定後的檔案數少於它 → 視為 AccuMark 還沒
    開始寫下一個檔案，回到第二階段繼續等（同一份預算）。等於或多於 → 完成。

    ## 中止

    abort_fn() 為 True 時任何階段都立刻回 ABORTED，不回報檔案。第一階段的
    中止檢查排在訊號之前——未知對話框可能就是在匯出觸發的瞬間彈出來的，
    守衛要比完成對話框優先。第二階段的中止由 stability 自己在每次取樣前檢查。

    ## 逾時

    任何階段逾時都回 TIMEOUT，並帶上當下暫存夾裡的檔名。呼叫端要把它們搬到
    `_逾時殘留\\`，不是刪——那可能是寫到一半的檔，也可能是完整但訊號沒來的檔，
    這裡分不出來，所以一律保留給人看。
    """
    _validate(
        poll_interval_ms=poll_interval_ms,
        stable_samples=stable_samples,
        quiet_period_sec=quiet_period_sec,
        timeout_sec=timeout_sec,
        expected_count=expected_count,
    )

    started = clock_fn()
    interval = poll_interval_ms / 1000.0

    def elapsed() -> float:
        return clock_fn() - started

    def residue() -> Tuple[str, ...]:
        return tuple(sorted(sample_fn()))

    def timed_out(signal_seen: bool, detail: str) -> CompletionResult:
        files = residue()
        return CompletionResult(
            status=TIMEOUT,
            signal_seen=signal_seen,
            files=files,
            elapsed_sec=elapsed(),
            reason=f"逾時 {timeout_sec} 秒：{detail}；暫存夾殘留 {len(files)} 個檔案，須搬走勿刪",
        )

    def aborted(signal_seen: bool, phase: str) -> CompletionResult:
        return CompletionResult(
            status=ABORTED,
            signal_seen=signal_seen,
            files=(),
            elapsed_sec=elapsed(),
            reason=f"{phase}被守衛中止，不回報任何產出",
        )

    # ── 第一階段：等訊號 ────────────────────────────────────────────
    while True:
        if abort_fn is not None and abort_fn():
            return aborted(False, "等待完成訊號時")
        if signal_fn():
            break
        if elapsed() >= timeout_sec:
            return timed_out(False, "完成訊號未出現")
        sleep_fn(interval)

    # ── 第二／第三階段：訊號到了，才看檔案 ─────────────────────────
    # last_stable_count 記住最近一次「穩定但沒齊」時的檔案數，逾時訊息才能
    # 說清楚是「沒齊（1/2）」而不是籠統的「未穩定」。
    last_stable_count: Optional[int] = None

    while True:
        remaining = timeout_sec - elapsed()
        if remaining <= 0:
            return timed_out(True, _short_or_unstable(last_stable_count, expected_count))

        result = stability.wait_for_stable(
            sample_fn=sample_fn,
            sleep_fn=sleep_fn,
            clock_fn=clock_fn,
            poll_interval_ms=poll_interval_ms,
            stable_samples=stable_samples,
            timeout_sec=remaining,
            quiet_period_sec=quiet_period_sec,
            abort_fn=abort_fn,
        )

        if result.reason == "aborted":
            return aborted(True, "等待檔案穩定時")
        if not result.stable:
            return timed_out(True, _short_or_unstable(last_stable_count, expected_count))

        if expected_count is not None and len(result.files) < expected_count:
            # 訊號來了、檔案也不動了，但數量不夠：AccuMark 還沒開始寫下一個。
            # 睡一個間隔再回頭，避免在同一瞬間重複取樣。
            last_stable_count = len(result.files)
            sleep_fn(interval)
            continue

        return CompletionResult(
            status=COMPLETE,
            signal_seen=True,
            files=result.files,
            elapsed_sec=elapsed(),
            reason=_complete_reason(len(result.files), expected_count),
        )


def _short_or_unstable(last_stable_count: Optional[int], expected_count: Optional[int]) -> str:
    if last_stable_count is not None:
        return f"完成訊號已到，但檔案數未齊（{last_stable_count}/{expected_count}）"
    return "完成訊號已到，但檔案未穩定"


def _complete_reason(count: int, expected_count: Optional[int]) -> str:
    if expected_count is None:
        return f"完成訊號已到，{count} 個檔案已穩定"
    return f"完成訊號已到，{count} 個檔案已穩定（預期 {expected_count}）"
