
## 功能概覽
- 歡迎模組：新成員加入伺服器時自動送上歡迎訊息。
- 客服單模組：
   - 固定頻道中顯示「聯絡我們」面板按鈕，使用者選擇分類後填寫表單即可開啟客服單。
   - 建立客服單時會檢查禁止字詞、提供 FAQ 建議並由貓娘 AI 送出第一則回覆。
   - `/ticket close` 匯出對話紀錄、關閉頻道並私訊開單者。
   - `/ticket panel`（需要伺服器管理權限或在 `support_role_ids` 列表內的身分組）可重新部署面板。
   - `/ticket refresh` 由提問者或客服可清空目前客服頻道訊息（刷新）。
- LLM 聊天模組：使用者 @ 機器人時，由 Cloudflare Worker AI 以貓娘語氣回覆，同時保護系統提示與模型資訊。
- 設定面板：於指定頻道使用 `/config_panel` 顯示互動面板，可編輯 FAQ、禁止字詞、重新載入設定並檢視即時日誌。
  - 亦可透過「編輯 LLM 設定」更新模型、人格提示、最大句數等參數。
  - `/config_set_channel` 可將當前頻道設定為面板限定頻道。
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
   - `LLM_SYSTEM_PROMPT`（選填，自訂系統提示，留空則採預設貓娘語氣）
4. 編輯 `config/settings.json`：
   - `guild_id`：伺服器 ID
   - `welcome_channel_id`：歡迎訊息頻道 ID
   - `ticket_category_id`：客服頻道分類 ID
   - `ticket_panel_channel_id`：顯示客服面板的文字頻道 ID
   - `config_channel_id`：設定面板專用頻道 ID（可為 0 表示不限）
   - `support_role_ids`：擁有客服權限的身分組 ID 陣列
   - `ticket_categories`：面板可選分類，包含顯示名稱、頻道前綴與 AI 提示
   - `faq_entries`：常見問題關鍵字與回覆內容
   - `blocked_keywords`：禁止出現的字詞清單
   - `llm`：模型名稱、貓娘 persona 與回覆句數上限
   - `transcript_dir`：客服紀錄儲存路徑

## 執行
```powershell
python main.py
```

第一次啟動後，Slash 指令會同步到指定伺服器（`guild_id`）。若想推播為全域指令，可將 `guild_id` 設為 `0`。

## 測試

詳細的測試說明請參閱 [TESTING.md](TESTING.md)。

快速運行測試：
```powershell
pytest
```

查看測試覆蓋率：
```powershell
pytest --cov=bot --cov-report=html
```

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
3. 若需遠端調整 FAQ、禁止字詞或重新載入設定，可於 `config_channel_id` 指定頻道執行 `/config_panel` 呼叫互動面板。

