readme by gpt-5.2-codex
## 功能概覽
- 歡迎模組：新成員加入伺服器時自動送上歡迎訊息。
- 客服單模組：
   - 固定頻道中顯示「聯絡我們」面板按鈕，使用者選擇分類後填寫表單即可開啟客服單。
   - 建立客服單時會檢查禁止字詞、提供 FAQ 建議並由 AI 送出第一則回覆。
   - `/ticket close` 匯出對話紀錄、關閉頻道並私訊開單者。
   - `/ticket panel`（需要伺服器管理權限或在 `support_role_ids` 列表內的身分組）可重新部署面板。
   - `/ticket refresh` 由提問者或客服可清空目前客服頻道訊息（刷新）。
- LLM 聊天模組：使用者 @ 機器人時，由 Cloudflare Worker AI 依提示詞與時間情境回覆，同時保護系統提示與模型資訊。
- 配置集中於 `config/settings.json` 與 `.env`，支援 Cloudflare Worker AI。

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
   - `DISCORD_TOKEN`：Discord 機器人令牌
   - `CLOUDFLARE_ACCOUNT_ID`：Cloudflare 帳戶 ID
   - `CLOUDFLARE_API_TOKEN`：Cloudflare API 令牌
   - `CLOUDFLARE_MODEL`（選填，預設 `@cf/meta/llama-3.1-8b-instruct`）
   - `LLM_SYSTEM_PROMPT`：系統提示
   - `LLM_STYLE_RULES`：風格規則
   - `LLM_CONTEXT_PREAMBLE`：提示上下文開場
   - `LLM_RESPONSE_RULES`：回覆規則（含時間情境指引）
4. 編輯 `config/settings.json`：
   - `guild_id`：伺服器 ID
   - `welcome_channel_id`：歡迎訊息頻道 ID
   - `ticket_category_id`：客服頻道分類 ID
   - `ticket_panel_channel_id`：顯示客服面板的文字頻道 ID
   - `support_role_ids`：擁有客服權限的身分組 ID 陣列
   - `ticket_categories`：面板可選分類，包含顯示名稱、頻道前綴與 AI 提示
   - `blocked_keywords`：禁止出現的字詞清單
   - `llm`：模型名稱與回覆句數上限
   - `transcript_dir`：客服紀錄儲存路徑

## 執行
```powershell
python main.py
```

第一次啟動後，Slash 指令會同步到指定伺服器（`guild_id`）。若想推播為全域指令，可將 `guild_id` 設為 `0`。

## 對話紀錄
關閉客服單時會將頻道歷史訊息寫入 `data/transcripts/` 下的文字檔，並私訊檔案給開單者。

## 開發說明
- Cog 模組位於 `bot/cogs/`
- 配置助手 `bot/utils/`
- 進入點 `main.py`

如需新增功能，只要在 `config/settings.json` 的 `extensions` 陣列加入新的模組路徑即可。