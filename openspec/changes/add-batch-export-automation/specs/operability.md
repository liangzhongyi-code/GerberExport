# Spec: 可運維性與安全（期二）

## ADDED Requirements

### Requirement: 對話框白名單制
The system SHALL 僅處理白名單中明確定義的對話框。遇到白名單外的任何視窗時，MUST NOT 送出任何按鍵或點擊，並須中止當前任務。

> **背景**：這是本專案最危險的失敗模式。若腳本對未知對話框盲目送出 Enter，可能誤觸「覆蓋既有檔案？」而造成**不可回復的資料遺失**。

#### Scenario: 出現未知對話框
- **GIVEN** 匯出過程中彈出白名單未收錄的對話框（例如授權警告、model 損毀提示）
- **WHEN** 腳本偵測到該視窗
- **THEN** 腳本擷取該視窗的標題、內文與所有按鈕文字寫入日誌，將任務標記為 `HALTED_UNKNOWN_DIALOG`，並停止整批執行；MUST NOT 對該視窗送出任何輸入

#### Scenario: 出現已知的覆蓋確認對話框
- **GIVEN** 白名單中定義了「檔案已存在，是否覆蓋」對話框，且其處置方式為「取消」
- **WHEN** 該對話框出現
- **THEN** 腳本選擇取消，任務標記為 `FAILED_TARGET_EXISTS`，暫存目錄與目的地皆無檔案被覆蓋

#### Scenario: 白名單可擴充而不改程式碼
- **GIVEN** 使用者在目標機遇到一個新的已知安全對話框
- **WHEN** 使用者將其標題與處置方式加入設定檔的白名單區段
- **THEN** 下次執行時腳本依該規則處理，未修改任何 `.py` 檔案

---

### Requirement: 啟動前環境檢查
The system SHALL 在執行第一個任務之前驗證執行環境，任一項不通過即中止並輸出明確原因。

檢查項目：AccuMark 程序存在、目標視窗可被 UI Automation 存取、暫存目錄可寫、輸出根目錄可寫、工作階段未鎖定。

#### Scenario: AccuMark 未啟動
- **GIVEN** AccuMark 未執行
- **WHEN** 使用者啟動腳本
- **THEN** 腳本立即中止，訊息指出「AccuMark 未執行」，MUST NOT 建立任何目錄或檔案

#### Scenario: 工作階段已鎖定
- **GIVEN** 使用者按下 Win+L 鎖定螢幕後才排定腳本執行
- **WHEN** 腳本啟動並執行環境檢查
- **THEN** 腳本中止並記錄「工作階段已鎖定，UI Automation 不可用」

#### Scenario: 環境檢查全數通過
- **GIVEN** 所有檢查項目皆符合
- **WHEN** 環境檢查完成
- **THEN** 日誌記錄各項檢查結果，腳本進入第一個任務

---

### Requirement: 結構化日誌
The system SHALL 為每次執行產生一份日誌檔，逐筆記錄每個任務的 model 名稱、格式、開始時間、結束時間、結果狀態與產出檔案清單。

#### Scenario: 全部成功
- **GIVEN** 12 個任務全部成功
- **WHEN** 腳本結束
- **THEN** 日誌含 12 筆狀態為 `SUCCESS` 的記錄，並在末尾輸出「成功 12 / 失敗 0」摘要，結束碼為 0

#### Scenario: 部分失敗
- **GIVEN** 12 個任務中有 2 個失敗
- **WHEN** 腳本結束
- **THEN** 摘要顯示「成功 10 / 失敗 2」並逐條列出失敗任務與原因，結束碼為非零

---

### Requirement: 中斷後續跑
The system SHALL 將已成功的任務記錄於狀態檔。重跑時，狀態檔中標記為成功且產出檔案確實存在的任務 MUST 被跳過。

#### Scenario: 第 7 個任務中斷後重跑
- **GIVEN** 上次執行在第 7 個任務因未知對話框中止，前 6 個成功
- **WHEN** 使用者排除問題後重新執行腳本
- **THEN** 前 6 個任務被跳過並記錄為 `SKIPPED_ALREADY_DONE`，腳本從第 7 個任務開始

#### Scenario: 狀態檔記載成功但檔案已被刪除
- **GIVEN** 狀態檔標記某任務成功，但其產出檔案已被使用者手動刪除
- **WHEN** 重跑時進行續跑判定
- **THEN** 該任務不被跳過，重新執行

#### Scenario: 強制全部重跑
- **GIVEN** 使用者雙擊 `4_強制全部重跑.bat`（`--force`）
- **WHEN** 腳本進行續跑判定
- **THEN** 所有任務皆執行，狀態檔被重置

---

### Requirement: 設定外置
The system SHALL 將 model 清單、暫存目錄、輸出根目錄、逾時秒數、輪詢間隔、對話框白名單與控制項定位資訊全部置於外部設定檔，MUST NOT 硬編碼於腳本中。

#### Scenario: 更換 model 清單
- **GIVEN** 使用者下個月要處理另外四個 model
- **WHEN** 使用者只編輯設定檔的 model 清單
- **THEN** 腳本正確處理新的 model，`.py` 檔案未被修改

#### Scenario: 目標機控制項與探測結果不符
- **GIVEN** 目標機的 AccuMark 版本小改，某控制項的 AutomationId 改變
- **WHEN** 使用者更新設定檔中該控制項的定位資訊
- **THEN** 腳本恢復正常運作，`.py` 檔案未被修改

---

### Requirement: 純雙擊啟動
The system SHALL 為每個使用情境提供對應的 `.bat` 啟動檔，使用者 MUST 能僅以滑鼠雙擊完成操作，MUST NOT 需要開啟終端機或輸入任何命令。

#### Scenario: 雙擊執行批次匯出
- **GIVEN** 使用者已完成首次設定
- **WHEN** 雙擊 `3_執行批次匯出.bat`
- **THEN** 批次流程啟動，主控台顯示進度，使用者未輸入任何命令

#### Scenario: 從其他工作目錄雙擊
- **GIVEN** `.bat` 位於含空白與中文字元的路徑
- **WHEN** 使用者雙擊（此時 Windows 的工作目錄未必等於 `.bat` 所在目錄）
- **THEN** 腳本正確解析自身位置並載入同層的 `lib\` 與 `config.json`，未出現「找不到檔案」

#### Scenario: 執行結束後視窗保留
- **GIVEN** 腳本執行完畢（成功或失敗皆然）
- **WHEN** 主控台即將關閉
- **THEN** 視窗暫停並提示按鍵才關閉，使用者得以讀取完整結果

#### Scenario: 腳本啟動即失敗
- **GIVEN** Python 因語法錯誤或缺少模組而立即中止
- **WHEN** 使用者雙擊 `.bat`
- **THEN** 錯誤訊息保留於視窗中，MUST NOT 一閃即逝

#### Scenario: 結束碼正確傳遞
- **GIVEN** Python 腳本以非零結束碼結束
- **WHEN** `.bat` 結束
- **THEN** `.bat` 的 `ERRORLEVEL` 等於該結束碼，且視窗中顯示失敗摘要

---

### Requirement: 啟動檔內容限用 ASCII
The system SHALL 使 `.bat` 檔案內容僅含 ASCII 字元；所有中文訊息 MUST 由 Python 輸出，MUST NOT 寫在 `.bat` 內。檔案名稱本身不受此限（可用中文）。

> **背景**：`.bat` 由 cmd.exe 以主控台代碼頁解讀（台灣環境通常為 cp950）。若 `.bat` 內含中文又存成 UTF-8，指令會被解析成亂碼而執行失敗。把中文全部推到 Python 層可徹底避開此問題。

#### Scenario: 不同系統語系下執行
- **GIVEN** 目標機的主控台代碼頁為 cp950 或其他非 UTF-8 設定
- **WHEN** 執行任一 `.bat`
- **THEN** 指令正確解析，未出現亂碼或語法錯誤

#### Scenario: 中文進度訊息在 cp950 主控台正常顯示
- **GIVEN** 目標機主控台代碼頁為 cp950，腳本需輸出含中文的進度訊息
- **WHEN** Python 寫入 stdout
- **THEN** 中文正確顯示，且 MUST NOT 拋出 `UnicodeEncodeError`（腳本須主動設定輸出編碼）

#### Scenario: 日誌檔以 UTF-8 寫入
- **GIVEN** 日誌內容含中文 model 名稱或錯誤訊息
- **WHEN** 寫入日誌檔
- **THEN** 檔案以 UTF-8 編碼寫入，可被記事本與編輯器正確開啟

---

### Requirement: 可攜交付
The system SHALL 以一個自足資料夾交付，複製到目標機後不需執行安裝程序即可使用。

#### Scenario: 複製即用
- **GIVEN** 使用者將整個 `scripts\` 資料夾複製到目標機，且 `pywinauto` 已安裝
- **WHEN** 依使用手冊指示雙擊 `.bat`
- **THEN** 腳本正常執行，未要求額外設定環境變數或修改系統

#### Scenario: 路徑含中文或空白
- **GIVEN** 資料夾被放在含中文字元或空白的路徑下（例如桌面）
- **WHEN** 腳本執行
- **THEN** 所有路徑處理正確，未出現編碼或引號錯誤

---

### Requirement: 完整使用手冊
The system SHALL 交付一份中文使用手冊，涵蓋從零到日常使用的完整流程，使用者 MUST 能在無開發者協助下完成安裝、設定、執行與基本排錯。

手冊 MUST 涵蓋：安裝與相依處理（含離線 wheel）、期一探測操作、首次設定、日常操作、輸出結構說明、螢幕可關但不可鎖的限制、七種任務狀態的意義與處置、日誌位置與判讀、常見問題排除、白名單擴充方式。

#### Scenario: 使用者依手冊完成首次設定
- **GIVEN** 使用者從未執行過本工具，手上只有 `scripts\` 資料夾與手冊
- **WHEN** 依手冊逐步操作
- **THEN** 能完成環境檢查、探測、設定與第一次批次匯出，過程中不需詢問開發者

#### Scenario: 遇到未知對話框後自行排除
- **GIVEN** 執行中止並記錄 `HALTED_UNKNOWN_DIALOG`
- **WHEN** 使用者查閱手冊的排錯章節
- **THEN** 能找到「如何從日誌讀出對話框資訊」與「如何將其加入白名單」的具體步驟

#### Scenario: 手冊與實作一致
- **GIVEN** 手冊中列出的每個 `.bat` 檔名、設定欄位與狀態代碼
- **WHEN** 與實際交付物比對
- **THEN** 完全一致，無過時或不存在的項目
