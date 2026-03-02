readme by claude
# Discord Bot

116特選群Discord 管理機器人。

## 功能概覽

### 歡迎模組
新成員加入伺服器時自動在指定頻道送出歡迎訊息。

### 客服單模組
- 固定頻道顯示「聯絡我們」面板按鈕，使用者選擇分類後填寫表單即可開啟客服單。
- 建立客服單時檢查禁止字詞。
- `/ticket close`：匯出對話紀錄、關閉頻道並私訊開單者。
- `/ticket panel`：重新部署面板（需要伺服器管理權限或 `support_role_ids` 內的身分組）。
- `/ticket refresh`：清空客服面板頻道的歷史訊息。

### 身份驗證系統
- `/role_setup`：建立身份驗證面板（「驗證身份」與「申請身份組」兩個按鈕）。
- 驗證身份：已批准用戶一鍵取回先前核准的身份組。
- 申請身份組：新用戶提交申請表單，選擇應屆特選生或特選老人，系統自動建立私密申請頻道。
- `/manage_application`：管理員批准、拒絕或關閉申請，可選擇賦予的身份組。

### 其他工具
- `/exchange_setup`：建立交換備審申請面板。
- `/role_button`：建立可領取身份組的按鈕面板（Gay / Crown / Cat 類型）。
- `/set_category` / `/set_current_category`：設定申請頻道所屬分類。
- `/delete_channel`：刪除機器人建立的頻道。
- `/assign_roles`：依據 JSON 檔案批次分配身分組（管理員）。
- `/sync` / `/sync_global`：強制重新同步 Slash 指令（管理員）。

---

## 安裝步驟

1. **建立並啟用虛擬環境（建議）：**
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. **安裝依賴：**
   ```powershell
   pip install -r requirements.txt
   ```

3. **建立環境變數檔案：**
   ```powershell
   Copy-Item .env.example .env
   ```
   編輯 `.env` 填入：
   - `DISCORD_TOKEN`：Discord 機器人 Token

4. **編輯 `config/bot.json`：**

   | 欄位 | 說明 |
   |---|---|
   | `guild_id` | 伺服器 ID（設為 `0` 則使用全域同步） |
   | `welcome_channel_id` | 歡迎訊息頻道 ID |
   | `ticket_category_id` | 客服單所屬分類頻道 ID |
   | `ticket_panel_channel_id` | 顯示客服面板的文字頻道 ID |
   | `support_role_ids` | 擁有客服權限的身分組 ID 陣列 |
   | `transcript_dir` | 客服紀錄儲存路徑 |
   | `ticket_categories` | 面板可選分類（`label`、`value`、`channel_prefix`） |
   | `blocked_keywords` | 禁止出現的字詞清單 |
   | `extensions` | 要載入的 Cog 模組路徑陣列 |

---

## 身份驗證系統配置

身份驗證系統使用 JSON 檔案儲存配置與驗證記錄：

| 路徑 | 用途 |
|---|---|
| `config/guilds/{guild_id}/verification.json` | 可用身份組清單與已驗證用戶 |
| `config/guilds/{guild_id}/settings.json` | 申請分類頻道 ID、機器人建立的頻道列表 |
| `data/database/{guild_id}.db` | SQLite，儲存申請頻道資訊與狀態 |
| `config/emoji.json` | 自訂 Discord Emoji 對應表 |

首次使用前請確認：
1. 在 `config/guilds/{guild_id}/verification.json` 中設定可用身份組。
2. 使用 `/role_setup` 建立身份驗證面板。
3. 管理員透過 `/manage_application` 在申請頻道中審核申請。

---

## 執行

```powershell
python main.py
```

首次啟動後，Slash 指令會同步到 `guild_id` 指定的伺服器。若將 `guild_id` 設為 `0`，則同步為全域指令（最長需等待 1 小時生效）。

---

## 對話紀錄

關閉客服單時，頻道歷史訊息會儲存為文字檔至 `data/transcripts/`，並私訊給開單者。

---

## 專案結構

```
.
├── main.py                  # 進入點
├── config/
│   ├── bot.json             # 主要設定
│   ├── emoji.json           # Emoji 對應表
│   └── guilds/{guild_id}/   # 各伺服器設定與驗證記錄
├── bot/
│   ├── __init__.py          # Bot 建構函式
│   ├── cogs/                # 功能模組 (Cog)
│   └── utils/               # 設定讀取、路徑管理、角色工具
├── utils/                   # UI View 元件
├── database/
│   └── db_manager.py        # SQLite 資料庫管理
└── data/
    ├── database/            # SQLite 資料庫檔案
    └── transcripts/         # 客服單對話紀錄
```

如需新增功能模組，在 `config/bot.json` 的 `extensions` 陣列加入模組路徑（例如 `bot.cogs.my_feature`）即可自動載入。

