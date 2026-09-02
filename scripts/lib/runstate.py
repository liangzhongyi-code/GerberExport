"""
續跑狀態：記住哪些任務已經真的做完了（design.md §4.2）。

狀態檔落在 scripts\\runs\\state.json，形狀是：
  runId、outputDir、models、tasks[]；
  每筆 task 有 format、model、status、startedAt、finishedAt、outputs。

核心規則：**狀態檔說成功還不夠，產出檔案要真的還在。**
只信狀態檔的話，使用者手動清掉輸出資料夾之後重跑，會拿到一個什麼都沒做
卻宣稱成功的批次——比重做一次糟糕得多。

`--force` 的方向以 spec「強制全部重跑」為準：所有任務皆執行、狀態檔被重置。
重置＝把既有 state.json 改名為 state_<舊 runId>.json 保留（不刪）、開新批次；
一般執行才續跑，沿用舊批次的 runId 與 outputDir，補跑的產出落在同一個資料夾。

只有 load／save／start 三個函式碰檔案系統，其餘全是純函式：
存在性檢查以 exists_fn 注入，「該續跑還是重來」由 plan_start 決定。
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from .reporting import Status, TaskRecord

STATE_FILENAME = "state.json"
_SUCCESS = Status.SUCCESS.value


# ── 資料結構 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskEntry:
    """
    狀態檔裡的一筆任務。status 存字串而不是 Status 列舉：§4.3 日後再新增
    狀態時，舊版程式讀到新狀態不該炸——讀不懂的狀態一律當成「沒成功」
    重跑，這是安全方向。
    """

    model: str
    fmt: str
    status: str
    started_at: str = ""
    finished_at: str = ""
    outputs: Tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        """§4.2 的 kind 欄位：ZIP 走 Explorer，其餘走 DCU。由 format 決定。"""
        return "ZIP" if self.fmt == "ZIP" else "DXF"


@dataclass(frozen=True)
class RunState:
    run_id: str
    output_dir: str
    models: Tuple[str, ...]
    tasks: Tuple[TaskEntry, ...] = ()

    def latest(self, model: str, fmt: str) -> Optional[TaskEntry]:
        """
        該 (model, format) 的最新紀錄。mark() 會取代舊紀錄，所以正常只有
        一筆；手動編輯過的檔案可能有重複，那就以最後一筆為準。
        """
        for entry in reversed(self.tasks):
            if entry.model == model and entry.fmt == fmt:
                return entry
        return None


def new_state(run_id: str, output_dir, models: Iterable[str]) -> RunState:
    """一個還沒做任何任務的新批次。output_dir 存字串，才寫得進 JSON。"""
    return RunState(
        run_id=str(run_id),
        output_dir=str(output_dir),
        models=tuple(str(m) for m in models),
    )


# ── 判定（純函式）────────────────────────────────────────────────────


def should_skip(
    state: RunState,
    model: str,
    fmt: str,
    exists_fn: Callable[[str], bool],
) -> bool:
    """
    這個任務可以跳過嗎？三個條件缺一不可：

      1. 最新紀錄的 status 是 SUCCESS（其他狀態一律重跑）
      2. 它確實留下了產出（成功卻沒有任何產出很可疑，寧可重做）
      3. 每一個產出現在都還在
    """
    entry = state.latest(model, fmt)
    if entry is None or entry.status != _SUCCESS:
        return False
    if not entry.outputs:
        return False
    return all(exists_fn(p) for p in entry.outputs)


def mark(state: RunState, record: TaskRecord) -> RunState:
    """
    純函式：記下一筆任務結果，回傳新的 RunState。

    同一個 (model, format) 再記一次時**取代**舊紀錄而不是追加——重做過的
    任務只該剩最新那一筆，位置維持原本的順序，使用者打開檔案不會看到
    同一個任務兩種結論。

    唯一的例外是 SKIPPED_ALREADY_DONE：它代表「上次的 SUCCESS 還有效」，
    要保留那筆 SUCCESS 不動——否則跑第三次時，第二次被跳過的任務又會被重做。
    """
    key = (record.model, record.fmt)
    if record.status is Status.SKIPPED_ALREADY_DONE and state.latest(*key) is not None:
        return state

    entry = TaskEntry(
        model=record.model,
        fmt=record.fmt,
        status=record.status.value,
        started_at=record.started_at,
        finished_at=record.finished_at,
        outputs=tuple(record.outputs),
    )
    tasks = []
    placed = False
    for old in state.tasks:
        if (old.model, old.fmt) == key:
            if not placed:
                tasks.append(entry)
                placed = True
            continue  # 手動編輯造成的重複也一併收成一筆
        tasks.append(old)
    if not placed:
        tasks.append(entry)
    return replace(state, tasks=tuple(tasks))


# ── 落點（純函式，§4.2）──────────────────────────────────────────────
#
# 全部放在交付資料夾自己的地盤 scripts\runs\ 裡。不放輸出資料夾（使用者會
# 整包搬走或刪掉），不放暫存夾（會被清空）。


def runs_dir(scripts_dir) -> Path:
    return Path(scripts_dir) / "runs"


def state_path(scripts_dir) -> Path:
    return runs_dir(scripts_dir) / STATE_FILENAME


def log_path(scripts_dir, run_id: str) -> Path:
    return runs_dir(scripts_dir) / f"日誌_{run_id}.txt"


def force_archive_path(scripts_dir, old_run_id: str) -> Path:
    """--force 時舊 state.json 的去處：改名保留，不刪。"""
    return runs_dir(scripts_dir) / f"state_{old_run_id}.json"


def resume_output_dir(state: Optional[RunState]) -> Optional[Path]:
    """續跑時沿用的輸出資料夾；沒有狀態就回 None，由呼叫端開新的。"""
    if state is None or not state.output_dir:
        return None
    return Path(state.output_dir)


# ── JSON 形狀（純函式）───────────────────────────────────────────────


def to_json(state: RunState) -> dict:
    return {
        "runId": state.run_id,
        "outputDir": state.output_dir,
        "models": list(state.models),
        "tasks": [
            {
                "kind": t.kind,
                "format": t.fmt,
                "model": t.model,
                "status": t.status,
                "startedAt": t.started_at,
                "finishedAt": t.finished_at,
                "outputs": list(t.outputs),
            }
            for t in state.tasks
        ],
    }


def _str_or_empty(value) -> str:
    return value if isinstance(value, str) else ""


def _task_from_json(item) -> Optional[TaskEntry]:
    """一筆任務形狀不對就丟掉這一筆，其餘照收。"""
    if not isinstance(item, dict):
        return None
    fmt = item.get("format")
    if not isinstance(fmt, str) and item.get("kind") == "ZIP":
        fmt = "ZIP"  # §4.2 的 ZIP 範例只有 kind 沒有 format
    model = item.get("model")
    status = item.get("status")
    outputs = item.get("outputs", [])
    if not isinstance(model, str) or not isinstance(fmt, str):
        return None
    if not isinstance(status, str) or not isinstance(outputs, list):
        return None
    return TaskEntry(
        model=model,
        fmt=fmt,
        status=status,
        started_at=_str_or_empty(item.get("startedAt")),
        finished_at=_str_or_empty(item.get("finishedAt")),
        outputs=tuple(str(p) for p in outputs),
    )


def from_json(data) -> Optional[RunState]:
    """
    形狀不對就回 None（沒有狀態）。這裡刻意寬容：最壞情況只是多做一次工，
    總比讓使用者卡在原地好。

    runId 與 outputDir 缺一不可——少了前者不知道要封存成什麼名字，
    少了後者不知道續跑要落在哪個資料夾。
    """
    if not isinstance(data, dict):
        return None
    run_id = data.get("runId")
    output_dir = data.get("outputDir")
    tasks = data.get("tasks")
    if not isinstance(run_id, str) or not run_id:
        return None
    if not isinstance(output_dir, str) or not output_dir:
        return None
    if not isinstance(tasks, list):
        return None
    models = data.get("models")
    if not isinstance(models, list):
        models = []
    entries = [e for e in map(_task_from_json, tasks) if e is not None]
    return RunState(
        run_id=run_id,
        output_dir=output_dir,
        models=tuple(m for m in models if isinstance(m, str)),
        tasks=tuple(entries),
    )


# ── 讀寫 ─────────────────────────────────────────────────────────────


def load(path) -> Optional[RunState]:
    """讀取狀態檔。檔案不存在、壞掉或形狀不對都回 None。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return from_json(data)


def save(path, state: RunState) -> None:
    """
    寫出狀態檔。縮排並保留中文原字，使用者可能想自己打開看，
    或手動刪掉某一筆讓它重做。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(to_json(state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ── 開始一次執行：續跑或 --force ─────────────────────────────────────


@dataclass(frozen=True)
class StartPlan:
    """
    plan_start 的結論：用哪個狀態、是不是續跑、要把哪個舊批次的檔案封存。
    archive_run_id 為 None 代表沒有東西要封存。
    """

    state: RunState
    resumed: bool
    archive_run_id: Optional[str] = None


def plan_start(
    previous: Optional[RunState],
    force: bool,
    run_id: str,
    output_dir,
    models: Iterable[str],
) -> StartPlan:
    """
    純函式：決定這次執行從哪個狀態開始。

    一般執行且有舊狀態 → 續跑，沿用舊狀態（含 runId 與 outputDir）。
    --force 或沒有舊狀態 → 開新批次；有舊狀態就把它的 runId 交出去封存。
    """
    if previous is not None and not force:
        return StartPlan(state=previous, resumed=True)
    return StartPlan(
        state=new_state(run_id, output_dir, models),
        resumed=False,
        archive_run_id=previous.run_id if previous is not None else None,
    )


@dataclass(frozen=True)
class Started:
    state: RunState
    resumed: bool
    archived_to: Optional[Path] = None


def _unused_path(target: Path) -> Path:
    """封存檔已存在就加序號，寧可多一個檔也不能蓋掉舊紀錄。"""
    if not target.exists():
        return target
    n = 2
    while True:
        candidate = target.with_name(f"{target.stem}_{n}{target.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def start(
    scripts_dir,
    run_id: str,
    output_dir,
    models: Iterable[str],
    force: bool = False,
) -> Started:
    """
    讀舊狀態、依 plan_start 決定續跑或重來，並把結果落地。

    重來時（--force 或首次執行）會立刻寫出新的 state.json：outputDir 在第一個
    任務開始前就落地，forced 批次若中途中斷，下一次一般執行接回的是這個
    新批次而不是被封存的舊批次。舊 state.json 改名保留，不刪。
    續跑時不寫任何東西。
    """
    path = state_path(scripts_dir)
    plan = plan_start(load(path), force, run_id, output_dir, models)
    if plan.resumed:
        return Started(state=plan.state, resumed=True)

    archived_to = None
    if plan.archive_run_id is not None and path.is_file():
        archived_to = _unused_path(force_archive_path(scripts_dir, plan.archive_run_id))
        path.rename(archived_to)
    save(path, plan.state)
    return Started(state=plan.state, resumed=False, archived_to=archived_to)
