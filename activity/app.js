const state = {
  sdk: null,
  token: null,
  guildId: null,
  documents: [],
  selectedSlug: null,
  document: null,
  contentMarkdown: "",
  lastPreview: null,
  showingDiff: false,
  dirty: false,
  loading: false,
};

const elements = {
  connection: document.querySelector("#connection-status"),
  meta: document.querySelector("#document-meta"),
  editStatus: document.querySelector("#edit-status"),
  editor: document.querySelector("#editor-content"),
  navigation: document.querySelector("#resource-navigation"),
  panel: document.querySelector("#channel-panel"),
  panelToggle: document.querySelector("#toggle-channel-panel"),
  previewPanel: document.querySelector("#preview-panel"),
  previewOutput: document.querySelector("#preview-output"),
  toast: document.querySelector("#toast"),
  reloadButton: document.querySelector("#reload-button"),
  previewButton: document.querySelector("#preview-button"),
  submitButton: document.querySelector("#submit-button"),
};

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

function setConnection(text, isError = false) {
  elements.connection.textContent = text;
  elements.connection.classList.toggle("error", isError);
}

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 5000);
}

async function requestJson(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  let result;
  if (contentType.includes("application/json")) {
    result = await response.json();
  } else {
    result = { message: await response.text() };
  }
  if (!response.ok) {
    throw new Error(result.message || result.error || `請求失敗 (${response.status})`);
  }
  return result;
}

function currentMarkdown() {
  return state.contentMarkdown;
}

function updateStatus() {
  if (!state.document) {
    elements.editStatus.textContent = "請先從右側選擇一份資源文件。";
    return;
  }
  elements.meta.textContent = `正式版本 v${state.document.version} · 最後更新 ${new Date(state.document.updated_at).toLocaleString("zh-TW")}`;
  elements.editStatus.textContent = state.dirty
    ? "有尚未提交的修改；正式內容尚未變更。"
    : "目前內容與 Database 正式版本一致。";
}

function renderNavigation() {
  elements.navigation.replaceChildren();
  if (!state.documents.length) {
    elements.navigation.append(el("p", "navigation-placeholder", "目前沒有可用的資源文件。"));
    return;
  }
  for (const documentData of state.documents) {
    const button = el("button", "resource-nav-item");
    button.type = "button";
    button.classList.toggle("selected", documentData.slug === state.selectedSlug);
    button.setAttribute("aria-current", documentData.slug === state.selectedSlug ? "page" : "false");
    const title = el("span", "resource-nav-title", documentData.title);
    const details = el("span", "resource-nav-meta", `v${documentData.version} · ${documentData.channel_id}`);
    button.append(title, details);
    button.addEventListener("click", () => {
      selectDocument(documentData.slug);
      if (
        window.matchMedia("(max-width: 720px)").matches &&
        state.selectedSlug === documentData.slug
      ) {
        setChannelPanelCollapsed(true);
      }
    });
    elements.navigation.append(button);
  }
}

function setChannelPanelCollapsed(collapsed) {
  elements.panel.classList.toggle("collapsed", collapsed);
  elements.panelToggle.setAttribute("aria-expanded", String(!collapsed));
  elements.panelToggle.setAttribute(
    "aria-label",
    collapsed ? "展開頻道導覽" : "收合頻道導覽",
  );
  elements.panelToggle.textContent = collapsed ? "›" : "‹";
}

function markDirty() {
  const original = state.document?.content_md || "";
  const normalizeNewlines = (value) => value.replace(/\r\n?/g, "\n");
  state.dirty = normalizeNewlines(state.contentMarkdown) !== normalizeNewlines(original);
  state.lastPreview = null;
  updateStatus();
}

function renderEditor() {
  elements.editor.replaceChildren();
  if (!state.document) {
    const empty = el("div", "empty-state");
    empty.append(
      el("div", "empty-icon", "✦"),
      el("h2", "", "載入資源文件"),
      el("p", "", "請從右側選擇要編輯的資源文件。"),
    );
    elements.editor.append(empty);
    return;
  }

  const field = el("label", "field-label markdown-label", "Markdown 原始內容");
  const hint = el("span", "editor-hint", "直接編輯完整 Markdown；提交後仍須管理員審核才會更新正式內容。最多 2,000 字元。 ");
  const textarea = el("textarea", "markdown-editor");
  textarea.id = "markdown-editor";
  textarea.rows = 28;
  textarea.maxLength = 2000;
  textarea.wrap = "soft";
  textarea.spellcheck = false;
  textarea.value = state.contentMarkdown;
  textarea.addEventListener("input", () => {
    state.contentMarkdown = textarea.value;
    markDirty();
  });
  field.append(hint, textarea);
  elements.editor.append(field);
  updateStatus();
}

function selectDocument(slug) {
  if (state.dirty && !window.confirm("切換文件會捨棄尚未提交的本機修改，要繼續嗎？")) return;
  const documentData = state.documents.find((item) => item.slug === slug);
  if (!documentData) return;
  state.selectedSlug = slug;
  state.document = documentData;
  state.contentMarkdown = documentData.content_md || "";
  state.lastPreview = null;
  state.dirty = false;
  elements.reloadButton.disabled = false;
  elements.previewButton.disabled = false;
  elements.submitButton.disabled = false;
  elements.previewPanel.hidden = true;
  renderNavigation();
  renderEditor();
  updateStatus();
}

async function getPreview() {
  if (!state.document) throw new Error("請先選擇文件。");
  const result = await requestJson(
    `/api/guilds/${encodeURIComponent(state.guildId)}/resources/${encodeURIComponent(state.document.slug)}/preview`,
    {
      method: "POST",
      body: JSON.stringify({
        base_version: state.document.version,
        content_md: currentMarkdown(),
      }),
    },
  );
  state.lastPreview = result;
  elements.previewOutput.textContent = state.showingDiff
    ? result.diff || "本次修改沒有差異。"
    : result.content_md;
  return result;
}

function setPreviewTab(showDiff) {
  state.showingDiff = showDiff;
  document.querySelector("#preview-tab").classList.toggle("active", !showDiff);
  document.querySelector("#diff-tab").classList.toggle("active", showDiff);
  if (state.lastPreview) {
    elements.previewOutput.textContent = showDiff
      ? state.lastPreview.diff || "本次修改沒有差異。"
      : state.lastPreview.content_md;
  }
}

async function reloadCurrentDocument() {
  if (!state.document) return;
  if (state.dirty && !window.confirm("重新載入會捨棄尚未提交的本機修改，要繼續嗎？")) return;
  const result = await requestJson(`/api/guilds/${encodeURIComponent(state.guildId)}/resources`);
  state.documents = result.documents;
  state.dirty = false;
  const selected = state.documents.find((item) => item.slug === state.selectedSlug) || state.documents[0];
  if (selected) {
    selectDocument(selected.slug);
    return;
  }
  state.selectedSlug = null;
  state.document = null;
  state.contentMarkdown = "";
  state.lastPreview = null;
  elements.meta.textContent = "";
  elements.reloadButton.disabled = true;
  elements.previewButton.disabled = true;
  elements.submitButton.disabled = true;
  elements.previewPanel.hidden = true;
  renderNavigation();
  renderEditor();
  updateStatus();
}

async function submitDraft() {
  if (!state.document || state.loading) return;
  state.loading = true;
  elements.submitButton.disabled = true;
  elements.editStatus.textContent = "正在驗證內容並建立審核 Draft…";
  try {
    const preview = await getPreview();
    if (!preview.diff) throw new Error("內容沒有變更，無需提交審核。");
    const result = await requestJson(
      `/api/guilds/${encodeURIComponent(state.guildId)}/resources/${encodeURIComponent(state.document.slug)}/drafts`,
      {
        method: "POST",
        body: JSON.stringify({
          base_version: state.document.version,
          content_md: currentMarkdown(),
        }),
      },
    );
    state.dirty = false;
    elements.editStatus.textContent = `Draft 已送出審核：${result.draft_id}`;
    showToast("Draft 已建立，正式內容會在管理員核准後更新。", false);
    const threadUrl = `https://discord.com/channels/${state.guildId}/${result.thread_id}`;
    const link = el("a", "review-link", "開啟 Discord Review Thread ↗");
    link.href = threadUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    elements.editor.prepend(link);
  } catch (error) {
    elements.editStatus.textContent = "提交失敗；正式內容未變更。";
    showToast(error.message, true);
  } finally {
    state.loading = false;
    elements.submitButton.disabled = !state.document;
  }
}

async function loadDocuments() {
  const result = await requestJson(`/api/guilds/${encodeURIComponent(state.guildId)}/resources`);
  state.documents = result.documents;
  renderNavigation();
  setConnection("已連線 · Draft 模式");
  elements.editStatus.textContent = "一般伺服器成員可建立 Draft；核准需要管理伺服器權限。";
  if (state.documents.length) selectDocument(state.documents[0].slug);
}

function renderStandaloneLogin(errorCode = "") {
  const mode = document.querySelector("#app-mode");
  if (mode) mode.textContent = "LOCAL STANDALONE TEST";
  const errorMessages = {
    invalid_state: "登入驗證逾時或無效，請重新登入。",
    authorization_failed: "Discord 登入失敗或授權已取消。",
    test_guild_unavailable: "Bot 尚未連線至設定的測試伺服器。",
    not_a_test_guild_member: "目前 Discord 帳號不是設定的測試伺服器成員。",
    membership_check_failed: "暫時無法確認測試伺服器成員資格。",
  };
  setConnection("本機獨立測試");
  elements.editor.replaceChildren();
  const panel = el("div", "empty-state");
  panel.append(
    el("div", "empty-icon", "✦"),
    el("h2", "", "登入以開始本機測試"),
    el("p", "", "使用 Discord 帳號登入；本機 API 僅允許設定的測試伺服器。"),
  );
  if (errorCode) panel.append(el("p", "danger-text", errorMessages[errorCode] || "登入失敗，請重試。"));
  const login = el("button", "button button-primary", "使用 Discord 登入");
  login.type = "button";
  login.addEventListener("click", () => {
    window.location.assign("/api/auth/standalone/login");
  });
  panel.append(login);
  elements.editor.append(panel);
  elements.editStatus.textContent = "獨立本機模式；登入只會授權讀取你的 Discord 身分。";
}

async function startStandalone(config) {
  const authError = new URLSearchParams(window.location.search).get("standalone_error") || "";
  const token = new URLSearchParams(window.location.hash.slice(1)).get("access_token");
  if (window.location.hash) {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  }
  if (window.location.search) {
    window.history.replaceState(null, "", window.location.pathname);
  }
  if (authError || !token) {
    renderStandaloneLogin(authError);
    return;
  }

  state.token = token;
  state.guildId = config.guild_id;
  if (!state.guildId) throw new Error("尚未設定 RESOURCE_STANDALONE_GUILD_ID。");
  const mode = document.querySelector("#app-mode");
  if (mode) mode.textContent = "LOCAL STANDALONE TEST";
  await loadDocuments();
}

async function startActivity(config) {
  if (!new URLSearchParams(window.location.search).has("frame_id")) {
    throw new Error("請從 Discord 頻道的「開啟編輯器」按鈕啟動 Activity；直接開啟網址不會取得 Discord 的啟動資訊。");
  }
  setConnection("Discord Activity 授權中…");
  const sdkModule = await import("/activity/discord-sdk.js?v=2.5.0");
  const sdk = new sdkModule.DiscordSDK(config.client_id);
  state.sdk = sdk;
  await sdk.ready();
  const authorization = await sdk.commands.authorize({
    client_id: config.client_id,
    response_type: "code",
    state: crypto.randomUUID(),
    prompt: "none",
    scope: config.scope,
  });
  const tokenResult = await requestJson("/api/auth/token", {
    method: "POST",
    body: JSON.stringify({ code: authorization.code }),
  });
  state.token = tokenResult.access_token;
  await sdk.commands.authenticate({ access_token: state.token });
  if (!sdk.guildId) throw new Error("請從 Discord 伺服器頻道開啟 Activity。 ");
  state.guildId = sdk.guildId;
  await loadDocuments();
}

async function startEditor() {
  const config = await requestJson("/api/config");
  if (config.standalone) {
    await startStandalone(config);
  } else {
    await startActivity(config);
  }
}

elements.reloadButton.addEventListener("click", () => {
  reloadCurrentDocument().catch((error) => showToast(error.message, true));
});
elements.previewButton.addEventListener("click", async () => {
  try {
    await getPreview();
    elements.previewPanel.hidden = false;
    setPreviewTab(false);
  } catch (error) {
    showToast(error.message, true);
  }
});
elements.submitButton.addEventListener("click", submitDraft);
document.querySelector("#preview-tab").addEventListener("click", () => setPreviewTab(false));
document.querySelector("#diff-tab").addEventListener("click", () => setPreviewTab(true));
document.querySelector("#close-preview").addEventListener("click", () => {
  elements.previewPanel.hidden = true;
});
if (window.matchMedia("(max-width: 720px)").matches) {
  setChannelPanelCollapsed(true);
}
elements.panelToggle.addEventListener("click", () => {
  setChannelPanelCollapsed(!elements.panel.classList.contains("collapsed"));
});

startEditor().catch((error) => {
  console.error("Resource editor initialization failed", error);
  setConnection("資源編輯器無法連線", true);
  elements.editStatus.textContent = error.message;
  elements.editor.replaceChildren();
  const panel = el("div", "empty-state error-state");
  panel.append(
    el("div", "empty-icon", "!"),
    el("h2", "", "無法載入編輯器"),
    el("p", "", error.message),
    el("p", "helper-text", "請確認 Bot 正在執行，並檢查 Activity 或本機測試模式的 OAuth 設定。"),
  );
  elements.editor.append(panel);
});
