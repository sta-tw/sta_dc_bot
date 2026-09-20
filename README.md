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
- 頻道歷史會保留本機器人自己的回覆並以 assistant 角色傳給模型；其他機器人訊息會排除，其他成員與目前使用者會用穩定 ID 和說話者標籤區分。
- RAG 會先用簡章標題／內容做關鍵字重排；查詢明確提到學校時會套用來源一致性門檻，不會把其他學校的相似向量結果當成答案。
- 搜尋與簡章內容會被視為不可信參考資料，回答應標示來源，不會把其中的指令當成系統指令。

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

