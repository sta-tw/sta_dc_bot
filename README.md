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
- `/role_setup`：建立身份驗證面板（「驗證身份」與「申請身分組」兩個按鈕）。
- 驗證身份：已批准用戶一鍵取回先前核准的身分組。
- 申請身分組：新用戶提交申請表單，選擇應屆特選生或特選老人，系統自動建立私密申請頻道。
- `/manage_application`：管理員批准、拒絕或關閉申請，可選擇賦予的身分組。

### 其他工具
- `/exchange_setup`：建立交換備審申請面板。
- `/role_button`：建立可領取身分組的按鈕面板（Gay / Crown / Cat 類型）。
- 爆言功能：當同一則訊息累積 `⭐`（預設 3 位非機器人使用者）會自動轉發到固定爆言頻道。
- `/set_category` / `/set_current_category`：設定申請頻道所屬分類。
- `/delete_channel`：刪除機器人建立的頻道。
- `/assign_roles`：依據 JSON 檔案批次分配身分組（管理員）。
- `/sync` / `/sync_global`：強制重新同步 Slash 指令（管理員）。
- Instagram 貼文通知：每 5 分鐘輪詢設定好的公開 Instagram 個人頁面，有新貼文時在指定頻道通知並提及指定身分組，訊息會顯示預覽文字與圖片；領取身分組使用獨立的持久化面板。
- `/instagram_setup`：用 Slash Command 設定公開 Instagram 帳號、通知頻道與通知身分組。
- `/instagram_role_button`：在目前執行指令的頻道建立獨立的領取「走在時代尖端」身分組面板；不綁定 Instagram 通知頻道。

### AI 助手
- 在頻道中提及機器人即可取得 AI 回覆。
- AI 會先使用頻道上下文、長期記憶與已匯入的招生簡章；資料不足時會透過 SearXNG 搜尋工具再回答。
- `/rag_add`：由伺服器管理員或 `support_role_ids` 身分組上傳 PDF/UTF-8 文字格式簡章，供同一伺服器的 AI 查詢。
- `/llm_channel`：由伺服器管理員或 `support_role_ids` 身分組設定指定文字頻道是否啟用 LLM；停用時不會觸發 AI 回覆，也不會將頻道訊息寫入長期記憶。
- 頻道歷史會保留本機器人自己的回覆並以 assistant 角色傳給模型；其他機器人訊息會排除，其他成員與目前使用者會用穩定 ID 和說話者標籤區分。
- RAG 會先用簡章標題／內容做關鍵字重排；查詢明確提到學校時會套用來源一致性門檻，不會把其他學校的相似向量結果當成答案。
- 搜尋與簡章內容會被視為不可信參考資料，回答應標示來源，不會把其中的指令當成系統指令。

### 資源彙整編輯與審核
- `/resource_setup`：由具有「管理伺服器」權限的管理員指定五個正式資源頻道、審核頻道及通知身分組；初始化時由 Database 建立五份 Markdown 文件，Bot 在各正式頻道建立一則管理訊息。
- `/resource_editor`：發布持久化的「開啟編輯器」按鈕，啟動 Discord Activity。編輯器可修改現役文件的 Markdown 原文、預覽 Markdown 及查看 Diff。
- 提交內容會先以 `base_version` 儲存為 Draft 並建立 Review Thread；只有重新驗證後具有「管理伺服器」或「管理員」Discord 權限的人員可批准或拒絕。通知身分組只負責提醒，不代表審核權限。
- 核准後 Database 版本遞增，再由 Bot 編輯原有正式訊息；`/resource_sync` 可將 Database 正式內容重新同步到五個頻道。人工修改的 Discord 訊息不會反向寫入 Database。
- 「活動資訊分享」已停用：既有正式訊息、文件與草稿保留，但不再出現在編輯器，也不再建立草稿、審核或自動同步。

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
   - `DISCORD_CLIENT_ID`、`DISCORD_CLIENT_SECRET`：Discord Application OAuth 憑證（Client Secret 僅保留在伺服器端）
   - `RESOURCE_WEB_HOST`、`RESOURCE_WEB_PORT`：Web/API 綁定位址與連接埠（預設 `0.0.0.0:8080`）
   - `RESOURCE_STANDALONE_ENABLED`、`RESOURCE_STANDALONE_GUILD_ID`、`RESOURCE_STANDALONE_REDIRECT_URI`：只供本機獨立測試；預設停用，詳見下方說明

4. **編輯 `config/bot.json`：**

   | 欄位 | 說明 |
   |---|---|
   | `guild_id` | 伺服器 ID（設為 `0` 則使用全域同步） |
   | `welcome_channel_id` | 歡迎訊息頻道 ID |
   | `ticket_category_id` | 客服單所屬分類頻道 ID |
   | `ticket_panel_channel_id` | 顯示客服面板的文字頻道 ID |
   | `starboard_channel_id` | 爆言功能的目標文字頻道 ID（設 `0` 表示停用） |
   | `starboard_min_reactions` | 觸發爆言所需反應人數（預設 `3`） |
   | `starboard_emoji` | 觸發爆言的 emoji（預設 `⭐`） |
   | `support_role_ids` | 擁有客服權限的身分組 ID 陣列 |
   | `instagram_feed` | Instagram 公開 feed、通知頻道、通知身分組與輪詢設定 |
   | `transcript_dir` | 客服紀錄儲存路徑 |
   | `ticket_categories` | 面板可選分類（`label`、`value`、`channel_prefix`） |
   | `blocked_keywords` | 禁止出現的字詞清單 |
   | `extensions` | 要載入的 Cog 模組路徑陣列 |

---

## Instagram Feed 配置

不需要手動編輯 `config/bot.json` 的 Instagram 欄位。Bot 啟動並同步 Slash Command 後，在目標伺服器使用：

```text
/instagram_setup profile_url:https://www.instagram.com/帳號名稱/ channel:#通知頻道 role:@走在時代尖端
```

`profile_url` 也可以直接填 Instagram 帳號名稱。此指令需要伺服器管理權限或 `support_role_ids` 內的身分組，並會將設定保存到 Bot 設定檔，立即啟用輪詢。設定完成後，可以在設定的伺服器內任意頻道執行 `/instagram_role_button`，面板會建立在目前執行指令的頻道；Instagram 貼文通知本身不會附帶按鈕，仍會固定發送到 `/instagram_setup` 設定的通知頻道。`/instagram_setup` 目前設定的是單一全域 Instagram 目標；重複執行會更新現有設定。若 `INSTAGRAM_PROFILE_URL` 環境變數有值，會優先於 Slash Command 設定，請先清除該環境變數。

如果 Slash Command 尚未出現，請確認根層 `guild_id` 已設定為目標伺服器 ID 後重啟 Bot，或使用管理員的 `/sync`；全域同步可能需要等待一段時間。

Bot 會每 5 分鐘以不帶登入狀態的單次 HTTP GET 讀取公開 Instagram 個人頁面，解析頁面中公開呈現的貼文連結。**不支援 Instagram 登入、Cookie、私人 API、CAPTCHA、代理輪換或繞過反爬限制**。如果 Instagram 回傳登入頁、401/403 或暫時封鎖，Bot 會略過該次檢查，不會嘗試繞過限制。首次啟動會先記錄目前已存在的貼文，不會一次刷出歷史貼文。

貼文去重狀態會儲存在 `data/instagram_feed/{guild_or_channel_id}/state.json`，Bot 重啟後會沿用狀態，通知訊息上的領取身分組按鈕也會在啟動時重新註冊。

---

## 資源彙整 Activity 部署

資源編輯器使用與 Bot 同一個 Python 程序提供的 HTTP API，Activity 網頁位於 `/activity`，API 預設監聽 `0.0.0.0:8080`。Discord Activity 必須使用公開 HTTPS 網域；請在反向代理終止 TLS，將 HTTPS 流量轉送至 Bot 的 Web 連接埠。Docker Compose 會發布 `RESOURCE_WEB_PORT`（預設 8080）。不要將 `DISCORD_CLIENT_SECRET` 放進網頁或提交至版控。

Discord SDK 由專案內的 `/activity/discord-sdk.js` 提供，避免 Discord 代理的 CSP 擋下外部 CDN。此檔案已納入部署；更新前端 SDK 時，在 `activity/` 執行 `npm ci` 和 `npm run build`，一併更新鎖檔、bundle 及授權聲明，Bot 執行時不需要 Node.js。

首次使用前，請在 Discord Developer Portal 完成以下設定：

1. 使用 Bot 所屬的 Discord Application 啟用 Activities；在 **Activities → URL Mappings** 只保留 Prefix `/`、Target `<你的網域>`（例如 `stabot.justin0711.com`，不含 `https://`、`/activity` 或尾斜線），刪除其他重疊 Mapping。首頁 `/` 會提供編輯器，`/activity`、靜態檔及 `/api/` 也會經此 Mapping 存取。不要直接在瀏覽器開啟公開網址測試 Activity：必須在 Discord 伺服器頻道執行 `/resource_editor` 並按「開啟編輯器」，才能取得 Discord 提供的啟動參數。
2. 在 OAuth2 Redirects 登記 Activity OAuth 所需的 Redirect URI；依 Discord Activity 文件使用 `https://127.0.0.1` placeholder。Activity 的 `authorize` 與伺服器端 token exchange 不傳送 `redirect_uri`。在 `.env` 設定同一個應用程式的 `DISCORD_CLIENT_ID` 與 `DISCORD_CLIENT_SECRET`。若要使用下方的本機獨立模式，還要另外登記 `RESOURCE_STANDALONE_REDIRECT_URI` 指定的 localhost callback。
3. 讓 Activity 網域可透過 HTTPS 從 Discord 用戶端連線；不要將反向代理限制為只有 Docker 內部可存取。
4. 邀請 Bot 至目標伺服器並授予正式資源頻道的檢視、發送訊息及嵌入連結權限；審核頻道還需建立公開 Thread、在 Thread 發送訊息及管理 Thread 的權限。啟用伺服器成員意圖，供 API 驗證 Activity 使用者是否為該伺服器成員。

重啟 Bot 並同步 Slash Command 後，在目標伺服器執行 `/resource_setup`，分別選取五個不同的正式資源頻道、審核頻道及通知身分組。Bot 會初始化 Database 文件並發布 Bot 管理的正式訊息。接著在要放入口的文字頻道執行 `/resource_editor`；入口訊息可在 Bot 重啟後繼續使用。一般伺服器成員可建立 Draft；批准或拒絕時，Bot 會重新從 Discord 取得審核者的成員權限，要求「管理伺服器」或「管理員」權限。通知身分組只用於 ping 管理員，不授予審核權限。

### 本機獨立測試（不啟動 Discord Activity）

如要先用一般瀏覽器測試完整編輯流程，不必公開部署 Activity，也不需要開啟 Activity URL Mapping。此模式仍會連到 Discord OAuth、Bot 和 Database；提交 Draft 會在測試伺服器建立真正的 Review Thread。請使用專用測試伺服器及測試 Bot/Application，避免碰觸正式資料。

1. 在 `.env` 設定 `RESOURCE_STANDALONE_ENABLED=1`、`RESOURCE_STANDALONE_GUILD_ID=<測試伺服器 ID>`、`RESOURCE_WEB_HOST=127.0.0.1` 及 `RESOURCE_WEB_PORT=8080`。
2. 將 `RESOURCE_STANDALONE_REDIRECT_URI` 設為 `http://127.0.0.1:8080/api/auth/standalone/callback`，並在 Discord Developer Portal 的 OAuth2 Redirects 登記完全相同的 URI。OAuth Client Secret 只放在 Bot 的 `.env`。
3. 啟動 Bot，在測試伺服器執行 `/resource_setup`，設定五個測試資源頻道及審核頻道。Standalone 模式會限制 API、資源 Slash Command 與啟動同步只作用於 `RESOURCE_STANDALONE_GUILD_ID` 指定的伺服器。
4. 在這台電腦的瀏覽器開啟 `http://127.0.0.1:8080/activity`，按「使用 Discord 登入」後即可編輯；也可在測試伺服器執行 `/resource_editor` 取得本機網址。

此模式預設停用，且設定後 Web server 必須綁定 `127.0.0.1`；不要透過反向代理、Tunnel 或 Docker 公開埠提供此測試頁。Standalone OAuth callback 和 OAuth state 僅供本機登入，與 Activity 使用的 `https://127.0.0.1` placeholder 不同。

各伺服器的資源文件與 Draft 儲存在 `data/database/{guild_id}.db`。正式資源訊息會直接以 Markdown 文字呈現，每份文件必須不超過 2,000 字元。Bot 啟動時及核准後只會以 Database 內容更新五份現役文件的原有訊息；若現役訊息曾被人工修改，可由管理員執行 `/resource_sync` 還原，不會將 Discord 內容寫回 Database。已停用的「活動資訊分享」訊息不再同步。

---

## 身份驗證系統配置

身份驗證系統使用 JSON 檔案儲存配置與驗證記錄：

| 路徑 | 用途 |
|---|---|
| `config/guilds/{guild_id}/verification.json` | 可用身分組清單與已驗證用戶 |
| `config/guilds/{guild_id}/settings.json` | 申請分類頻道 ID、機器人建立的頻道列表 |
| `data/database/{guild_id}.db` | SQLite，儲存申請頻道資訊與狀態 |
| `config/emoji.json` | 自訂 Discord Emoji 對應表 |

首次使用前請確認：
1. 在 `config/guilds/{guild_id}/verification.json` 中設定可用身分組。
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
├── main.py
├── config/
│   ├── bot.json
│   ├── emoji.json
│   └── guilds/{guild_id}/
├── bot/
│   ├── __init__.py
│   ├── cogs/
│   └── utils/
├── utils/
├── database/
│   └── db_manager.py
└── data/
    ├── database/
    └── transcripts/
```

如需新增功能模組，在 `config/bot.json` 的 `extensions` 陣列加入模組路徑（例如 `bot.cogs.my_feature`）即可自動載入。

