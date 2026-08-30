"""
C5 靜態掃描守衛（對應 spec: batch-export「執行期間游標不動」「使用者同時操作
其他程式」、operability「啟動檔內容限用 ASCII」）。

這個檔案不 import 任何 scripts/ 的模組、也不執行任何程式——它只把原始碼
當成純文字讀進來比對。理由見 design.md §9.1：

「不佔用滑鼠」若改在執行期驗證（例如定時取樣游標座標），只要使用者自己
動一下滑鼠就會偽陽性；反過來腳本偷點一下、剛好落在兩次取樣之間，就會偽
陰性。靜態掃描沒有這個問題——原始碼裡有沒有那個字串是確定的事實。

最危險的具體對象是 pywinauto 的 `click_input()`：它與 `click()`（UIA Invoke，
游標不動）名稱只差三個字，誤用不會拋錯、不會寫進日誌，只會在批次跑到一半
時默默把實體滑鼠搶走。使用者的核心需求正是「一邊跑批次一邊用同一台電腦做
別的事」，所以這個失敗模式直接摧毀整個工具的價值，而除了讀原始碼之外沒有
任何地方會發現它。

掃描範圍刻意**只含 `scripts/`，不含 `tests/`**：這個檔案本身必須把禁用字串
逐字寫出來才能拿去比對，若把 `tests/` 也納入，第一個被判死的就是它自己。
"""

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"
LIB = SCRIPTS / "lib"


# ── 禁用清單 ──────────────────────────────────────────────────────────

# 每一個都會移動實體游標或搶奪鍵盤焦點。正確的替代品是 UIA 的
# Invoke / Value / Selection pattern，它們直接對控制項下指令，
# 游標與焦點全程不動。
BANNED_INPUT_APIS = (
    "SetCursorPos",  # user32：直接把實體游標搬到指定座標
    "mouse_event",   # user32 舊版合成滑鼠事件
    "SendInput",     # user32 合成鍵盤／滑鼠輸入
    "keybd_event",   # user32 舊版合成鍵盤事件
    "click_input",   # pywinauto：移動實體滑鼠去點，與 click() 只差三個字
    "type_keys",     # pywinauto：搶前景焦點後逐鍵送出
    "send_keys",     # pywinauto：全域鍵盤注入，使用者正在打字就會被插隊
    "pyautogui",     # 整個套件都建立在實體輸入上，沒有安全的用法
)

# `sleep(` 後面直接接數字字面值＝寫死的等待。TD-4 明訂等待間隔一律從設定檔
# 來：寫死的秒數太短會搬走寫到一半的檔案（靜默資料損毀，最嚴重的失敗模式），
# 太長則每次任務都在空等。
#
# `\(` 緊跟在 sleep 之後是刻意的——stability.py 用 `sleep_fn(interval)` 把睡眠
# 注入進來，那是正確寫法，不能被誤殺。IGNORECASE 是為了一併攔住 Win32 的
# `Sleep(1000)`。
_HARDCODED_SLEEP = re.compile(r"\bsleep\s*\(\s*[-+]?\d", re.IGNORECASE)

# 逐行比對（不用 MULTILINE，呼叫端已經拆好行）。
# 第三個分支涵蓋 `importlib.import_module("pywinauto")` 這種繞過 import 語法的寫法。
_PYWINAUTO_IMPORT = re.compile(
    r"^\s*import\s+pywinauto\b"
    r"|^\s*from\s+pywinauto[\s.]"
    r"|(?:__import__|import_module)\s*\(\s*[\"']pywinauto"
)


# ── 偵測器（純函式，可獨立自檢）────────────────────────────────────────


def scan_banned_api(text):
    """
    回傳 [(行號, 命中的 API 名稱)]；沒命中就是空清單。

    刻意用最笨的子字串比對，不做 AST 解析也不排除註解與字串。理由是誤判
    方向不對稱：把註解裡的 `click_input` 判成違規，代價是改一行註解；漏掉
    一個用字串組出來的動態呼叫，代價是使用者的滑鼠被搶走而沒人知道。
    """
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for api in BANNED_INPUT_APIS:
            if api in line:
                hits.append((lineno, api))
    return hits


def scan_hardcoded_sleep(text):
    """回傳 [(行號, 該行內容)]。"""
    return [
        (lineno, line.strip())
        for lineno, line in enumerate(text.splitlines(), 1)
        if _HARDCODED_SLEEP.search(line)
    ]


def scan_pywinauto_import(text):
    """回傳 [(行號, 該行內容)]。"""
    return [
        (lineno, line.strip())
        for lineno, line in enumerate(text.splitlines(), 1)
        if _PYWINAUTO_IMPORT.search(line)
    ]


def scan_non_ascii(raw):
    """
    回傳 [(位元組位移, 位元組值)]。

    輸入是 bytes 且刻意不先解碼：這裡要驗的正是「這些位元組本身合不合法」，
    先 decode 會把問題掩蓋掉——UTF-8 解得開不代表 cmd.exe 讀得懂。
    """
    return [(offset, value) for offset, value in enumerate(raw) if value > 0x7F]


# ── 掃描範圍 ──────────────────────────────────────────────────────────


def _collect(pattern):
    """__pycache__ 裡不會有原始碼，明確排除以免有人放了 .py 進去干擾結果。"""
    return sorted(
        p
        for p in SCRIPTS.rglob(pattern)
        if "__pycache__" not in p.parts
    )


def _rel(path):
    return str(path.relative_to(PROJECT_ROOT))


PY_SOURCES = _collect("*.py")
BAT_FILES = _collect("*.bat")

# uia.py 是唯一允許碰 pywinauto 的模組——TD-3 的分層就是把 UI 呼叫全部關在
# 那一個檔案裡，其餘模組才能在沒有 AccuMark 的開發機上完整測試。
# 它目前尚未建立（B1 才會做），glob 自然容忍它不存在。
#
# 注意：豁免只針對「import pywinauto」這一條。uia.py 一樣要通過上面的實體
# 輸入 API 掃描——它正是最可能誤用 click_input() 的地方。
LIB_MODULES = sorted(
    p for p in LIB.glob("*.py") if p.name != "uia.py" and "__pycache__" not in p.parts
)


# ── 掃描範圍非空（防真空綠燈）──────────────────────────────────────────


def test_python_scan_covers_every_entry_point():
    """
    目錄搬動或改名會讓 rglob 掃出空集合，底下每一條斷言都會變成真空的綠燈，
    而且不會有任何徵兆。這裡釘住幾個一定存在的檔案當成範圍的存活指標。
    """
    names = {p.name for p in PY_SOURCES}
    missing = {"batch_export.py", "probe_ui.py", "check_env.py"} - names
    assert not missing, f"掃描範圍看不到進入點 {missing}，掃描可能已經失效"


def test_bat_scan_is_not_empty():
    assert BAT_FILES, "掃不到任何 .bat，ASCII 斷言等於沒有執行"


def test_lib_layer_scan_is_not_empty():
    assert LIB_MODULES, "掃不到任何 lib 模組，分層守護等於沒有執行"


# ── 偵測器自檢 ────────────────────────────────────────────────────────
#
# 底下四組自檢存在的理由：靜態掃描的失敗模式不是「誤報」而是「沉默」。
# 只要比對條件寫錯一個字，掃描永遠回傳空清單、測試永遠是綠的，而防護
# 早就不存在了——這種綠燈比沒有測試更危險，因為它給了錯誤的安全感。
# 所以先餵已知會中的內容，確認偵測器真的咬得到，再拿它去掃真的原始碼。


@pytest.mark.parametrize("api", BANNED_INPUT_APIS)
def test_scanner_catches_each_banned_api(api):
    """清單裡每一個字串都要能單獨被咬到，不能只有第一個有效。"""
    hits = scan_banned_api(f"    handle.{api}()\n")
    assert hits == [(1, api)], f"偵測器漏掉 {api}"


def test_scanner_does_not_flag_uia_click():
    """
    click() 是 UIA Invoke，游標不動，正是我們要求的寫法。
    偵測器若連它一起殺，開發者只好去繞過掃描，防護反而歸零。
    """
    assert not scan_banned_api("    handle.click()\n")


@pytest.mark.parametrize(
    "snippet",
    [
        "time.sleep(3)",
        "sleep(0.5)",
        "await asyncio.sleep( 2 )",
        "kernel32.Sleep(1000)",
    ],
)
def test_scanner_catches_hardcoded_sleep(snippet):
    assert scan_hardcoded_sleep(snippet), f"寫死的等待沒被咬到：{snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "sleep_fn(interval)",  # stability.py 的注入式睡眠，正確寫法
        "time.sleep(poll_interval_ms / 1000.0)",
        "sleep(cfg.poll_interval)",
    ],
)
def test_scanner_does_not_flag_injected_sleep(snippet):
    """等待長度來自設定或參數就是合規的，誤殺會逼人把注入改回寫死。"""
    assert not scan_hardcoded_sleep(snippet), f"注入式等待被誤判：{snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "import pywinauto",
        "from pywinauto import Desktop",
        "    import pywinauto.controls",  # 縮排在 try/函式裡一樣算
        "from pywinauto.application import Application",
        'mod = importlib.import_module("pywinauto")',
    ],
)
def test_scanner_catches_pywinauto_import(snippet):
    assert scan_pywinauto_import(snippet), f"pywinauto 匯入沒被咬到：{snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "# 這一層不得 import pywinauto",  # 註解提到不算匯入
        '"""說明：pywinauto 的呼叫全部關在 uia.py"""',
    ],
)
def test_scanner_does_not_flag_pywinauto_mention(snippet):
    assert not scan_pywinauto_import(snippet), f"純提及被誤判為匯入：{snippet}"


def test_scanner_catches_non_ascii_bytes():
    assert scan_non_ascii("執行".encode("utf-8")), "非 ASCII 位元組沒被咬到"


def test_scanner_does_not_flag_plain_ascii():
    assert not scan_non_ascii(b"@echo off\r\npause\r\n")


# ── 斷言一：scripts/ 不得出現任何實體輸入 API ─────────────────────────


@pytest.mark.parametrize("path", PY_SOURCES, ids=_rel)
def test_no_physical_input_api(path):
    """
    擋的失敗模式：程式在跑的時候把實體滑鼠或鍵盤焦點搶走。

    使用者的核心需求是能一邊跑批次一邊用同一台電腦做別的事。這裡任何一個
    字串出現，就代表某次呼叫會移動游標或插隊送出按鍵——不會報錯、不會進
    日誌，只會讓使用者手上的工作被打斷，而且他不會知道是誰幹的。
    """
    hits = scan_banned_api(path.read_text(encoding="utf-8"))
    detail = "、".join(f"第 {n} 行 {api}" for n, api in hits)
    assert not hits, (
        f"{_rel(path)} 含實體輸入 API：{detail}。"
        "請改用 UIA 的 Invoke / Value / Selection pattern（例如 click_input → click）"
    )


# ── 斷言二：.bat 純 ASCII 且無 BOM ────────────────────────────────────


@pytest.mark.parametrize("path", BAT_FILES, ids=_rel)
def test_bat_is_pure_ascii(path):
    """
    擋的失敗模式：cmd.exe 以主控台代碼頁（目標機是 cp950）解讀 .bat，
    非 ASCII 位元組會讓指令本身變亂碼，使用者雙擊只看到一行看不懂的錯誤。

    檔名可以是中文（由檔案系統處理），內容不行。
    """
    bad = scan_non_ascii(path.read_bytes())
    assert not bad, f"{_rel(path)} 含非 ASCII 位元組（前 3 個）：{bad[:3]}"


@pytest.mark.parametrize("path", BAT_FILES, ids=_rel)
def test_bat_has_no_bom(path):
    """UTF-8 BOM 會被 cmd.exe 當成第一道指令的一部分，整行直接失效。"""
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf"), f"{_rel(path)} 帶有 UTF-8 BOM"


# ── 斷言三：分層守護（TD-3）───────────────────────────────────────────


@pytest.mark.parametrize("path", LIB_MODULES, ids=_rel)
def test_lib_stays_free_of_pywinauto(path):
    """
    擋的失敗模式：純函式層被 UI 依賴汙染，導致它在沒有 AccuMark 的開發機上
    根本 import 不起來。

    TD-3 的整個 TDD 策略押在這條線上——約 70% 的邏輯（穩定判定、續跑判定、
    衝突改名、白名單比對）之所以看得到真實的 RED，前提就是這一層不碰 UI。
    一旦有人為了方便在 config.py 裡 import pywinauto，這些測試會在開發機上
    全部變成 collection error，而分層一旦破掉就很難再收回來。

    uia.py 是唯一的豁免（見上方 LIB_MODULES），UI 呼叫全部關在那一個檔案裡。
    """
    hits = scan_pywinauto_import(path.read_text(encoding="utf-8"))
    detail = "、".join(f"第 {n} 行：{line}" for n, line in hits)
    assert not hits, (
        f"{_rel(path)} 匯入了 pywinauto（{detail}）。"
        "純函式層必須能在沒有 AccuMark 的機器上完整測試，UI 呼叫請收進 lib/uia.py"
    )


# ── 斷言四：不得硬編碼等待（TD-4）─────────────────────────────────────


@pytest.mark.parametrize("path", PY_SOURCES, ids=_rel)
def test_no_hardcoded_sleep(path):
    """
    擋的失敗模式：用寫死的秒數猜「匯出應該好了吧」。

    TD-4 已經把這個選項評估掉了——秒數猜太短會搬走一個寫到一半的檔案，
    而且完全不會報錯，使用者要等到工廠打不開檔案才會發現；猜太長則 12 次
    任務累積大量空等。正確做法是輪詢檔案大小，且取樣參數一律從設定檔來。
    """
    hits = scan_hardcoded_sleep(path.read_text(encoding="utf-8"))
    detail = "、".join(f"第 {n} 行：{line}" for n, line in hits)
    assert not hits, (
        f"{_rel(path)} 出現寫死的等待（{detail}）。"
        "等待間隔請從設定檔取得，並以 stability.wait_for_stable 判定完成"
    )
