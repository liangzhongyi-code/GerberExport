"""
續跑狀態：記住哪些任務已經真的做完了。

核心規則：**狀態檔說成功還不夠，產出檔案要真的還在。**
只信狀態檔的話，使用者手動清掉輸出資料夾之後重跑，會拿到一個什麼都沒做
卻宣稱成功的批次——比重做一次糟糕得多。

存在性檢查以 exists_fn 注入，所以「狀態檔說成功但檔案被刪掉了」這種情境
不用真的去刪檔案就能測。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence, Tuple

from .reporting import Status, TaskRecord

Key = Tuple[str, str]  # (model, format)


@dataclass(frozen=True)
class RunState:
    """只記錄「已完成且有產出」的任務。失敗的沒有記住的價值。"""

    completed: Mapping[Key, Tuple[str, ...]]

    def outputs_of(self, model: str, fmt: str) -> Tuple[str, ...]:
        return self.completed.get((model, fmt), ())


EMPTY = RunState(completed=MappingProxyType({}))


def should_skip(
    state: RunState,
    model: str,
    fmt: str,
    exists_fn: Callable[[str], bool],
) -> bool:
    """
    這個任務可以跳過嗎？三個條件缺一不可：

      1. 狀態檔記得它成功過
      2. 它確實留下了產出（成功卻沒有任何產出很可疑，寧可重做）
      3. 每一個產出現在都還在
    """
    outputs = state.outputs_of(model, fmt)
    if not outputs:
        return False
    return all(exists_fn(p) for p in outputs)


def merge(previous: RunState, records: Sequence[TaskRecord]) -> RunState:
    """
    純函式：把本次的結果併進舊狀態，回傳新的 RunState。

    只有 SUCCESS 會被記錄。SKIPPED_ALREADY_DONE 保留舊記錄不動——
    否則跑第三次時，第二次被跳過的任務又會被重做。
    """
    merged = dict(previous.completed)
    for r in records:
        if r.status is Status.SUCCESS and r.outputs:
            merged[(r.model, r.fmt)] = tuple(r.outputs)
    return RunState(completed=MappingProxyType(merged))


# ── 讀寫 ─────────────────────────────────────────────────────────────


def _to_json(state: RunState, run_id: str) -> dict:
    tasks = [
        {"model": model, "format": fmt, "outputs": list(outputs)}
        for (model, fmt), outputs in sorted(state.completed.items())
    ]
    return {"run_id": run_id, "tasks": tasks}


def _from_json(data) -> RunState:
    """
    形狀不對就當作空狀態。這裡刻意寬容：最壞情況只是多做一次工，
    總比讓使用者卡在原地好。
    """
    if not isinstance(data, dict):
        return EMPTY
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return EMPTY

    completed = {}
    for item in tasks:
        if not isinstance(item, dict):
            continue
        model, fmt = item.get("model"), item.get("format")
        outputs = item.get("outputs")
        if not isinstance(model, str) or not isinstance(fmt, str):
            continue
        if not isinstance(outputs, list):
            continue
        completed[(model, fmt)] = tuple(str(p) for p in outputs)
    return RunState(completed=MappingProxyType(completed)) if completed else EMPTY


def load(path, force: bool = False) -> RunState:
    """
    讀取狀態檔。

    force 時直接回空狀態，但**不刪檔**——重跑期間若又中斷，舊狀態還在
    總比什麼都沒有好。真正的覆寫發生在本次跑完存檔的時候。
    """
    if force:
        return EMPTY
    p = Path(path)
    if not p.is_file():
        return EMPTY
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return EMPTY
    return _from_json(data)


def save(path, state: RunState, run_id: str) -> None:
    """
    寫出狀態檔。縮排並保留中文原字，使用者可能想自己打開看，
    或手動刪掉某一筆讓它重做。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(_to_json(state, run_id), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
