from bot.utils.ai_context import (
    calculate_output_tokens,
    compact_prompt,
    estimate_tokens,
    trim_lines,
)


def test_trim_lines_keeps_newest_context_within_budget():
    lines = ["old message", "middle message", "newest message"]

    trimmed = trim_lines(lines, max_chars=len("middle message") + len("newest message") + 1)

    assert trimmed == ["middle message", "newest message"]


def test_estimate_tokens_is_conservative_for_mixed_text():
    assert estimate_tokens("hello world") >= 2
    assert estimate_tokens("招生簡章") == 4


def test_compact_prompt_preserves_evidence_boundaries_and_latest_request():
    prompt = (
        "開頭證據：頻道規則\n"
        + "中間資料\n" * 100
        + "結尾證據：簡章期限\n"
        + "使用者最新訊息：請問報名截止日"
    )

    compacted = compact_prompt(prompt, max_chars=180)

    assert len(compacted) <= 180
    assert "開頭證據" in compacted
    assert "結尾證據" in compacted
    assert "使用者最新訊息：" in compacted
    assert "請問報名截止日" in compacted


def test_output_budget_never_exceeds_remaining_context():
    assert calculate_output_tokens(
        input_tokens=700,
        context_window_tokens=1024,
        configured_max_tokens=512,
        safety_tokens=128,
    ) == 196

    assert calculate_output_tokens(
        input_tokens=950,
        context_window_tokens=1024,
        configured_max_tokens=512,
        safety_tokens=128,
    ) == 1

    assert calculate_output_tokens(
        input_tokens=300,
        context_window_tokens=1024,
        configured_max_tokens=0,
        safety_tokens=128,
    ) == 596


def test_compact_prompt_preserves_latest_user_message():
    prompt = "歷史資料\n" * 100 + "使用者最新訊息：請問報名截止日"

    compacted = compact_prompt(prompt, max_chars=120)

    assert len(compacted) <= 120
    assert "使用者最新訊息：" in compacted
    assert "請問報名截止日" in compacted
