"""
判斷一次匯出到底寫完了沒有。

這是全專案最容易造成**靜默資料損毀**的地方：判定太早就會搬走一個寫到
一半的檔案，而且當下不會有任何錯誤訊息——使用者要等到工廠打不開檔案
才會發現。所以這裡的預設一律偏向「還沒好」。

做法（TD-4）：不用固定等待秒數，而是輪詢暫存夾，看檔案大小連續 N 次
取樣是否完全不變。直接觀測我們真正關心的東西——檔案本身——而不是任何
代理指標（進度條、視窗標題）。

取樣、睡眠、時鐘三者以參數注入，因此「大型 model 匯出 45 秒」這種情境
可以在測試裡幾毫秒內驗完，而且完全確定性。
"""

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence, Tuple

# 只取樣一次等於完全沒有穩定判定。設定層已經擋過一次，
# 這裡再擋一次，避免有人直接呼叫函式繞過去。
MIN_REQUIRED_SAMPLES = 2

Snapshot = Mapping[str, int]


@dataclass(frozen=True)
class StabilityResult:
    stable: bool
    files: Tuple[str, ...]
    elapsed_sec: float
    reason: str  # "stable" | "timeout"


def is_stable(samples: Sequence[Snapshot], required: int) -> bool:
    """
    純判定：最後 required 次取樣是否代表「已經寫完」。

    三個條件缺一不可：
      1. 最後 required 次的檔案清單與大小完全相同
      2. 至少有一個檔案（空的暫存夾代表匯出還沒開始，不是「穩定地沒東西」）
      3. 沒有任何檔案是 0 位元組

    第 3 點是針對 AccuMark 可能先建立佔位檔再寫入的情況。把 0 位元組當成
    穩定，會讓腳本搬走一個空檔而且完全不會報錯。
    """
    if required < MIN_REQUIRED_SAMPLES:
        raise ValueError(
            f"required 至少要 {MIN_REQUIRED_SAMPLES}，目前是 {required}。"
            "只比對一次等於沒有穩定判定"
        )
    if len(samples) < required:
        return False

    window = samples[-required:]
    latest = window[-1]

    if not latest:
        return False
    if any(size <= 0 for size in latest.values()):
        return False
    return all(dict(s) == dict(latest) for s in window)


def wait_for_stable(
    *,
    sample_fn: Callable[[], Snapshot],
    sleep_fn: Callable[[float], None],
    clock_fn: Callable[[], float],
    poll_interval_ms: int,
    stable_samples: int,
    timeout_sec: int,
) -> StabilityResult:
    """
    輪詢直到穩定或逾時。

    第一次取樣在睡眠之前——匯出瞬間完成時不該白等一輪。
    逾時的判定基準是「經過的時間」，不是「輪詢次數」，這樣調整
    poll_interval_ms 不會意外改變等待總長。
    """
    started = clock_fn()
    interval = poll_interval_ms / 1000.0
    samples = []

    while True:
        samples.append(dict(sample_fn()))

        if is_stable(samples, stable_samples):
            latest = samples[-1]
            return StabilityResult(
                stable=True,
                files=tuple(sorted(latest)),
                elapsed_sec=clock_fn() - started,
                reason="stable",
            )

        if clock_fn() - started >= timeout_sec:
            return StabilityResult(
                stable=False,
                files=(),
                elapsed_sec=clock_fn() - started,
                reason="timeout",
            )

        sleep_fn(interval)
