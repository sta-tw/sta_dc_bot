test
## 功能概覽
- 歡迎模組：新成員加入伺服器時自動送上歡迎訊息。
- 客服單模組：
   - 固定頻道中顯示「聯絡 HackIt」面板按鈕，使用者按下後填寫表單即可開啟客服單。
   - `/ticket close` 匯出對話紀錄、關閉頻道並私訊開單者。
   - `/ticket panel`（需要伺服器管理權限或在 `support_role_ids` 列表內的身分組）可重新發布面板訊息。
- LLM 聊天模組：使用者 @ 機器人時，由 Gemini-2.0-Flash 回覆，附帶前 10 則上下文與上一則回覆資訊。
- 配置集中於 `config/settings.json` 與 `.env`。

## 安裝步驟
1. 建立並啟用虛擬環境（建議）：
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. 安裝依賴：
   ```powershell
   pip install -r requirements.txt
   ```
3. 建立 `.env`（可由 `.env.example` 複製）並填入：
   - `DISCORD_TOKEN`
   - `GEMINI_API_KEY`
   - `GEMINI_MODEL`（預設 `gemini-2.0-flash`）
   - `LLM_SYSTEM_PROMPT`
4. 編輯 `config/settings.json`：
   - `guild_id`：伺服器 ID
   - `welcome_channel_id`：歡迎訊息頻道 ID
   - `ticket_category_id`：客服頻道分類 ID
   - `ticket_panel_channel_id`：顯示客服面板的文字頻道 ID
   - `support_role_ids`：擁有客服權限的身分組 ID 陣列
   - `transcript_dir`：客服紀錄儲存路徑

## 執行
```powershell
py main.py
```

第一次啟動後，Slash 指令會同步到指定伺服器（`guild_id`）。若想推播為全域指令，可將 `guild_id` 設為 `0`。

## 對話紀錄
關閉客服單時會將頻道歷史訊息寫入 `data/transcripts/` 下的文字檔，並私訊檔案給開單者。

## 開發說明
- Cog 模組位於 `bot/cogs/`
- 配置助手 `bot/utils/`
- 進入點 `main.py`

如需新增功能，只要在 `config/settings.json` 的 `extensions` 陣列加入新的模組路徑即可。

## 面板部署提醒
1. 確認 `ticket_panel_channel_id` 指向要展示面板的頻道。
2. 啟動機器人後由具權限的成員執行 `/ticket panel`，即可生成帶有按鈕與嵌入說明的訊息。
