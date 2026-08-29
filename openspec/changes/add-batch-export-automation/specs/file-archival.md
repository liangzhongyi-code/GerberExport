# Spec: 檔案歸檔（期二）

## ADDED Requirements

### Requirement: 即時搬離暫存區
The system SHALL 在每次匯出完成偵測成功後，立即將該次產出的所有檔案搬離暫存目錄，使暫存目錄在下一次匯出開始前為空。

#### Scenario: 連續兩次匯出
- **GIVEN** 某 model 的 AAMA 匯出已完成並搬離
- **WHEN** 腳本開始同一 model 的 ASTM 匯出
- **THEN** 暫存目錄在觸發匯出前為空，因此新出現的任何檔案必定屬於本次任務

#### Scenario: 搬移失敗
- **GIVEN** 目的地磁碟空間不足或路徑無寫入權限
- **WHEN** 腳本嘗試搬移
- **THEN** 該任務標記為 `FAILED_MOVE`，原始檔案**保留**在暫存目錄，腳本停止整批執行並提示使用者

---

### Requirement: 依 model 分資料夾
The system SHALL 將每個 model 的產出集中到以該 model 命名的獨立資料夾，該資料夾位於設定檔指定的輸出根目錄之下。

#### Scenario: 四個 model 的歸檔結果
- **GIVEN** 4 個 model 全部匯出成功
- **WHEN** 腳本結束
- **THEN** 輸出根目錄下存在 4 個以 model 名命名的子資料夾，每個含該 model 的 3 種格式產出

#### Scenario: 輸出根目錄含批次時間戳
- **GIVEN** 使用者在同一天執行兩次批次
- **WHEN** 第二次執行
- **THEN** 第二次的輸出根目錄與第一次不同（目錄名含日期與時間），第一次的結果未被觸碰

---

### Requirement: 保留原始檔名
The system SHALL 預設保留 AccuMark 產出的原始檔名，MUST NOT 主動改名。

> **背景**（TD-8）：檔名依版片編號產生，使用者確認實務上不會衝突並希望沿用原名，讓工廠端與 Illustrator 端看到的檔名與手動匯出完全一致。

#### Scenario: 正常無衝突
- **GIVEN** 某 model 的三種格式產出檔名互不相同
- **WHEN** 全部歸檔完成
- **THEN** 目的資料夾中的檔名與 AccuMark 原始產出**逐字相同**

---

### Requirement: 衝突時才附加區別字尾
The system SHALL 僅在目的地已存在同名檔案時，才為新檔案附加區別字尾（`_AAMA` / `_ASTM` / `_ZIP`，仍衝突則附加序號），並記錄 `WARN` 等級訊息。

#### Scenario: 兩種 DXF 原始檔名相同
- **GIVEN** 某 model 的 AAMA 與 ASTM 匯出產生的檔案原始名稱皆為 `<model>.dxf`
- **WHEN** ASTM 歸檔時偵測到目的地已有同名檔
- **THEN** 先歸檔者維持原名 `<model>.dxf`，後歸檔者成為 `<model>_ASTM.dxf`，**兩者皆完整保留**，日誌記錄一筆 WARN 說明發生衝突

#### Scenario: 附加字尾後仍衝突
- **GIVEN** 目的地連 `<model>_ASTM.dxf` 都已存在
- **WHEN** 腳本歸檔
- **THEN** 再附加序號成為 `<model>_ASTM_2.dxf`，所有既有檔案皆未被更動

#### Scenario: 強制一律加後綴
- **GIVEN** 設定檔中 `add_format_suffix` 設為 `true`
- **WHEN** 腳本歸檔
- **THEN** 每個檔案一律附加格式後綴，不論是否衝突

---

### Requirement: 絕不覆蓋既有檔案
The system SHALL 在搬移前檢查目的地是否已存在同名檔案。若存在，MUST NOT 覆蓋。

#### Scenario: 目的地已有同名檔案
- **GIVEN** 目的地資料夾已存在與待搬入檔案同名的檔案
- **WHEN** 腳本嘗試搬移
- **THEN** 腳本改以附加序號的名稱搬入（`<name>_2.dxf`），並在日誌記錄 `WARN` 等級的衝突訊息，兩個檔案皆保留

#### Scenario: 檔案內容不被修改
- **GIVEN** 任一次歸檔搬移
- **WHEN** 搬移完成
- **THEN** 目的地檔案的位元組內容與 AccuMark 原始產出完全相同（腳本只搬移與改名，MUST NOT 修改內容）
