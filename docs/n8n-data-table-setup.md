# n8n Data Table：測試機狀態儀表板

Daemon 會定期與狀態變更時，向 n8n 送出 **扁平 JSON**（`table_row`），由 n8n 工作流寫入 **Data Table**（內建表格）。Owner 在 n8n 介面打開 Data Tables 即可看到各台測試機當前狀態。

> 需 n8n 版本支援 Data Table 節點的 **Upsert**（建議 1.110+）。

---

## 架構

```
測試機 Daemon                    n8n
     |                            |
     |-- POST machine_status ---->| Webhook
     |    (table_row + 扁平欄位)   |     |
     |                            |     v
     |                            | Data Table: Upsert
     |                            | (條件: machine_id)
     |                            |     |
     |                            |     v
     |                            | test_machines 表
```

- **每台測試機一列**，以 `machine_id` 為唯一鍵。
- Heartbeat 每 5 分鐘更新一次；指令開始/結束、異常、復原會立即更新。

---

## 步驟 1：建立 Data Table

在 n8n 首頁 → **Data tables** → **Create table**，表名建議：`test_machines`。

| 欄位名稱 | 類型 | 說明 |
|----------|------|------|
| `machine_id` | Text | **Upsert 比對鍵**，每台機器唯一 |
| `machine_name` | Text | 顯示名稱 |
| `owner` | Text | 負責人 / 團隊 |
| `location` | Text | 機房 / 機架 |
| `hostname` | Text | OS hostname |
| `state` | Text | `idle` / `testing` / `abnormal` |
| `healthy_for_idle` | Text | `true` / `false`（Data Table 用字串較省事） |
| `current_command_id` | Text | 執行中指令 ID |
| `current_pid` | Text | 執行中 PID |
| `abnormal_reason` | Text | 異常原因 |
| `nvme_baseline` | Text | 例如 `nvme0,nvme1` |
| `nvme_current` | Text | 目前掃到的碟 |
| `nvme_dropped` | Text | 掉碟列表 |
| `last_event` | Text | 最近事件類型 |
| `last_alert_type` | Text | `disk_drop` / `crash` |
| `last_message` | Text | 說明 |
| `last_command_id` | Text | 最近指令 |
| `last_command_status` | Text | `accepted` / `running` / `success` |
| `last_updated_at` | Text | UTC 時間 ISO8601 |
| `dev_mode` | Text | 是否開發模式 |

欄位名稱需與 Daemon 送出的 JSON **完全一致**（見下方範例）。

---

## 步驟 2：建立「狀態寫入」工作流

1. 新增 Workflow，名稱例如：`NVMe - Update Machine Status`
2. 節點 1：**Webhook**
   - Method: `POST`
   - Path: 自訂，例如 `nvme-status`
   - Response: `Immediately`
3. 節點 2：**Data Table**
   - Resource: **Row**
   - Operation: **Upsert**
   - Data table: `test_machines`
   - **Conditions**: `machine_id` **Equals** `{{ $json.machine_id }}`
   - Mapping: **Map Automatically**（欄位名稱已對齊時最簡單）

   若自動對應失敗，改 **Map Each Column Manually**，從 Webhook body 對應：
   - `machine_id` ← `{{ $json.machine_id }}`
   - `state` ← `{{ $json.state }}`
   - …其餘欄位同理

4. 啟用 Workflow，複製 Webhook URL。

---

## 步驟 3：測試機環境變數

每台 Linux 測試機的 `.env`：

```bash
# 每台機器必須不同
MACHINE_ID=lab-nvme-01
MACHINE_NAME=Rack A #1
MACHINE_OWNER=storage-team
MACHINE_LOCATION=lab-shanghai

# 指向步驟 2 的 Webhook
N8N_STATUS_WEBHOOK_URL=https://your-n8n.example.com/webhook/nvme-status

# 其餘 Log / Alert Webhook 維持不變
N8N_LOG_WEBHOOK_URL=...
N8N_HEARTBEAT_WEBHOOK_URL=...
N8N_ALERT_WEBHOOK_URL=...
```

若未設定 `N8N_STATUS_WEBHOOK_URL`，狀態會改送到 `N8N_HEARTBEAT_WEBHOOK_URL`（可在同一條 Heartbeat 工作流加 Upsert 節點）。

---

## Daemon 送出的 JSON 範例

```json
{
  "event": "machine_status",
  "table_row": { "...": "..." },
  "machine_id": "lab-nvme-01",
  "machine_name": "Rack A #1",
  "state": "idle",
  "healthy_for_idle": true,
  "nvme_current": "nvme0,nvme1",
  "last_event": "heartbeat",
  "last_updated_at": "2026-05-31T08:00:00Z"
}
```

`table_row` 與外層欄位內容相同，方便 n8n 用 `$json.machine_id` 或 `$json.table_row.machine_id` 擷取。

### `last_event` 可能值

| 值 | 時機 |
|----|------|
| `startup` | Daemon 啟動 |
| `heartbeat` | 定期心跳 |
| `command_accepted` | API 接受指令 |
| `command_start` | 開始執行 |
| `command_success` | 指令成功結束 |
| `abnormal` | 掉碟 / Crash |
| `reset` | 從 abnormal 復原 |

---

## 步驟 4（建議）：Alert 也更新同一張表

複製上述 Upsert 節點到 **Alert Webhook** 工作流末尾，或讓 Alert 與 Status 共用同一 Webhook。

異常時 Daemon 會同時：
1. `POST` Alert Webhook（含 `table_row`）
2. `POST` Status Webhook（`state=abnormal`）

Owner 在 Data Table 依 `state` 篩選即可看到異常機台。

---

## 步驟 5：可選的第二張表（指令歷史）

若需要保留每次測試 Log，可另建 `command_logs` 表，在 **Log Webhook** 工作流用 Data Table **Insert**（不必 Upsert）。狀態總覽仍只看 `test_machines`。

---

## Mac 本機驗證

```bash
# .env 使用 .env.mac.example（含 N8N_STATUS_WEBHOOK_URL）
python scripts/mock_webhook_server.py
# 另一終端啟動 Daemon 後，mock 終端會印出 machine_status 列
```

---

## 常見問題

**Q: 為什麼表裡一直新增多列？**  
A: Upsert 條件未設 `machine_id`，或各機 `MACHINE_ID` 不一致。

**Q: 欄位是空的？**  
A: 檢查 Data Table 欄位名稱是否與 JSON 完全一致（區分大小寫）。

**Q: 多久更新一次？**  
A: 狀態變更即時；此外每 `HEARTBEAT_INTERVAL_SEC`（預設 300 秒）更新一次。
