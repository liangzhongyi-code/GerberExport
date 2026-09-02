"""
UI Automation 樹走訪與定位策略評估（B1、B2）。

這是全專案唯一允許碰 pywinauto 的檔案（TD-3）。UI 互動在開發機上根本跑不
起來——這裡沒有 AccuMark——所以整個專案的策略是把不確定的東西壓縮進這一個
檔案，其餘模組維持純函式並完整 TDD。

但「壓縮進一個檔案」不等於「這個檔案就不用測」。檔案**內部**同樣切成兩段：

  純的一段   ─ 定位策略評估、唯一性判定、UNSTABLE 標記、扁平化、摘要統計、
               選取狀態判讀。吃 UiaNode／dict，吐 dict，不 import pywinauto，
               由 tests/test_locator.py 完整覆蓋。
  不純的一段 ─ 只負責把 pywinauto 的控制項物件翻譯成 UiaNode。薄到幾乎沒有
               判斷邏輯，因此目標機驗收時要看的東西很少。

pywinauto 一律**延遲匯入**（在函式內部才 import）。理由有二：純函式層必須
在沒有安裝 pywinauto 的機器上也能 import，否則 test_locator.py 會整份變成
collection error；而在目標機上缺套件時，使用者該看到的是「請先跑
0_檢查環境.bat」而不是一段 ImportError traceback。

走訪與操作全程只讀屬性、只用 UIA pattern，不動實體游標與鍵盤（由
tests/test_static_scan.py 逐字串守護）。
"""

import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ── 定位策略詞彙 ─────────────────────────────────────────────────────
#
# 前三個必須與 config.VALID_STRATEGIES 逐字相同：使用者是照著探測報告把
# strategy 抄進 config.json 的，兩邊用詞一漂移，他照抄的設定會被設定檔
# 驗證擋下來，而錯誤訊息會指向設定檔——完全指不到真正的原因。
STRATEGY_AUTO_ID = "auto_id"
STRATEGY_NAME = "name"
STRATEGY_INDEX = "index"

# UNSTABLE 刻意不是合法的 config 策略：它代表「這個控制項沒有可靠的定位
# 方式」，是一個要人來處理的結論，不是可以填進設定檔的選項。
STRATEGY_UNSTABLE = "UNSTABLE"

ALL_STRATEGIES: Tuple[str, ...] = (
    STRATEGY_AUTO_ID,
    STRATEGY_NAME,
    STRATEGY_INDEX,
    STRATEGY_UNSTABLE,
)

# 摘要區的 UNSTABLE 清單上限。決策依據是總數，逐條路徑只是樣本——
# 一棵樹可能有上千個無名的 ListItem，全列出來的摘要沒有人會讀。
MAX_LISTED_UNSTABLE = 50

PATH_SEPARATOR = " > "
UNKNOWN_TYPE = "?"


# ── 純資料結構 ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class UiaNode:
    """
    一個控制項節點。欄位對應 spec「控制項樹匯出」列的最小集合。

    全部欄位都有預設值，因為「什麼都讀不到」正是最需要被測到的情況——
    自繪畫布與動態產生的清單項目常常只剩一個 control_type。
    """

    name: str = ""
    automation_id: str = ""
    control_type: str = ""
    class_name: str = ""
    is_enabled: bool = False
    depth: int = 0
    sibling_index: int = 0
    children: Tuple["UiaNode", ...] = ()
    # 因深度上限而沒有再往下走。與「真的沒有子節點」必須分得開，
    # 否則使用者會以為樹就是這麼淺，然後在報告裡找不到他要的控制項。
    truncated: bool = False


@dataclass(frozen=True)
class Locator:
    """對單一控制項的定位建議。value 可直接抄進 config.json 的 controls。"""

    strategy: str
    value: str
    stable: bool
    reason: str


@dataclass(frozen=True)
class NodeReport:
    node: UiaNode
    path: str
    locator: Locator

    @property
    def child_count(self) -> int:
        return len(self.node.children)


@dataclass(frozen=True)
class SelectionProbe:
    """
    不純層對清單控制項讀取選取狀態的原始結果，未經判讀。

    supported 是「控制項曝光了選取狀態」，items 是「真的讀到的項目」。
    兩者刻意分開記錄——它們並不等價，而這正是判讀時的關鍵（見
    evaluate_selection）。
    """

    supported: bool = False
    items: Tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class SelectionReport:
    readable: bool
    items: Tuple[str, ...]
    hint: str


@dataclass(frozen=True)
class ProbeSummary:
    total_nodes: int
    max_depth: int
    truncated_count: int
    unstable_count: int
    strategy_counts: Mapping[str, int]
    selection_readable: bool
    selection_items: Tuple[str, ...]
    selection_hint: str
    unstable_paths: Tuple[str, ...]


@dataclass(frozen=True)
class ProbeReport:
    target: str
    nodes: Tuple[NodeReport, ...]
    selection: SelectionReport
    summary: ProbeSummary


# ── 純函式：序列化 ───────────────────────────────────────────────────


def node_to_mapping(node: UiaNode) -> Dict[str, Any]:
    """把節點連同子樹轉成可 json.dump 的巢狀 dict。"""
    return {
        "name": node.name,
        "automation_id": node.automation_id,
        "control_type": node.control_type,
        "class_name": node.class_name,
        "is_enabled": node.is_enabled,
        "depth": node.depth,
        "sibling_index": node.sibling_index,
        "truncated": node.truncated,
        "children": [node_to_mapping(c) for c in node.children],
    }


def node_from_mapping(data: Mapping[str, Any]) -> UiaNode:
    """
    從報告 JSON 讀回節點樹（D1 在開發機重新評估定位策略時用）。

    這裡刻意寬鬆——與 config.py 的「未知欄位一律拒絕」相反。設定檔拒絕未知
    欄位是為了攔住拼錯的參數；探測報告則是一份會隨版本長出新欄位的紀錄，
    讀舊報告時因為多了一個欄位就整份掛掉，只會讓人放棄用工具去讀它。
    """
    return UiaNode(
        name=str(data.get("name") or ""),
        automation_id=str(data.get("automation_id") or ""),
        control_type=str(data.get("control_type") or ""),
        class_name=str(data.get("class_name") or ""),
        is_enabled=bool(data.get("is_enabled", False)),
        depth=int(data.get("depth", 0)),
        sibling_index=int(data.get("sibling_index", 0)),
        children=tuple(node_from_mapping(c) for c in data.get("children") or ()),
        truncated=bool(data.get("truncated", False)),
    )


# ── 純函式：定位策略評估 ─────────────────────────────────────────────


def _count_matching(nodes: Sequence[UiaNode], getter: Callable[[UiaNode], str], value: str) -> int:
    return sum(1 for n in nodes if getter(n).strip() == value)


def _padding_note(raw: str) -> str:
    """
    值前後帶空白時的警語。

    記事本的狀態列就是這樣：Name 是 "  第 1 列，第 1 行"。空白在報告裡看不
    出來，使用者抄進 config.json 時會很自然地把它修掉，然後定位就找不到東西
    ——而錯誤訊息只會說「找不到控制項」，完全指不到真正的原因。
    """
    return "（注意：這個值前後帶有空白，抄進設定檔時必須一字不差）" if raw != raw.strip() else ""


def _sibling_pool(node: UiaNode, siblings: Sequence[UiaNode]) -> Tuple[UiaNode, ...]:
    """
    確保待評估的節點本身在同層集合裡。

    身分用 sibling_index 判斷而不是物件相等：同層兩個一模一樣的空白按鈕在
    值上完全相等，用 `node in siblings` 會把「同層有兩個」誤判成「同層只有
    我一個」——而那正好是唯一性判定最需要抓到的情況。
    """
    pool = tuple(siblings)
    if any(s.sibling_index == node.sibling_index for s in pool):
        return pool
    return pool + (node,)


def evaluate_locator(node: UiaNode, siblings: Sequence[UiaNode]) -> Locator:
    """
    純函式：這個控制項該怎麼定位。

    優先序 automation_id > name > control_type + 同層索引，理由是穩定性由高
    到低：AutomationId 由開發者指定，改版才會動；Name 通常是顯示文字，換語
    系或改文案就會變；位置則會隨版本與資料筆數改變。

    唯一性只在**同一個父容器**下判斷。拿整棵樹判斷的話，AccuMark 那種每個
    ListItem 都叫 "item" 的樹會讓幾乎所有節點被判成不可用；而實務上定位本來
    就是先找到容器再往下找。

    三者皆不可靠時回傳 UNSTABLE 而不是硬給一個位置。硬給位置的定位「今天能
    跑、明天沉默失效」，而失效的樣子是點到隔壁的控制項——那比直接標紅危險
    太多。

    value 一律輸出**原始字串**，只有「空不空」「唯不唯一」這兩個判斷才看去掉
    前後空白的版本。UIA 的屬性比對是逐字的，報告裡若擅自把空白修掉，抄進設定
    檔的值就定位不到真正的控制項。
    """
    pool = _sibling_pool(node, siblings)

    auto_id = node.automation_id.strip()
    if auto_id and _count_matching(pool, lambda n: n.automation_id, auto_id) == 1:
        return Locator(
            strategy=STRATEGY_AUTO_ID,
            value=node.automation_id,
            stable=True,
            reason=(
                "AutomationId 在同層唯一，是最不受改版與語系影響的定位方式"
                + _padding_note(node.automation_id)
            ),
        )

    name = node.name.strip()
    if name and _count_matching(pool, lambda n: n.name, name) == 1:
        return Locator(
            strategy=STRATEGY_NAME,
            value=node.name,
            stable=True,
            reason=(
                "沒有可用的 AutomationId；Name 在同層唯一。"
                "注意 Name 多半是顯示文字，改文案或換語系就會失效"
                + _padding_note(node.name)
            ),
        )

    control_type = node.control_type.strip()
    same_type = [n for n in pool if n.control_type.strip() == control_type]
    if control_type and len(same_type) == 1:
        position = sorted(s.sibling_index for s in same_type).index(node.sibling_index)
        return Locator(
            strategy=STRATEGY_INDEX,
            value=f"{control_type}#{position}",
            stable=True,
            reason=f"沒有可用的 AutomationId 與 Name，但同層只有這一個 {control_type}",
        )

    if not control_type:
        reason = "連 ControlType 都讀不到，沒有任何可用的定位依據"
    else:
        reason = (
            f"AutomationId 與 Name 皆不可用，同層還有 {len(same_type)} 個 "
            f"{control_type}，只能靠位置——位置會隨版本與資料筆數改變"
        )
    return Locator(strategy=STRATEGY_UNSTABLE, value="", stable=False, reason=reason)


# ── 純函式：樹的扁平化與標註 ─────────────────────────────────────────


def flatten(root: UiaNode) -> Tuple[UiaNode, ...]:
    """深度優先前序展開。葉節點（例如自繪畫布）自然回傳只有它自己的序列。"""
    collected: List[UiaNode] = [root]
    for child in root.children:
        collected.extend(flatten(child))
    return tuple(collected)


def _segment(node: UiaNode) -> str:
    return f"{node.control_type.strip() or UNKNOWN_TYPE}[{node.sibling_index}]"


def annotate(root: UiaNode) -> Tuple[NodeReport, ...]:
    """
    純函式：為整棵樹的每個節點算出路徑與定位建議。

    路徑存在的理由是「指名」：摘要區說有 37 個 UNSTABLE 節點，使用者得有辦法
    在報告裡把它們一個個找出來，否則那個數字只能拿來焦慮。
    """
    reports: List[NodeReport] = []

    def visit(node: UiaNode, siblings: Sequence[UiaNode], parent_path: str) -> None:
        segment = _segment(node)
        path = f"{parent_path}{PATH_SEPARATOR}{segment}" if parent_path else segment
        reports.append(
            NodeReport(node=node, path=path, locator=evaluate_locator(node, siblings))
        )
        for child in node.children:
            visit(child, node.children, path)

    visit(root, (root,), "")
    return tuple(reports)


# ── 純函式：選取狀態判讀 ─────────────────────────────────────────────


_FALLBACK_HINT = "請把 config.json 的 models 改成明確的 model 清單"


def evaluate_selection(probe: Optional[SelectionProbe]) -> SelectionReport:
    """
    純函式：這份清單到底能不能拿來當 `models: "SELECTED"` 的來源。

    關鍵判斷是「曝光 pattern 不等於讀得到」。控制項可能宣稱支援 Selection
    卻永遠回傳空集合（自繪清單、只在內部維護選取狀態的實作都會這樣）。把
    「宣稱支援」當成證據，使用者會設成 SELECTED 模式、框選了四個 model、
    然後跑出零個任務——而摘要只會說「沒有任何任務被執行」，他無從得知是這
    裡出的問題。

    因此 readable 只在**真的讀到項目**時才為真。代價是使用者若忘了先框選就
    探測，會拿到 false；所以那個情況給的是「請先框選再重跑」而不是「不可用」，
    兩者的差別在 hint 講清楚。
    """
    if probe is None:
        return SelectionReport(
            readable=False,
            items=(),
            hint=(
                "沒有指定 model 清單控制項，因此沒有探測選取狀態。"
                "請先在報告中找出清單控制項（ControlType 為 List／DataGrid／Table），"
                "再重跑一次探測"
            ),
        )

    if probe.error:
        return SelectionReport(
            readable=False,
            items=(),
            hint=f"讀取選取狀態時出錯：{probe.error}。SELECTED 模式不可用，{_FALLBACK_HINT}",
        )

    if not probe.supported:
        return SelectionReport(
            readable=False,
            items=(),
            hint=f"這個控制項未曝光選取狀態。SELECTED 模式不可用，{_FALLBACK_HINT}",
        )

    if not probe.items:
        return SelectionReport(
            readable=False,
            items=(),
            hint=(
                "控制項曝光了選取狀態，但目前讀到 0 個項目。"
                "請先在 AccuMark Explorer 框選幾個 model 再重跑探測；"
                f"若框選後仍是 0，代表 SELECTED 模式不可用，{_FALLBACK_HINT}"
            ),
        )

    return SelectionReport(
        readable=True,
        items=tuple(probe.items),
        hint=(
            f"讀到 {len(probe.items)} 個選取項目，"
            "請比對是否與你在 Explorer 框選的完全一致；一致才代表 SELECTED 模式可用"
        ),
    )


# ── 純函式：摘要與報告 ───────────────────────────────────────────────


def summarize(
    nodes: Sequence[NodeReport], selection: SelectionReport
) -> ProbeSummary:
    """純函式：把逐節點的評估壓成一份看得完的摘要。"""
    # 四個策略一律給值，缺鍵會逼報告閱讀端自己處理 KeyError。
    counts = {strategy: 0 for strategy in ALL_STRATEGIES}
    unstable_paths: List[str] = []

    for report in nodes:
        counts[report.locator.strategy] = counts.get(report.locator.strategy, 0) + 1
        if not report.locator.stable:
            unstable_paths.append(report.path)

    return ProbeSummary(
        total_nodes=len(nodes),
        max_depth=max((r.node.depth for r in nodes), default=0),
        truncated_count=sum(1 for r in nodes if r.node.truncated),
        unstable_count=len(unstable_paths),
        strategy_counts=MappingProxyType(counts),
        selection_readable=selection.readable,
        selection_items=selection.items,
        selection_hint=selection.hint,
        unstable_paths=tuple(unstable_paths[:MAX_LISTED_UNSTABLE]),
    )


def build_report(
    root: UiaNode,
    *,
    target: str = "",
    selection: Optional[SelectionProbe] = None,
) -> ProbeReport:
    """純函式：走訪結果 ＋ 選取狀態探測 → 完整報告。"""
    nodes = annotate(root)
    selection_report = evaluate_selection(selection)
    return ProbeReport(
        target=target,
        nodes=nodes,
        selection=selection_report,
        summary=summarize(nodes, selection_report),
    )


def report_to_dict(report: ProbeReport) -> Dict[str, Any]:
    """
    純函式：報告 → 可 json.dump 的 dict（B3 直接拿去寫檔）。

    節點輸出成扁平清單而不是巢狀樹：報告的用途是被人用記事本翻、被 grep 找，
    而每個節點都帶著完整路徑，扁平化不會丟失結構資訊。
    """
    return {
        "target": report.target,
        "summary": {
            "total_nodes": report.summary.total_nodes,
            "max_depth": report.summary.max_depth,
            "truncated_count": report.summary.truncated_count,
            "unstable_count": report.summary.unstable_count,
            "strategy_counts": dict(report.summary.strategy_counts),
            "selection_readable": report.summary.selection_readable,
            "selection_items": list(report.summary.selection_items),
            "selection_hint": report.summary.selection_hint,
            "unstable_paths": list(report.summary.unstable_paths),
        },
        "nodes": [
            {
                "path": r.path,
                "depth": r.node.depth,
                "sibling_index": r.node.sibling_index,
                "name": r.node.name,
                "automation_id": r.node.automation_id,
                "control_type": r.node.control_type,
                "class_name": r.node.class_name,
                "is_enabled": bool(r.node.is_enabled),
                "child_count": r.child_count,
                "truncated": r.node.truncated,
                "locator": {
                    "strategy": r.locator.strategy,
                    "value": r.locator.value,
                    "stable": r.locator.stable,
                    "reason": r.locator.reason,
                },
            }
            for r in report.nodes
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# 以下為不純的部分：唯一會呼叫 pywinauto 的地方
# ══════════════════════════════════════════════════════════════════════

BACKEND = "uia"

# 深度上限的用途不是省時間，是防失控：AccuMark 的樹裡可能有上萬個節點的清單，
# 而探測的目的是拿到定位資訊，不是完整鏡射整個 UI。走不完的部分會被標成
# truncated，使用者看得到，需要時再調高。
DEFAULT_MAX_DEPTH = 12

# 等待視窗出現的秒數。這不是「等匯出寫完」的等待（那由 stability.py 依設定檔
# 輪詢判定），只是找視窗時給 UIA 的一點餘裕。
DEFAULT_WAIT_SEC = 10.0


class UiaError(RuntimeError):
    """UI 自動化層的錯誤。訊息一律寫成使用者看得懂的下一步。"""


class PywinautoMissingError(UiaError):
    """pywinauto 匯入失敗。"""


class WindowNotFoundError(UiaError):
    """找不到目標視窗。"""


def _load_pywinauto():
    """
    延遲匯入。缺套件時給的是「下一步該做什麼」，不是 traceback——
    目標機上看報錯的人是使用者，不是開發者。
    """
    try:
        import pywinauto  # noqa: PLC0415 — 刻意延遲匯入，理由見模組 docstring
    except ImportError as exc:
        raise PywinautoMissingError(
            "找不到 pywinauto。請先雙擊 0_檢查環境.bat，"
            "它會告訴你線上與離線兩種安裝方式"
        ) from exc
    return pywinauto


def _read(getter: Callable[[], Any], default: Any) -> Any:
    """
    讀一個可能會爆的屬性。

    UIA 的屬性讀取是跨行程的 COM 呼叫：控制項在走訪途中被關掉、自繪畫布沒
    實作某個屬性、視窗正在重繪，都會直接丟例外。一個節點讀不到某個欄位，
    不該讓整棵樹的走訪失敗——探測報告拿回一半是沒有用的。
    """
    try:
        value = getter()
    except Exception:  # noqa: BLE001 — COM 的例外型別五花八門，一律當成讀不到
        return default
    return default if value is None else value


def snapshot(control, *, depth: int = 0, sibling_index: int = 0) -> UiaNode:
    """
    不純：把一個 pywinauto 控制項翻譯成 UiaNode（不含子節點）。

    is_enabled 讀不到時取 False。這個欄位在報告裡是給人看的判斷依據，
    「不確定」寫成「可用」會讓人把一個其實按不動的控制項填進設定檔。
    """
    info = _read(lambda: control.element_info, None)
    if info is None:
        info = control

    return UiaNode(
        name=str(_read(lambda: info.name, "")),
        automation_id=str(_read(lambda: info.automation_id, "")),
        control_type=str(_read(lambda: info.control_type, "")),
        class_name=str(_read(lambda: info.class_name, "")),
        is_enabled=bool(_read(lambda: info.enabled, False)),
        depth=depth,
        sibling_index=sibling_index,
    )


def _children(control) -> List[Any]:
    """
    取子控制項。取不到一律當成沒有子節點。

    對應 spec 的 Scenario「自繪畫布無法曝光」：PDS 的版型畫布在 UIA 下是一個
    沒有子節點的控制項，有些實作甚至會在列舉子節點時直接丟 COM 例外。報告
    要能照樣產出——那個「沒有內容」本身就是我們要帶回去的結論。
    """
    kids = _read(lambda: control.children(), None)
    if not kids:
        return []
    return list(kids)


def walk(
    control,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    depth: int = 0,
    sibling_index: int = 0,
) -> UiaNode:
    """
    不純：走訪整棵 UIA 樹，回傳純資料的 UiaNode 樹。

    這一層刻意不做任何判斷——策略評估、統計、選取狀態判讀全部交給上面的純
    函式。它唯一的職責是翻譯，所以目標機驗收時只要確認「樹的形狀對不對」。

    深度上限處理成「照樣去問有沒有子節點、但不往下走」，這樣 truncated 才是
    誠實的：把「到頂了」與「真的是葉節點」混為一談，會讓使用者以為 AccuMark
    的樹只有這麼深，然後找不到控制項也不知道要調參數。
    """
    node = snapshot(control, depth=depth, sibling_index=sibling_index)
    kids = _children(control)

    if depth >= max_depth:
        return replace(node, truncated=bool(kids))

    return replace(
        node,
        children=tuple(
            walk(child, max_depth=max_depth, depth=depth + 1, sibling_index=index)
            for index, child in enumerate(kids)
        ),
    )


def _no_pattern_error_types() -> tuple:
    """
    pywinauto 用來表示「這個控制項沒有實作該 pattern」的例外型別。

    抓不到型別時回傳空 tuple，效果是全部落到一般錯誤分支——比誤判成「沒有
    pattern」保守，也不會因為 pywinauto 改了模組路徑就整支探測掛掉。
    """
    try:
        from pywinauto.uia_defines import NoPatternInterfaceError  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ()
    return (NoPatternInterfaceError,)


def _describe_exception(exc: BaseException) -> str:
    """例外訊息常常是空的（NoPatternInterfaceError 就是），只留型別名比較好讀。"""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def read_selection(control) -> SelectionProbe:
    """
    不純：讀清單控制項目前選取了哪些項目。

    只讀不寫：走的是 UIA 的 Selection pattern，不碰實體滑鼠鍵盤，因此使用者
    在 Explorer 裡的選取狀態不會被這支探測改掉。

    「沒有 Selection pattern」與「有 pattern 但讀失敗」必須分開回報。前者是
    spec 的 Scenario「清單不支援讀取選取項」，結論明確——改用明確 model 清單；
    後者是偶發故障，值得重試一次再下結論。兩者都塞進 error 欄位的話，報告會
    對第一種情況印出一行沒有訊息的例外型別名，使用者看不出那其實是個確定的
    答案。

    任何例外都轉成欄位而不是往上拋：探測失敗本身就是要帶回去的資訊（它直接
    決定 `models: "SELECTED"` 能不能用），讓整支腳本掛掉反而什麼都不知道。
    """
    try:
        selected = control.get_selection()
    except _no_pattern_error_types():
        return SelectionProbe(supported=False)
    except Exception as exc:  # noqa: BLE001 — COM 的失敗型別五花八門
        return SelectionProbe(supported=False, error=_describe_exception(exc))

    return SelectionProbe(
        supported=True,
        items=tuple(_element_label(element) for element in selected or ()),
    )


def _element_label(element) -> str:
    """選取項的顯示名稱。Name 空的清單項目退回 rich_text，兩個都空就給空字串。"""
    name = str(_read(lambda: element.name, "")).strip()
    if name:
        return name
    return str(_read(lambda: element.rich_text, "")).strip()


def find_window(
    *,
    title_re: Optional[str] = None,
    class_name: Optional[str] = None,
    process: Optional[int] = None,
    path: Optional[str] = None,
    backend: str = BACKEND,
    timeout: float = DEFAULT_WAIT_SEC,
):
    """
    不純：找到要探測的頂層視窗，回傳 pywinauto 的控制項物件。

    找不到時丟 WindowNotFoundError 而不是回傳 None。對應 spec 的 Scenario
    「目標程序未啟動」：腳本必須以非零結束碼終止並說清楚原因，MUST NOT 產生
    一份空白或誤導性的報告檔——一份看起來正常但其實沒探到東西的報告，會讓
    整個期二依著錯誤的假設寫下去。
    """
    pywinauto = _load_pywinauto()

    if path is not None:
        try:
            app = pywinauto.Application(backend=backend).connect(
                path=path, timeout=timeout
            )
            return app.top_window().wrapper_object()
        except Exception as exc:  # noqa: BLE001 — pywinauto 的失敗型別不只一種
            raise WindowNotFoundError(
                f"連不到程式 {path}：{exc}。請先確認該程式正在執行且視窗沒有最小化"
            ) from exc

    criteria = {
        key: value
        for key, value in (
            ("title_re", title_re),
            ("class_name", class_name),
            ("process", process),
        )
        if value is not None
    }
    if not criteria:
        raise UiaError(
            "find_window 至少要給一個條件（title_re／class_name／process／path），"
            "否則會抓到不確定是哪一個的視窗"
        )

    try:
        spec = pywinauto.Desktop(backend=backend).window(**criteria)
        spec.wait("exists ready", timeout=timeout)
        return spec.wrapper_object()
    except Exception as exc:  # noqa: BLE001
        raise WindowNotFoundError(
            f"找不到符合 {criteria} 的視窗：{exc}。"
            "請確認目標程式正在執行、視窗沒有最小化，且條件沒有打錯"
        ) from exc


SELECTION_CONTROL_TYPES = ("List", "DataGrid", "Table", "Tree", "ListView")


def foreground_handle() -> int:
    """
    目前最前面那個視窗的控制代碼。

    直接問 user32，不走 pywinauto 的內部模組——原本這裡用的是
    `pywinauto.win32functions.GetForegroundWindow`，而 0.6.9 裡**沒有**
    這個屬性（實測 AttributeError）。四支對話框探測腳本全部走這條路，
    等於整個交付點 1 的主要內容在目標機上跑不起來。

    屬性是執行期才解析的，靜態掃描看不到；把 pywinauto 換成假物件也測
    不到——假物件有沒有這個屬性都不代表真的那個有。所以改用一個不會變
    的東西：GetForegroundWindow 是 Win32 API 的一部分，比任何套件穩定。

    鎖屏或切換桌面時會回 0，呼叫端要當成「抓不到」。
    """
    import ctypes  # noqa: PLC0415

    return int(ctypes.windll.user32.GetForegroundWindow())


def find_foreground_window(backend: str = BACKEND, handle_fn=None):
    """
    不純：抓目前最前面的那個視窗。

    對話框模式需要這個。原本 `--mode dialog` 只改變「要不要讀選取狀態」，
    視窗搜尋條件仍然是 `AccuMark.*` —— 於是它抓到的是主視窗，不是使用者
    剛剛開啟的匯出對話框，而報告看起來完全正常。

    使用者被指示「先把對話框開起來再跑」，所以前景視窗就是它。

    handle_fn 可注入，讓「抓不到前景視窗」這條錯誤路徑不必真的去弄掉
    桌面上的視窗才測得到。
    """
    pywinauto = _load_pywinauto()
    get_handle = handle_fn or foreground_handle
    handle = get_handle()
    if not handle:
        raise WindowNotFoundError(
            "抓不到前景視窗，請確認對話框開著且停在最上層"
        )
    try:
        return pywinauto.Desktop(backend=backend).window(handle=handle).wrapper_object()
    except Exception as exc:  # noqa: BLE001
        raise WindowNotFoundError(
            f"抓不到前景視窗：{exc}。請先把匯出對話框開起來，讓它停在最上層"
        ) from exc


def probe_selection_candidates(control, max_depth: int = DEFAULT_MAX_DEPTH):
    """
    不純：走訪樹，對**每一個清單類控制項**嘗試讀取選取狀態。

    這是審查實測抓到的缺陷所在。原本 `probe_ui` 把頂層視窗餵給
    `read_selection()`，但 SelectionPattern 是清單控制項才有的東西——頂層
    Window 永遠不會有，於是 `selection_readable` 恆為 false。

    後果是假陰性：使用者會被告知「SELECTED 模式不可用，請手動維護 model
    清單」，而真正的清單控制項根本沒被問過。那是期一探測最重要的一個問題。

    回傳 [(路徑, 控制項型別, SelectionProbe)]，讓報告能列出**每一個**候選，
    使用者與後續設定都看得到是哪一個清單可讀。

    走訪失敗（控制項消失、COM 例外）只跳過該節點：探測是唯讀的診斷工具，
    不該因為某個角落壞掉就整份報告拿不到。
    """
    results = []

    def visit(node, path):
        try:
            ctype = str(_read(lambda: node.element_info.control_type, UNKNOWN_TYPE))
        except Exception:  # noqa: BLE001
            return
        label = str(_read(lambda: node.window_text(), "")).strip()
        here = path + [f"{ctype}:{label}" if label else ctype]

        if ctype in SELECTION_CONTROL_TYPES:
            try:
                results.append((PATH_SEPARATOR.join(here), ctype, read_selection(node)))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    (
                        PATH_SEPARATOR.join(here),
                        ctype,
                        SelectionProbe(supported=False, items=(), error=_describe_exception(exc)),
                    )
                )

        if len(here) > max_depth:
            return
        for child in _children(node):
            visit(child, here)

    visit(control, [])
    return results


def window_label(control) -> str:
    """
    不純：實際抓到的視窗是誰。

    這個函式的存在是因為一個實測抓到的缺陷：探測報告原本記的是「搜尋條件」
    而不是「實際抓到的視窗」。開發機上沒有 AccuMark，`title_re="AccuMark.*"`
    卻探測成功——它匹配到了瀏覽器開著的一份標題以 AccuMark 開頭的文件，而
    報告上只顯示搜尋條件，使用者完全看不出探錯了對象。

    在目標機上，桌面只要有任何標題以 AccuMark 開頭的視窗，就會產出一份看
    起來正常、其實結構全錯的報告。
    """
    title = str(_read(lambda: control.window_text(), "")).strip()
    klass = str(_read(lambda: control.class_name(), "")).strip()
    if title and klass:
        return f"{title}　[{klass}]"
    return title or klass or "(無法取得視窗標題)"


def list_top_windows(backend: str = BACKEND) -> Tuple[str, ...]:
    """
    不純：列出目前畫面上有標題的頂層視窗，去重且保持原順序。

    只在「找不到目標視窗」時用得到。第一次探測時沒人知道 AccuMark 的視窗
    標題長什麼樣，列出候選讓使用者能直接指認，比雙方反覆猜快得多。

    任何失敗都回空 tuple 而不是拋例外：這是輔助資訊，它自己壞掉不該蓋掉
    「找不到視窗」這個真正的錯誤。
    """
    try:
        pywinauto = _load_pywinauto()
        titles = []
        for window in pywinauto.Desktop(backend=backend).windows():
            text = str(_read(lambda w=window: w.window_text(), "")).strip()
            if text:
                titles.append(text)
        return tuple(dict.fromkeys(titles))
    except Exception:  # noqa: BLE001
        return ()


def popup_windows(windows: Sequence[Any], backend: str = BACKEND) -> Tuple[Any, ...]:
    """
    不純：AccuMark 自己彈出來的視窗——同 process、但不是那幾個主視窗。

    守衛只能看這些。使用者的核心需求是「一邊跑批次一邊用同一台電腦做別的
    事」，所以「前景視窗不是 AccuMark」是**正常狀態**，不是異常：拿前景
    視窗當偵測對象的話，他切去瀏覽器打字就會把整批停掉。

    反過來，模態對話框在使用者切走之後仍然擋著 AccuMark，只是不在前景。
    照 process 找才看得到它。

    失敗回空 tuple：守衛讀不到畫面時，讓等待迴圈自然走到逾時，比拋例外
    炸掉整批安全。
    """
    try:
        pywinauto = _load_pywinauto()
        known = set()
        pids = []
        for w in windows:
            info = getattr(w, "element_info", None)
            handle = getattr(info, "handle", None)
            pid = getattr(info, "process_id", None)
            if handle is not None:
                known.add(handle)
            if pid is not None and pid not in pids:
                pids.append(pid)

        found = []
        for pid in pids:
            for w in pywinauto.Desktop(backend=backend).windows(process=pid, visible_only=True):
                handle = getattr(w.element_info, "handle", None)
                if handle is not None and handle not in known:
                    known.add(handle)  # 兩個主視窗同 process 時不重複列
                    found.append(w)
        return tuple(found)
    except Exception:  # noqa: BLE001
        return ()


# ══════════════════════════════════════════════════════════════════════
# 期二操作函式（D3）：依 spec 定位、只用 UIA pattern 操作
# ══════════════════════════════════════════════════════════════════════
#
# 這一段是 batch_export.py 的 UI 層會呼叫的全部東西。三條設計線：
#
#   1. 只碰 iface_*，不用 pywinauto 的便利方法。讀 0.6.9 原始碼會發現
#      下拉選單的 select／collapse、選單路徑選取、Edit 的整段設文字，
#      內部都有「pattern 不支援就改動實體滑鼠或搶前景焦點」的退路分支，
#      而那些分支不報錯、不留紀錄。直接對 pattern 下指令就沒有退路——
#      不支援就是拋錯，使用者的滑鼠永遠不會被碰。
#   2. 每個失敗都翻成人看得懂的繁中訊息。UiaError 家族全是 RuntimeError，
#      目標機上看到訊息的人是使用者，他要的是「缺哪個、下一步做什麼」，
#      不是 COM 的 traceback。
#   3. 定位條件是鴨子型別：任何有 .strategy 與 .value 的物件都行，
#      config.Control 可以直接丟進來，測試也不必為了它造一個類別。

STRATEGY_TITLE_RE = "title_re"
STRATEGY_CONTROL_TYPE = "control_type"

# 操作層接受的五種策略（design.md §4.1）。與 ALL_STRATEGIES 刻意分開：
# 那個是探測報告的詞彙表，含 UNSTABLE；這個是「可以拿來定位」的集合。
LOCATOR_STRATEGIES: Tuple[str, ...] = (
    STRATEGY_AUTO_ID,
    STRATEGY_NAME,
    STRATEGY_TITLE_RE,
    STRATEGY_CONTROL_TYPE,
    STRATEGY_INDEX,
)

# 定位輪詢的間隔。這不是 TD-4 講的「等匯出完成」——那個要從設定檔來；
# 這只是「視窗還在畫、控制項還沒長出來」時再問一次 UIA 的節奏。
LOCATOR_POLL_SEC = 0.25

# 展開選單後子項出現的等待上限。Win32 選單是同步的，通常幾十毫秒就有；
# 給到 2 秒是為了慢機器與遠端桌面。
DEFAULT_POPUP_TIMEOUT_SEC = 2.0

# select_single 與 set_combo 認定為「項目」的控制項型別。三者對應
# List／DataGrid／Tree 的子項；自繪清單的項目可能是 Custom，見 _items_of。
ITEM_CONTROL_TYPES = ("ListItem", "DataItem", "TreeItem")

# 找不到項目時，訊息裡最多列幾個現有項目——列全會把真正的錯誤淹掉。
MAX_LISTED_ITEMS = 20


class WindowAmbiguousError(WindowNotFoundError):
    """
    標題條件同時匹配到多個可見視窗。

    歸在 WindowNotFoundError 底下，因為對呼叫端而言結論一樣——「沒有找到
    唯一的目標視窗」；但訊息會列出撞到的標題，讓使用者知道該把 title_re
    寫精確還是該關掉多餘的視窗。
    """


class ControlNotFoundError(UiaError):
    """
    依 spec 找不到子控制項。

    訊息固定含三樣東西：用什麼策略、找什麼值、在哪個父控制項底下。
    dry-run 的整個價值就是把這句話原封不動印出來——少任何一項，使用者
    就得回頭翻 700 個節點的探測報告。三樣也留成屬性，讓 dry-run 能排表。
    """

    def __init__(self, strategy: str, value: str, parent, detail: str = ""):
        self.strategy = strategy
        self.value = value
        self.parent_label = window_label(parent) if parent is not None else "(無父控制項)"
        message = (
            f"在「{self.parent_label}」底下找不到控制項"
            f"（strategy={strategy}, value={value!r}）"
        )
        if detail:
            message += f"。{detail}"
        super().__init__(message)


# ── 共用小工具 ───────────────────────────────────────────────────────


def _spec_fields(spec) -> Tuple[str, str]:
    """
    從鴨子型別的 spec 取出 (strategy, value)，順便把明顯錯的擋在門口。

    未知策略在這裡就拒絕，而不是在各分支默默回「找不到」：策略拼錯是
    設定檔的問題，訊息要指向設定檔，不能偽裝成「控制項不存在」。
    """
    try:
        strategy = str(spec.strategy)
        value = str(spec.value)
    except AttributeError as exc:
        raise UiaError("定位條件必須是有 strategy 與 value 兩個屬性的物件") from exc
    if strategy not in LOCATOR_STRATEGIES:
        raise UiaError(
            f"未知的定位策略 {strategy!r}，只接受 {list(LOCATOR_STRATEGIES)}"
        )
    if not value.strip():
        raise UiaError(f"定位策略 {strategy} 的 value 不可為空")
    return strategy, value


def _normalize(text: str) -> str:
    """比對項目名稱用：去頭尾空白、不分大小寫。回報時一律用原名。"""
    return str(text).strip().casefold()


def _name(ctrl) -> str:
    return str(_read(lambda: ctrl.element_info.name, ""))


def _ctype(ctrl) -> str:
    return str(_read(lambda: ctrl.element_info.control_type, ""))


def _first(items: Sequence[Any]):
    return items[0] if items else None


def _pattern(ctrl, attr: str, pattern_name: str):
    """
    取控制項的某個 UIA pattern 介面（iface_*），不支援就拋 UiaError。

    AttributeError 也算「不支援」：只有 UIAWrapper 才有 iface_*，若有人用
    win32 backend 抓到的控制項丟進來，該看到的訊息是「不支援這個 pattern」
    而不是一行 AttributeError。
    """
    label = window_label(ctrl)
    try:
        return getattr(ctrl, attr)
    except _no_pattern_error_types() + (AttributeError,) as exc:
        raise UiaError(
            f"控制項「{label}」不支援 {pattern_name} pattern，無法以 UIA 操作它。"
            "請對照探測報告確認定位到的是不是正確的控制項"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — COM 例外型別五花八門
        raise UiaError(
            f"取得「{label}」的 {pattern_name} pattern 失敗：{_describe_exception(exc)}"
        ) from exc


def _retry(func: Callable[[], Any], timeout_sec: float, retry_on: tuple):
    """
    在 timeout 內反覆呼叫 func，直到它不拋 retry_on 裡的例外。

    timeout 為 0 就只試一次——dry-run 與測試都靠這個不空等。逾時後拋出的
    是**最後一次的原始例外**，不是 pywinauto 的 TimeoutError：原始例外的訊息
    才有「找什麼、在哪裡找」，TimeoutError 只有一句「逾時」。
    """
    if timeout_sec <= 0:
        return func()
    from pywinauto import timings  # noqa: PLC0415 — 刻意延遲匯入

    try:
        return timings.wait_until_passes(timeout_sec, LOCATOR_POLL_SEC, func, retry_on)
    except timings.TimeoutError as err:
        original = getattr(err, "original_exception", None)
        if isinstance(original, BaseException):
            raise original
        raise UiaError(f"等待 {timeout_sec} 秒後仍未成功") from err


def _wrap_element(element, backend: str):
    """把 findwindows 回傳的 element_info 包成控制項物件（與 Desktop.windows() 同一條路）。"""
    from pywinauto import backend as backends  # noqa: PLC0415

    return backends.registry.backends[backend].generic_wrapper_class(element)


# ── 找視窗 ───────────────────────────────────────────────────────────


def find_window_by_spec(spec, timeout_sec: float, backend: str = BACKEND, *, process: Optional[int] = None):
    """
    不純：依 title_re 找唯一一個**可見**的頂層視窗，找不到拋 WindowNotFoundError。

    不走既有 find_window 的 `Desktop.window().wait()`：pywinauto 0.6.9 的
    `exists()` 會把 visible_only 強制設成 False（application.py 第 424 行），
    於是其他虛擬桌面上、或最小化到工具列的同名視窗都會被算進去。這台開發機
    實測 `.*記事本|.*Notepad` 因此匹配到 5 個元素而直接拋 ElementAmbiguousError
    ——而畫面上明明只有一個記事本。目標機上只要使用者曾開過第二份 AccuMark
    沒關乾淨，同樣的事就會發生。

    所以這裡自己用 find_elements(visible_only=True) 輪詢。多於一個可見視窗
    匹配時拋 WindowAmbiguousError 並列出標題；歧義在 timeout 內會重試，
    因為啟動畫面（splash）與主視窗常常短暫同名。

    process 可選：呼叫端知道目標的 PID 時（例如自己啟動的程序）用它縮小範圍。
    只要求可見、不要求 enabled——主視窗被 modal 對話框蓋住時是 disabled 的，
    dry-run 仍然要找得到它。
    """
    strategy, value = _spec_fields(spec)
    if strategy != STRATEGY_TITLE_RE:
        raise UiaError(
            f"視窗只能用 title_re 定位，目前是 strategy={strategy!r}（value={value!r}）"
        )
    try:
        re.compile(value)
    except re.error as exc:
        raise UiaError(f"title_re {value!r} 不是合法的正規式：{exc}") from exc

    _load_pywinauto()
    from pywinauto import findwindows  # noqa: PLC0415

    criteria: Dict[str, Any] = {
        "title_re": value,
        "backend": backend,
        "top_level_only": True,
        "visible_only": True,
        "enabled_only": False,
    }
    if process is not None:
        criteria["process"] = process

    def once():
        try:
            elements = list(findwindows.find_elements(**criteria))
        except Exception as exc:  # noqa: BLE001
            raise WindowNotFoundError(
                f"搜尋標題符合 {value!r} 的視窗時出錯：{_describe_exception(exc)}。"
                "請確認目標程式正在執行"
            ) from exc
        if not elements:
            raise WindowNotFoundError(
                f"在 {timeout_sec} 秒內找不到標題符合 {value!r} 的可見視窗。"
                "請確認目標程式正在執行、視窗沒有最小化，且 title_re 沒有打錯"
            )
        if len(elements) > 1:
            titles = [str(_read(lambda e=e: e.name, "")) for e in elements]
            raise WindowAmbiguousError(
                f"標題符合 {value!r} 的可見視窗不只一個（共 {len(elements)} 個）：{titles}。"
                "請把 title_re 寫得更精確，或先關掉多餘的視窗"
            )
        return elements[0]

    element = _retry(once, timeout_sec, (WindowNotFoundError,))
    try:
        return _wrap_element(element, backend)
    except Exception as exc:  # noqa: BLE001
        raise WindowNotFoundError(
            f"找到標題符合 {value!r} 的視窗，但無法包成控制項：{_describe_exception(exc)}"
        ) from exc


# ── 找子控制項 ───────────────────────────────────────────────────────


def _parse_index(value: str) -> Tuple[str, int]:
    """
    index 的 value 有兩種寫法，都要收：

      "3"       第 3 個子控制項（0 起算）——任務說明的定義
      "Edit#0"  同層第 0 個 Edit——探測報告 evaluate_locator 輸出的格式，
                使用者是照著報告抄進 config.json 的，不收就等於報告不能用
    """
    text = value.strip()
    type_name, sep, digits = text.rpartition("#")
    if not sep:
        type_name, digits = "", text
    type_name = type_name.strip()
    if not digits.strip().isdigit():
        raise UiaError(
            f"index 的 value 必須是整數或「ControlType#整數」（例如 \"3\" 或 \"Edit#0\"），"
            f"目前是 {value!r}"
        )
    return type_name, int(digits)


def _locate(parent, strategy: str, value: str):
    """
    單次定位，找不到回 None。各策略的搜尋範圍：

      name / control_type / auto_id / title_re  整棵子樹，取樹序第一個
      index                                    只看直接子節點

    name 與 control_type 走 UIA 自己的 FindAll 條件（一次跨行程呼叫）；
    auto_id 與 title_re 是 pywinauto 0.6.9 的條件建構器不支援的，只能逐
    節點走，所以用產生器一找到就停。
    """
    if strategy == STRATEGY_NAME:
        return _first(parent.descendants(title=value))

    if strategy == STRATEGY_CONTROL_TYPE:
        try:
            return _first(parent.descendants(control_type=value))
        except KeyError as exc:
            raise UiaError(
                f"未知的 ControlType {value!r}。請用探測報告裡 control_type 欄位的原字"
            ) from exc

    if strategy == STRATEGY_AUTO_ID:
        for ctrl in parent.iter_descendants():
            if str(_read(lambda c=ctrl: c.element_info.automation_id, "")) == value:
                return ctrl
        return None

    if strategy == STRATEGY_TITLE_RE:
        try:
            regex = re.compile(value)
        except re.error as exc:
            raise UiaError(f"title_re {value!r} 不是合法的正規式：{exc}") from exc
        for ctrl in parent.iter_descendants():
            if regex.match(_name(ctrl)):
                return ctrl
        return None

    type_name, position = _parse_index(value)
    kids = parent.children(control_type=type_name) if type_name else parent.children()
    return kids[position] if 0 <= position < len(kids) else None


def resolve(parent, spec, timeout_sec: float = 0.0):
    """
    不純：在 parent 底下依 spec 找一個子控制項，回傳控制項物件。

    control_type 的語意是「該視窗下第一個此型別的控制項」——config 預填的
    `export_to_path: control_type=Edit` 就是靠這個，標準資料夾對話框只有一個
    Edit。index 是「第 n 個子控制項」。

    找不到拋 ControlNotFoundError。走訪途中的 COM 例外（視窗正在重畫、
    控制項被關掉）也當成「這一次沒找到」而重試，直到 timeout；訊息會帶
    原因，讓最後一次的失敗看得出是真的沒有還是一直讀不到。
    """
    strategy, value = _spec_fields(spec)

    def once():
        try:
            found = _locate(parent, strategy, value)
        except UiaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ControlNotFoundError(
                strategy, value, parent, detail=f"走訪時出錯：{_describe_exception(exc)}"
            ) from exc
        if found is None:
            raise ControlNotFoundError(strategy, value, parent)
        return found

    return _retry(once, timeout_sec, (ControlNotFoundError,))


# ── 清單選取 ─────────────────────────────────────────────────────────


def _items_of(container) -> List[Any]:
    """
    清單／下拉的「項目」。先認 ListItem／DataItem／TreeItem；整棵子樹一個
    都沒有時退回直接子節點——自繪清單的項目型別常是 Custom，不退的話那種
    清單永遠選不到東西，而錯誤訊息會說「沒有任何項目」，明明畫面上一排。
    """
    everything = _read(lambda: container.descendants(), None) or []
    items = [c for c in everything if _ctype(c) in ITEM_CONTROL_TYPES]
    if items:
        return items
    return list(_read(lambda: container.children(), None) or [])


def _listing(names: Sequence[str]) -> str:
    if not names:
        return "清單目前沒有任何項目"
    shown = list(names[:MAX_LISTED_ITEMS])
    suffix = f"，只列前 {MAX_LISTED_ITEMS} 個" if len(names) > MAX_LISTED_ITEMS else ""
    return f"清單目前的項目（共 {len(names)} 個{suffix}）：{shown}"


def select_single(list_ctrl, item_name: str):
    """
    不純：在清單裡選取名稱等於 item_name 的那一項，回傳該項目的控制項。

    比對不分大小寫、去頭尾空白（使用者從 Explorer 抄 model 名時常多一個
    空白），但選取與回報用的都是清單裡的原名。走 SelectionItem.Select()，
    UIA 定義它會清掉其他選取——這正是 TD-9 要的單選。

    **不做讀回。** TD-9 說觸發前要讀回確認恰好一項，那要留在流程層才能被
    整合測試看到：這裡若偷偷驗了，流程層漏掉那一步時測試照樣是綠的。

    同名項目多於一個時拒絕而不是選第一個：DCU 裡兩個同名 model，隨便選
    一個會安靜地匯出錯的那份。
    """
    wanted = _normalize(item_name)
    if not wanted:
        raise UiaError("要選取的項目名稱不可為空")

    items = _items_of(list_ctrl)
    matches = [it for it in items if _normalize(_name(it)) == wanted]
    if not matches:
        raise ControlNotFoundError(
            STRATEGY_NAME, item_name, list_ctrl, detail=_listing([_name(it) for it in items])
        )
    if len(matches) > 1:
        raise UiaError(
            f"「{window_label(list_ctrl)}」裡有 {len(matches)} 個名稱等於 {item_name!r} 的項目"
            f"（{[_name(m) for m in matches]}），無法決定要選哪一個"
        )

    item = matches[0]
    iface = _pattern(item, "iface_selection_item", "SelectionItem")
    try:
        iface.Select()
    except Exception as exc:  # noqa: BLE001
        raise UiaError(
            f"選取「{window_label(list_ctrl)}」裡的 {_name(item)!r} 失敗：{_describe_exception(exc)}"
        ) from exc
    return item


def list_item_names(list_ctrl) -> Tuple[str, ...]:
    """
    不純：清單裡所有項目的名稱。

    用來回答「這個 model 在不在清單裡」——沒有它，找不到的 model 會走到
    select_single 才失敗，那時訊息指向「選取失敗」而不是「根本沒有這個
    model」，使用者會去查選取而不是去查名字。
    """
    return tuple(_name(it) for it in _items_of(list_ctrl))


def window_title(ctrl) -> Optional[str]:
    """
    不純：視窗標題原文，讀不到回 None。

    刻意不把讀不到轉成空字串：dialog_guard 對 None 與 "" 的處理一樣安全，
    但日誌上「(無標題)」與「標題是空字串」是不同的線索。
    """
    text = _read(lambda: ctrl.window_text(), None)
    return None if text is None else str(text)


def button_names(ctrl) -> Tuple[str, ...]:
    """
    不純：這個視窗上所有按鈕的文字。

    停機時要把它們寫進日誌——那正是使用者擴充白名單所需要的資訊（TD-5）。
    讀不到就回空 tuple，這是輔助資訊，不該讓停機路徑掛掉。
    """
    buttons = _read(lambda: ctrl.descendants(control_type="Button"), None) or []
    return tuple(_name(b) for b in buttons)


def read_selected_names(list_ctrl) -> Tuple[str, ...]:
    """
    不純：清單目前選取的項目名稱，沿用 read_selection 的讀法。

    讀不到是錯誤，不是空 tuple：TD-9 的讀回驗證若把「不支援 pattern」當成
    「0 項」，任務會標成 FAILED_SELECTION，訊息卻指向選取而不是指向 pattern，
    使用者會去重選十次而不是去查控制項。
    """
    probe = read_selection(list_ctrl)
    label = window_label(list_ctrl)
    if probe.error:
        raise UiaError(f"讀取「{label}」的選取狀態時出錯：{probe.error}")
    if not probe.supported:
        raise UiaError(f"「{label}」不支援 Selection pattern，讀不到選取狀態")
    return tuple(probe.items)


# ── 值與文字 ─────────────────────────────────────────────────────────


def set_value(ctrl, text: str) -> None:
    """
    不純：以 ValuePattern 設值，然後讀回比對；不一致拋 UiaError（RuntimeError）。

    一定要讀回。SetValue 對唯讀欄位、或對「看起來像輸入框但其實是自繪」的
    控制項，COM 常常回成功卻什麼都沒寫進去。路徑欄沒改到，匯出就寫進上
    一次的資料夾——而且每一步都看起來成功。
    """
    label = window_label(ctrl)
    iface = _pattern(ctrl, "iface_value", "Value")
    try:
        iface.SetValue(text)
    except Exception as exc:  # noqa: BLE001
        raise UiaError(
            f"對「{label}」設值失敗：{_describe_exception(exc)}。欄位可能是唯讀的"
        ) from exc
    try:
        actual = iface.CurrentValue
    except Exception as exc:  # noqa: BLE001
        raise UiaError(f"對「{label}」設值後讀不回目前的值：{_describe_exception(exc)}") from exc
    actual = "" if actual is None else str(actual)
    if actual != text:
        raise UiaError(
            f"對「{label}」設值後讀回不一致：期望 {text!r}，實際 {actual!r}。"
            "欄位可能是唯讀、有格式限制，或定位到的不是真正的輸入欄"
        )


def read_value(ctrl) -> str:
    """不純：讀 ValuePattern 的目前值。沒有 pattern 或讀失敗都拋 UiaError。"""
    iface = _pattern(ctrl, "iface_value", "Value")
    try:
        value = iface.CurrentValue
    except Exception as exc:  # noqa: BLE001
        raise UiaError(f"讀取「{window_label(ctrl)}」的值失敗：{_describe_exception(exc)}") from exc
    return "" if value is None else str(value)


def read_text(ctrl) -> str:
    """
    不純：盡力讀出控制項的文字，依序試 ValuePattern → TextPattern →
    window_text()；全失敗回空字串。DCU 的 Results 窗格是什麼控制項還不知道，
    這裡三種都要能吃。

    只在 pattern **不存在**時才退到下一個，內容是空字串就回空字串。記事本
    實測：文字區的 window_text() 回的是控制項 Name「文字編輯器」，不是內容。
    若空內容也往下退，Results 為空時會讀到它的標籤，完成偵測就誤判成
    「有結果」。
    """
    getters: Tuple[Callable[[], Any], ...] = (
        lambda: ctrl.iface_value.CurrentValue,
        lambda: ctrl.iface_text.DocumentRange.GetText(-1),
        lambda: ctrl.window_text(),
    )
    for getter in getters:
        try:
            value = getter()
        except Exception:  # noqa: BLE001 — 沒有 pattern、COM 失敗，都是「換下一種」
            continue
        return "" if value is None else str(value)
    return ""


# ── 觸發 ─────────────────────────────────────────────────────────────


def invoke(ctrl) -> None:
    """不純：InvokePattern.Invoke()。按鈕、Ribbon 項目、葉節點選單項都走這個。"""
    iface = _pattern(ctrl, "iface_invoke", "Invoke")
    try:
        iface.Invoke()
    except Exception as exc:  # noqa: BLE001
        raise UiaError(f"對「{window_label(ctrl)}」執行 Invoke 失敗：{_describe_exception(exc)}") from exc


def _child_button(ctrl, name: str):
    return _first(_read(lambda: ctrl.children(title=name, control_type="Button"), None) or [])


def _expand(ctrl) -> None:
    """
    ExpandCollapse.Expand()。沒有這個 pattern 時退到 Invoke 子按鈕「Open」
    ——WinForms 的下拉選單就是這樣曝光的，pywinauto 也同樣退。Invoke 不動
    游標，所以這條退路是安全的；再沒有就拋錯，不會有第三條路。
    """
    label = window_label(ctrl)
    try:
        ctrl.iface_expand_collapse.Expand()
        return
    except _no_pattern_error_types() + (AttributeError,):
        pass
    except Exception as exc:  # noqa: BLE001
        raise UiaError(f"展開「{label}」失敗：{_describe_exception(exc)}") from exc

    button = _child_button(ctrl, "Open")
    if button is None:
        raise UiaError(
            f"「{label}」不支援 ExpandCollapse pattern，也沒有 Open 按鈕，無法以 UIA 展開"
        )
    invoke(button)


def _collapse(ctrl) -> None:
    """
    ExpandCollapse.Collapse()。沒有 pattern 就找「Close」按鈕 Invoke；連按鈕
    都沒有的是 WinForms 的簡易下拉，它本來就永遠展開，不算失敗。
    """
    label = window_label(ctrl)
    try:
        ctrl.iface_expand_collapse.Collapse()
        return
    except _no_pattern_error_types() + (AttributeError,):
        pass
    except Exception as exc:  # noqa: BLE001
        raise UiaError(f"收合「{label}」失敗：{_describe_exception(exc)}") from exc

    button = _child_button(ctrl, "Close")
    if button is not None:
        invoke(button)


def _collapse_quietly(ctrl) -> None:
    """失敗路徑上的收尾：收不回去也不能蓋掉原本的錯誤。"""
    try:
        _collapse(ctrl)
    except Exception:  # noqa: BLE001
        pass


def _combo_text(combo) -> str:
    """下拉目前顯示的值：先問 Selection，再問 Value。兩個都沒有才算讀不到。"""
    try:
        selected = combo.get_selection()
        if selected:
            return _element_label(selected[0])
    except Exception:  # noqa: BLE001
        pass
    try:
        value = combo.iface_value.CurrentValue
        return "" if value is None else str(value)
    except Exception:  # noqa: BLE001
        pass
    raise UiaError(
        f"無法讀回下拉選單「{window_label(combo)}」目前的值：Selection 與 Value pattern 都讀不到"
    )


def set_combo(combo, item_name: str) -> str:
    """
    不純：把下拉選單切到 item_name。展開 → 找項目 → SelectionItem.Select →
    收合 → 讀回確認。回傳讀回的實際文字。

    讀回是 TD-9 的一部分：File Type 沒切到，AAMA 的任務會產出 ASTM 的檔，
    檔名還是對的。找項目或選取失敗時一定把下拉收回去——留一個開著的下拉
    在畫面上，下一步的定位會撞到它。
    """
    label = window_label(combo)
    _expand(combo)
    try:
        select_single(combo, item_name)
    except BaseException:
        _collapse_quietly(combo)
        raise
    _collapse(combo)

    actual = _combo_text(combo)
    if _normalize(actual) != _normalize(item_name):
        raise UiaError(
            f"下拉選單「{label}」選取後讀回不一致：期望 {item_name!r}，實際 {actual!r}"
        )
    return actual


# ── 選單 ─────────────────────────────────────────────────────────────


def _menu_root(window, spec):
    """
    第一層選單項目要在 MenuBar 底下找，不在整個視窗找：視窗裡可能另有一顆
    叫「File」的按鈕或標籤。找不到時把選單列上實際的項目列出來——語系落差
    （File vs 檔案(F)）是 TD-10 預期最常見的失敗，這一行就能診斷。
    """
    strategy, value = _spec_fields(spec)
    bars = list(_read(lambda: window.descendants(control_type="MenuBar"), None) or [])
    for bar in bars:
        try:
            return resolve(bar, spec)
        except ControlNotFoundError:
            continue
    if not bars:
        raise ControlNotFoundError(
            strategy, value, window,
            detail="視窗裡沒有 MenuBar——V18 若是 Ribbon 介面，請改用 invoke 對 Ribbon 按鈕",
        )
    names = [_name(item) for bar in bars for item in (_read(lambda b=bar: b.children(), None) or [])]
    raise ControlNotFoundError(strategy, value, window, detail=f"選單列上實際的項目：{names}")


def _invoke_menu_item(item) -> None:
    """
    葉節點選單項目用 Invoke；沒有 Invoke 的試 SelectionItem（某些框架這樣
    曝光）。兩個都沒有，多半是定位到了有子選單的那一層——它只有
    ExpandCollapse。
    """
    label = window_label(item)
    try:
        item.iface_invoke.Invoke()
        return
    except _no_pattern_error_types() + (AttributeError,):
        pass
    except Exception as exc:  # noqa: BLE001
        raise UiaError(f"執行選單項目「{label}」失敗：{_describe_exception(exc)}") from exc

    try:
        item.iface_selection_item.Select()
    except _no_pattern_error_types() + (AttributeError,) as exc:
        raise UiaError(
            f"選單項目「{label}」既沒有 Invoke 也沒有 SelectionItem pattern，無法以 UIA 觸發。"
            "它可能是有子選單的那一層（要再往下一層），或者這其實是 Ribbon"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise UiaError(f"執行選單項目「{label}」失敗：{_describe_exception(exc)}") from exc


def menu_invoke(window, specs: Iterable[Any], *, popup_timeout_sec: float = DEFAULT_POPUP_TIMEOUT_SEC) -> None:
    """
    不純：沿 MenuBar → MenuItem 逐層 ExpandCollapse.Expand()，最後一層 Invoke。
    specs 是由外而內的定位條件序列，例如 [name=File, name=Export Zip]。

    **這是全模組唯一會短暫影響鍵盤焦點的動作。** Win32 選單展開期間，系統
    會讓選單取得鍵盤焦點（幾百毫秒，Invoke 後立刻歸還），實體游標不動。
    這是 Explorer 的 Export Zip 目前唯一已知的觸發方式，所以保留；若
    dry-run 發現 V18 Explorer 是 Ribbon（沒有 MenuBar），應改用 invoke 對
    Ribbon 按鈕——那條路連這幾百毫秒都沒有。

    任何一層失敗，已展開的選單會反向逐層收回：留一個開著的選單在畫面上，
    使用者會以為是自己誤點的，而下一個任務的定位會撞到它。
    """
    path = tuple(specs)
    if not path:
        raise UiaError("menu_invoke 至少要給一層選單項目")

    opened: List[Any] = []
    try:
        current = _menu_root(window, path[0])
        for spec in path[1:]:
            _expand(current)
            opened.append(current)
            current = resolve(current, spec, timeout_sec=popup_timeout_sec)
        _invoke_menu_item(current)
    except BaseException:
        for item in reversed(opened):
            _collapse_quietly(item)
        raise
