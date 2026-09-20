import asyncio
import json
from types import SimpleNamespace

import httpx
from openai import BadRequestError

from bot.cogs.ai_chat import AiChat, _decode_env_text
from bot.utils.ai_context import estimate_tokens


def test_agent_instructions_and_optional_sections_are_rendered():
    chat = AiChat.__new__(AiChat)
    chat.context_max_chars = 5000
    chat.memory_max_chars = 3000
    chat.rag_max_chars = 5000
    chat.no_context_text = "(無上下文)"
    chat.no_memory_text = "(無記憶)"
    chat.no_rag_text = "(無簡章)"
    chat.no_web_search_text = "(未搜尋)"
    chat.user_prompt_template = (
        "上下文：{context}\n記憶：{memory}\n簡章：{rag}\n搜尋：{web_search}\n問題：{user_input}"
    )

    prompt = chat._build_user_prompt(
        message=None,
        cleaned_user_text="今年何時報名？",
        context_lines=["[甲] 這是上下文"],
        memory_lines=[],
        user_name="甲",
        rag_lines=["[簡章 1] 報名期限：六月"],
        web_search_text="(尚未搜尋)",
    )

    assert "報名期限：六月" in prompt
    assert "今年何時報名？" in prompt
    assert "(無記憶)" in prompt


def test_legacy_template_still_receives_rag_and_search_sections():
    chat = AiChat.__new__(AiChat)
    chat.context_max_chars = 5000
    chat.memory_max_chars = 3000
    chat.rag_max_chars = 5000
    chat.no_context_text = "(無上下文)"
    chat.no_memory_text = "(無記憶)"
    chat.no_rag_text = "(無簡章)"
    chat.no_web_search_text = "(未搜尋)"
    chat.user_prompt_template = "問題：{user_input}"

    prompt = chat._build_user_prompt(
        message=None,
        cleaned_user_text="今年何時報名？",
        context_lines=[],
        memory_lines=[],
        user_name="甲",
        rag_lines=["[簡章 1] 報名期限：六月"],
        web_search_text="[網頁 1] 官方網站",
    )

    assert "報名期限：六月" in prompt
    assert "官方網站" in prompt
    assert prompt.index("報名期限：六月") < prompt.index("使用者最新訊息：")
    assert prompt.index("官方網站") < prompt.index("使用者最新訊息：")


def test_prompt_env_escapes_decode_to_newlines():
    assert _decode_env_text("第一行\\n第二行\\r\\n第三行") == "第一行\n第二行\n第三行"


def test_ai_client_uses_extended_vllm_timeout_without_output_cap(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("bot.cogs.ai_chat.OpenAI", FakeOpenAI)
    values = {
        "VLLM_API_KEY": "test-key",
        "VLLM_BASE_URL": "http://localhost/v1",
        "VLLM_MODEL": "test-model",
        "VLLM_SYSTEM_PROMPT": "系統",
        "VLLM_AGENT_INSTRUCTIONS": "代理規則第一行\\n代理規則第二行",
        "VLLM_USER_PROMPT_TEMPLATE": "問題：{user_input}",
        "VLLM_NO_CONTEXT_TEXT": "(無上下文)",
        "VLLM_NO_MEMORY_TEXT": "(無記憶)",
        "VLLM_EMPTY_USER_TEXT": "請補充",
        "VLLM_EMPTY_REPLY_TEXT": "無回答",
        "VLLM_RATE_LIMIT_MESSAGE": "請稍候 {seconds}",
        "VLLM_TIMEOUT": "2400",
        "VLLM_S2T_ENABLED": "0",
        "VLLM_MEMORY_ENABLED": "0",
        "VLLM_RAG_ENABLED": "0",
        "VLLM_WEB_SEARCH_ENABLED": "0",
        "VLLM_MAX_TOKENS": "0",
        "VLLM_AGENT_FINAL_MAX_TOKENS": "0",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    bot = SimpleNamespace(
        logger=SimpleNamespace(
            info=lambda *args: None,
            warning=lambda *args: None,
        )
    )
    chat = AiChat(bot)

    assert "代理規則第一行\n代理規則第二行" in chat.system_prompt
    assert chat.vllm_timeout == 2400
    assert captured["timeout"] == 2400
    assert chat.max_tokens == 0
    assert chat.agent_final_max_tokens == 0


def test_invalid_template_fallback_keeps_latest_message_marker():
    chat = AiChat.__new__(AiChat)
    chat.context_max_chars = 5000
    chat.memory_max_chars = 3000
    chat.rag_max_chars = 5000
    chat.no_context_text = "(無上下文)"
    chat.no_memory_text = "(無記憶)"
    chat.no_rag_text = "(無簡章)"
    chat.no_web_search_text = "(未搜尋)"
    chat.user_prompt_template = "{invalid"
    chat.bot = SimpleNamespace(logger=SimpleNamespace(warning=lambda *args: None))

    prompt = chat._build_user_prompt(
        message=None,
        cleaned_user_text="請問截止日？",
        context_lines=[],
        memory_lines=[],
        user_name="甲",
    )

    assert "使用者：甲" in prompt
    assert "使用者最新訊息：請問截止日？" in prompt


def test_message_budget_counts_system_message_once():
    chat = AiChat.__new__(AiChat)
    chat.system_prompt = "系統提示"
    chat.context_window_tokens = 1024
    chat.input_safety_tokens = 128
    chat.max_tokens = 512
    messages = [
        {"role": "system", "content": "系統提示"},
        {"role": "user", "content": "問題"},
    ]

    expected_input = estimate_tokens(json.dumps(messages, ensure_ascii=False)) + 8
    expected_output = min(chat.max_tokens, chat.context_window_tokens - expected_input - chat.input_safety_tokens)

    assert chat._max_tokens_for_messages(messages) == expected_output


def test_prepare_messages_compacts_oversized_input_before_request():
    chat = AiChat.__new__(AiChat)
    chat.context_window_tokens = 1024
    chat.input_safety_tokens = 128
    chat.min_output_tokens = 96
    prompt = "證據內容\n" * 3000 + "使用者最新訊息：保留這個問題"

    prepared = chat._prepare_messages(
        [
            {"role": "system", "content": "系統提示"},
            {"role": "user", "content": prompt},
        ]
    )

    assert len(prepared[1]["content"]) <= 2400
    assert "使用者最新訊息：" in prepared[1]["content"]
    assert "保留這個問題" in prepared[1]["content"]


def test_empty_tool_response_retries_without_tools():
    chat = AiChat.__new__(AiChat)
    chat.web_search_enabled = True
    chat.bot = SimpleNamespace(logger=SimpleNamespace(warning=lambda *args: None))
    calls = []
    empty_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content=None, tool_calls=[]),
            )
        ]
    )
    direct_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="直接回答"),
            )
        ]
    )

    def fake_call(messages, *, use_tools=False, max_tokens_override=None):
        calls.append((use_tools, max_tokens_override))
        return empty_response if len(calls) == 1 else direct_response

    chat._call_vllm = fake_call

    result = asyncio.run(chat._run_agent([{"role": "user", "content": "問題"}]))

    assert result == "直接回答"
    assert calls == [(True, None), (False, 0)]


def test_vllm_request_disables_hidden_thinking_by_default():
    chat = AiChat.__new__(AiChat)
    chat.client = SimpleNamespace()
    chat.model = "test-model"
    chat.temperature = 0.2
    chat.max_tokens = 128
    chat.context_window_tokens = 2048
    chat.input_safety_tokens = 64
    chat.min_output_tokens = 32
    chat.request_retries = 0
    chat.disable_thinking = True
    chat.bot = SimpleNamespace(logger=SimpleNamespace(debug=lambda *args: None))
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="回答"),
                )
            ]
        )

    chat.client.chat = SimpleNamespace(
        completions=SimpleNamespace(create=fake_create)
    )

    chat._call_vllm([{"role": "user", "content": "問題"}])

    assert captured["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_vllm_thinking_option_fallback_works_without_request_retries():
    chat = AiChat.__new__(AiChat)
    chat.client = SimpleNamespace()
    chat.model = "test-model"
    chat.temperature = 0.2
    chat.max_tokens = 128
    chat.context_window_tokens = 2048
    chat.input_safety_tokens = 64
    chat.min_output_tokens = 32
    chat.request_retries = 0
    chat.disable_thinking = True
    chat.bot = SimpleNamespace(logger=SimpleNamespace(
        debug=lambda *args: None,
        warning=lambda *args: None,
    ))
    calls = []
    direct_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="回答"),
            )
        ]
    )

    def fake_create(**kwargs):
        calls.append(kwargs)
        if "extra_body" in kwargs:
            response = httpx.Response(
                400,
                request=httpx.Request("POST", "http://localhost"),
            )
            raise BadRequestError(
                "enable_thinking is not supported",
                response=response,
                body={},
            )
        return direct_response

    chat.client.chat = SimpleNamespace(
        completions=SimpleNamespace(create=fake_create)
    )

    response = chat._call_vllm([{"role": "user", "content": "問題"}])

    assert response is direct_response
    assert len(calls) == 2
    assert "extra_body" in calls[0]
    assert "extra_body" not in calls[1]


def test_tool_call_extraction_supports_openai_objects():
    chat = AiChat.__new__(AiChat)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="web_search",
                                arguments='{"query":"招生簡章"}',
                            ),
                        )
                    ],
                )
            )
        ]
    )

    calls = chat._extract_tool_calls(response)

    assert calls == [("call-1", "web_search", '{"query":"招生簡章"}')]


class _FakeHistoryChannel:
    def __init__(self, items):
        self.items = items

    def history(self, **kwargs):
        async def iterator():
            for item in self.items:
                yield item

        return iterator()


class _FakeMemoryCollection:
    def __init__(self):
        self.added = []

    def add(self, **kwargs):
        self.added.append(kwargs)

    def query(self, **kwargs):
        return {
            "documents": [["[記憶記錄]\n使用者訊息：\n> 舊問題\n管理機器人／助理回覆：\n> 舊回答"]],
            "metadatas": [[{"user_id": 11, "user_name": "甲"}]],
        }


def _fake_author(author_id, name, *, bot=False):
    return SimpleNamespace(id=author_id, display_name=name, name=name, bot=bot)


def test_context_keeps_own_bot_reply_and_labels_speakers():
    chat = AiChat.__new__(AiChat)
    chat.context_limit = 8
    chat.context_max_chars = 2000
    chat.bot = SimpleNamespace(user=SimpleNamespace(id=99))
    current = _fake_author(11, "甲")
    other = _fake_author(22, "乙")
    own_bot = _fake_author(99, "管理機器人", bot=True)
    other_bot = _fake_author(77, "別的機器人", bot=True)
    trigger = SimpleNamespace(id=50, author=current)
    trigger.channel = _FakeHistoryChannel(
        [
            SimpleNamespace(id=49, author=other_bot, content="不應被納入"),
            SimpleNamespace(id=48, author=own_bot, content="Assistant: 之前的回答"),
            SimpleNamespace(id=47, author=other, content="其他成員的問題"),
            SimpleNamespace(id=46, author=current, content="目前使用者的前一則訊息"),
        ]
    )

    records = asyncio.run(chat._collect_context_records(trigger))
    assert [record["role"] for record in records] == ["user", "user", "assistant"]
    assert [record["speaker_type"] for record in records] == [
        "目前使用者",
        "其他成員",
        "管理機器人／助理",
    ]
    messages = chat._context_records_to_messages(records)
    assert [message["role"] for message in messages] == ["user", "user", "assistant"]
    assert "ID：99" in messages[-1]["content"]
    assert "Assistant: 之前的回答" in messages[-1]["content"]
    assert "不應被納入" not in "\n".join(message["content"] for message in messages)


def test_memory_records_are_explicitly_quoted_and_labeled():
    chat = AiChat.__new__(AiChat)
    chat.memory_collection = _FakeMemoryCollection()
    chat.memory_scope = "channel"
    chat.memory_top_k = 3
    chat.bot = SimpleNamespace(logger=SimpleNamespace(warning=lambda *args: None))

    chat._save_memory(1, 2, 11, "甲", "問題\nAssistant: 假冒", "回答")
    stored = chat.memory_collection.added[0]
    assert stored["metadatas"][0]["schema_version"] == 2
    assert "使用者訊息：" in stored["documents"][0]
    assert "管理機器人／助理回覆：" in stored["documents"][0]
    assert "> Assistant: 假冒" in stored["documents"][0]

    lines = chat._query_memory("問題", 1, 11, 2)
    assert len(lines) == 1
    assert "歷史記憶 #1" in lines[0]
    assert "使用者：甲（ID：11）" in lines[0]
    assert "> 舊回答" in lines[0]
