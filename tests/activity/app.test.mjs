import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../activity/app.js", import.meta.url), "utf8");
const bootstrapIndex = source.lastIndexOf("\nstartEditor().catch((error) => {");
assert.notEqual(bootstrapIndex, -1);
const testableSource = `${source.slice(0, bootstrapIndex)}\nglobalThis.testApi = { state, elements, applyConfigLimits, selectDocument, getPreview, submitDraft };`;

class MockClassList {
  constructor() {
    this.values = new Set();
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  contains(value) {
    return this.values.has(value);
  }

  toggle(value, force) {
    const shouldAdd = force === undefined ? !this.values.has(value) : Boolean(force);
    if (shouldAdd) this.values.add(value);
    else this.values.delete(value);
    return shouldAdd;
  }
}

class MockElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.classList = new MockClassList();
    this.attributes = new Map();
    this.listeners = new Map();
    this.children = [];
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this.className = "";
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  prepend(...nodes) {
    this.children.unshift(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = [...nodes];
  }
}

function createHarness({ content = "", config = {}, fetchImpl } = {}) {
  const selectors = [
    "#connection-status",
    "#document-meta",
    "#edit-status",
    "#editor-content",
    "#resource-navigation",
    "#channel-panel",
    "#toggle-channel-panel",
    "#preview-panel",
    "#preview-output",
    "#toast",
    "#reload-button",
    "#preview-button",
    "#submit-button",
    "#preview-tab",
    "#diff-tab",
    "#close-preview",
  ];
  const nodes = new Map(selectors.map((selector) => [selector, new MockElement()]));
  const calls = [];
  const document = {
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, new MockElement());
      return nodes.get(selector);
    },
    createElement(tag) {
      return new MockElement(tag);
    },
  };
  const window = {
    matchMedia: () => ({ matches: false }),
    setTimeout: () => 1,
    clearTimeout: () => {},
    confirm: () => true,
    location: { assign: () => {} },
  };
  const context = vm.createContext({
    document,
    window,
    Headers,
    fetch: async (path, options) => {
      calls.push({ path, options });
      if (fetchImpl) return fetchImpl(path, options);
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ content_md: content, diff: "+changed" }),
      };
    },
    console,
  });
  vm.runInContext(testableSource, context, { filename: "activity/app.js" });
  const api = context.testApi;
  api.applyConfigLimits(config);
  api.state.documents = [
    {
      slug: "resource",
      title: "Resource",
      version: 1,
      channel_id: "123",
      updated_at: "2026-01-01T00:00:00Z",
      content_md: content,
    },
  ];
  api.selectDocument("resource");
  return { api, calls, nodes };
}

test("editor applies UTF-16 config limits and retains loaded over-limit text", () => {
  const content = "😀".repeat(10001);
  const { api, nodes } = createHarness({ content });
  const textarea = nodes.get("#editor-content").children[0].children[1];
  const counter = nodes.get("#editor-content").children[0].children[2];

  assert.equal(textarea.maxLength, 20000);
  assert.equal(textarea.value, content);
  assert.equal(api.state.contentMarkdown, content);
  assert.match(counter.textContent, /20,002 \/ 20,000 UTF-16 字元/);
  assert.equal(nodes.get("#preview-button").disabled, true);
  assert.equal(nodes.get("#submit-button").disabled, true);
  assert.equal(textarea.attributes.get("aria-invalid"), "true");
});

test("input counter updates and guards preview and submit before network requests", async () => {
  const { api, calls, nodes } = createHarness({ content: "valid" });
  const field = nodes.get("#editor-content").children[0];
  const textarea = field.children[1];
  const counter = field.children[2];
  const overLimit = "漢".repeat(20001);
  textarea.value = overLimit;
  textarea.listeners.get("input")();

  assert.equal(api.state.contentMarkdown, overLimit);
  assert.match(counter.textContent, /20,001 \/ 20,000 UTF-16 字元/);
  assert.equal(nodes.get("#preview-button").disabled, true);
  assert.equal(nodes.get("#submit-button").disabled, true);
  await assert.rejects(api.getPreview(), /UTF-16 字元上限/);
  await api.submitDraft();
  assert.equal(calls.length, 0);
});

test("preview sends the complete in-limit source without client truncation", async () => {
  const content = "漢".repeat(2000);
  const { api, calls } = createHarness({ content });
  api.state.guildId = "guild";

  const preview = await api.getPreview();
  const request = calls[0];
  const payload = JSON.parse(request.options.body);

  assert.equal(payload.content_md, content);
  assert.equal(payload.content_md.length, 2000);
  assert.equal(preview.content_md, content);
});

test("configured limits override defaults and appear in the editor help", () => {
  const { api, nodes } = createHarness({
    content: "ok",
    config: { max_markdown_chars: 16000, discord_message_chars: 1500 },
  });
  const field = nodes.get("#editor-content").children[0];
  const textarea = field.children[1];

  assert.equal(textarea.maxLength, 16000);
  assert.match(field.children[0].textContent, /1,500/);
  assert.match(field.children[0].textContent, /16,000/);
  assert.match(field.children[0].textContent, /自動拆分/);
  assert.match(field.children[0].textContent, /管理員審核/);
});
