# Spec: UI 控制項探測（期一）

## ADDED Requirements

### Requirement: 相依前置驗證（期零）
The system SHALL 提供一支獨立的環境檢查腳本，在任何實際工作開始前確認 Python 版本與 `pywinauto` 可用性；不可用時 MUST 輸出具體可執行的補救指示，MUST NOT 只丟出原始的 traceback。

#### Scenario: 相依齊備
- **GIVEN** 目標機已安裝 Python 3.8 以上且 `pywinauto` 可匯入
- **WHEN** 使用者雙擊 `0_檢查環境.bat`
- **THEN** 印出 Python 版本、`pywinauto` 版本與「檢查通過」，結束碼為 0

#### Scenario: `pywinauto` 未安裝
- **GIVEN** 目標機有 Python 但未安裝 `pywinauto`
- **WHEN** 執行環境檢查
- **THEN** 明確指出缺少的套件，並同時給出**線上安裝指令**與**離線 wheel 安裝指令**兩種補救方式，結束碼為非零

#### Scenario: Python 版本過低
- **GIVEN** 目標機的 Python 版本低於 3.8
- **WHEN** 執行環境檢查
- **THEN** 印出實際版本與所需版本，結束碼為非零

#### Scenario: `python` 不在 PATH 上
- **GIVEN** 目標機的 `python` 未加入 PATH，但 `py` launcher 存在
- **WHEN** 使用者雙擊任一 `.bat`
- **THEN** `.bat` 以 `py -3` 成功啟動腳本

#### Scenario: Python 與 py 皆不可用
- **GIVEN** 目標機兩者皆無法呼叫
- **WHEN** 使用者雙擊任一 `.bat`
- **THEN** 錯誤訊息停留在視窗中（因 `pause`），使用者得以讀取，MUST NOT 一閃即逝

---

### Requirement: 最小相依
The system SHALL 除 `pywinauto`（連帶其相依 `comtypes`、`pywin32`）外，MUST NOT 引入任何其他第三方套件。

#### Scenario: 相依清單稽核
- **GIVEN** 交付的所有 `.py` 檔案
- **WHEN** 檢視其 import 語句
- **THEN** 僅出現 Python 標準庫與 `pywinauto`

#### Scenario: 目標機無外網
- **GIVEN** 目標機無法連上 PyPI
- **WHEN** 使用者依使用手冊以 `pip install --no-index --find-links=wheels pywinauto` 安裝
- **THEN** 安裝成功，環境檢查通過

---

### Requirement: 控制項樹匯出
The system SHALL 將目標視窗的 UI Automation 控制項樹輸出為結構化報告檔，每個控制項至少含 `Name`、`AutomationId`、`ControlType`、`ClassName`、`IsEnabled`、階層深度與同層索引。

#### Scenario: 探測 AccuMark Explorer 主視窗
- **GIVEN** AccuMark Explorer 正在執行且視窗未最小化
- **WHEN** 執行 `probe_ui.py`
- **THEN** 產出報告檔，且檔案中可查到代表 model 清單的控制項節點，其 `ControlType` 為清單類（List／DataGrid／Table）

#### Scenario: 探測模態匯出對話框
- **GIVEN** 使用者已手動開啟任一匯出對話框
- **WHEN** 使用者雙擊 `2_執行探測_對話框.bat`
- **THEN** 報告檔含該對話框的控制項節點，包含輸出路徑輸入框與確認按鈕

---

### Requirement: 選取狀態可讀性探測
The system SHALL 於報告中明確標示 model 清單控制項**能否讀取目前選取的項目**，作為 `models: "SELECTED"` 模式是否可用的判斷依據。

> **背景**：使用者希望免維護 model 清單，直接處理他在 Explorer 中框選的項目。這取決於該控制項是否曝光選取狀態，必須實測才能得知。

#### Scenario: 清單支援讀取選取項
- **GIVEN** 使用者在 AccuMark Explorer 中框選了數個 model
- **WHEN** 執行探測
- **THEN** 報告中標示 `selection_readable: true`，並列出讀到的項目名稱供使用者比對是否正確

#### Scenario: 清單不支援讀取選取項
- **GIVEN** 清單控制項未曝光選取狀態
- **WHEN** 執行探測
- **THEN** 報告中標示 `selection_readable: false`，摘要區提示需改用明確 model 清單模式

#### Scenario: 目標程序未啟動
- **GIVEN** AccuMark 未執行
- **WHEN** 執行 `probe_ui.py`
- **THEN** 腳本以非零結束碼終止，並輸出可讀訊息指出「找不到 AccuMark 程序」，MUST NOT 產生空白或誤導性的報告檔

---

### Requirement: 定位策略評估
The system SHALL 為每個控制項標示建議的定位策略，優先序為 `AutomationId` > `Name` > `ControlType + 同層索引`，並對三者皆不可靠的控制項明確標示為不可穩定定位。

#### Scenario: 控制項具備 AutomationId
- **GIVEN** 某按鈕的 `AutomationId` 非空且在其父容器下唯一
- **WHEN** 產出報告
- **THEN** 該控制項的建議策略欄位為 `AutomationId`

#### Scenario: 控制項無任何穩定識別
- **GIVEN** 某控制項 `AutomationId` 與 `Name` 皆為空，且同層有多個同型控制項
- **WHEN** 產出報告
- **THEN** 該控制項被標示為 `UNSTABLE`，報告摘要區列出所有 `UNSTABLE` 項目的總數

#### Scenario: 自繪畫布無法曝光
- **GIVEN** 探測範圍涵蓋 PDS 的自繪版型畫布
- **WHEN** 產出報告
- **THEN** 該區域呈現為單一無子節點的控制項，且報告不因此中斷或報錯

---

### Requirement: 報告可攜回
The system SHALL 將報告輸出為單一純文字檔（JSON 或縮排文字），MUST NOT 包含螢幕截圖以外的二進位內容，使其可直接複製或貼回開發機。

#### Scenario: 報告產出位置
- **GIVEN** 探測成功完成
- **WHEN** 腳本結束
- **THEN** 報告檔位於腳本同層的 `probe-output\` 目錄，檔名含時間戳，且主控台印出該檔完整路徑
