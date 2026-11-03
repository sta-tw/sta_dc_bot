from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from difflib import unified_diff
from html import unescape
from typing import Any


MAX_MARKDOWN_CHARS = 20_000
MAX_DISCORD_MESSAGE_CHARS = 2_000


_HEADING_RE = re.compile(r"^(?P<prefix>#{1,6}[ \t]+)(?P<title>.*?)(?P<trailing>[ \t]*)$")
_RESOURCE_RE = re.compile(
    r"^(?P<prefix>\s*(?:[-*+]|\d+[.)])\s+)"
    r"\[(?P<name>(?:\\.|[^\]])+)\]"
    r"\((?P<url><[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"(?P<suffix>[^\r\n]*)(?P<newline>\r\n|\n|\r)?$"
)
_NEWLINE_RE = re.compile(r"(\r\n|\n|\r)$")
_INLINE_LINK_RE = re.compile(
    r"\]\(\s*(?:<(?P<angle>[^<>\r\n]*)>|(?P<bare>[^\s)]*))"
)
_REFERENCE_LINK_RE = re.compile(
    r"(?m)^[ \t]{0,3}\[(?:\\.|[^\]\r\n])+\]:[ \t]*(?:<(?P<angle>[^<>\r\n]*)>|(?P<bare>[^\s]*))"
)
_AUTOLINK_RE = re.compile(
    r"<((?:[A-Za-z][A-Za-z0-9+.-]*:|[^<>\s@]+@)[^<>\s]*)>", re.IGNORECASE
)
_HTML_HREF_RE = re.compile(
    r"\bhref\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)([\s\S]*?)(?<!`)\1(?!`)")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_URL_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_UNSAFE_SCHEMES = {"javascript", "data", "vbscript", "file", "blob"}


def _split_newline(line: str) -> tuple[str, str]:
    match = _NEWLINE_RE.search(line)
    if not match:
        return line, ""
    return line[: match.start()], match.group(1)


def _unescape_markdown(value: str) -> str:
    return re.sub(r"\\([\\\[\]])", r"\1", value)


def _escape_markdown_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _validated_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("資源 URL 必須是文字。")
    url = value.strip()
    decoded = re.sub(r"\\+:", ":", unescape(url))
    if not url or len(url) > 2048 or any(char.isspace() for char in decoded):
        raise ValueError("資源 URL 不可為空、超過 2048 字元或包含空白。")
    if any(char in decoded for char in "<>\r\n"):
        raise ValueError("資源 URL 含有不支援的字元。")
    scheme = _URL_SCHEME_RE.match(decoded)
    if scheme and scheme.group(1).lower() in _UNSAFE_SCHEMES:
        raise ValueError("資源 URL 不可使用不安全的協定。")
    return url


def _mask_markdown_code(content_md: str) -> str:
    masked_lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in content_md.splitlines(keepends=True):
        body, _ = _split_newline(line)
        if fence is not None:
            masked_lines.append("".join("\r" if char == "\r" else "\n" if char == "\n" else " " for char in line))
            marker, minimum_length = fence
            if re.fullmatch(rf" {{0,3}}{re.escape(marker)}{{{minimum_length},}}[ \t]*", body):
                fence = None
            continue

        match = _FENCE_RE.match(body)
        if match:
            marker = match.group(1)
            fence = (marker[0], len(marker))
            masked_lines.append("".join("\r" if char == "\r" else "\n" if char == "\n" else " " for char in line))
        elif body.startswith(("    ", "\t")):
            masked_lines.append("".join("\r" if char == "\r" else "\n" if char == "\n" else " " for char in line))
        else:
            masked_lines.append(line)

    without_fences = "".join(masked_lines)
    return _INLINE_CODE_RE.sub(
        lambda match: "".join(
            "\r" if char == "\r" else "\n" if char == "\n" else " "
            for char in match.group(0)
        ),
        without_fences,
    )


def validate_markdown_links(content_md: str) -> None:
    """Reject empty or unsafe Markdown link destinations."""
    if not isinstance(content_md, str):
        raise ValueError("Markdown 內容必須是文字。")

    source = _mask_markdown_code(content_md)
    destinations: list[str] = []
    for pattern in (_INLINE_LINK_RE, _REFERENCE_LINK_RE):
        for match in pattern.finditer(source):
            destinations.append(match.group("angle") or match.group("bare") or "")
    destinations.extend(match.group(1) for match in _AUTOLINK_RE.finditer(source))
    for match in _HTML_HREF_RE.finditer(source):
        destinations.append(match.group("double") or match.group("single") or match.group("bare") or "")

    for destination in destinations:
        _validated_url(destination)


def _render_resource(resource: dict[str, Any], template: dict[str, str], newline: str) -> str:
    name = resource.get("name")
    if not isinstance(name, str):
        raise ValueError("資源名稱必須是文字。")
    name = name.strip()
    if not name or len(name) > 200 or "\n" in name or "\r" in name:
        raise ValueError("資源名稱不可為空或超過 200 字元。")
    url = _validated_url(resource.get("url"))

    original_id = resource.get("id")
    if original_id == template.get("id"):
        if name == template.get("original_name") and url == template.get("original_url"):
            return template["raw"]
        prefix = template["prefix"]
        suffix = template["suffix"]
        line_ending = template["newline"]
    else:
        prefix = template.get("prefix", "- ")
        suffix = ""
        line_ending = template.get("newline", newline)

    target = f"<{url}>" if ")" in url else url
    return f"{prefix}[{_escape_markdown_text(name)}]({target}){suffix}{line_ending}"


def parse_resource_markdown(content_md: str) -> dict[str, Any]:
    """Parse ATX headings and Markdown link list items, retaining all source text."""
    if not isinstance(content_md, str):
        raise ValueError("Markdown 內容必須是文字。")

    lines = content_md.splitlines(keepends=True)
    heading_indexes: list[tuple[int, re.Match[str], str, str]] = []
    for index, line in enumerate(lines):
        body, newline = _split_newline(line)
        match = _HEADING_RE.fullmatch(body)
        if match:
            heading_indexes.append((index, match, newline, line))

    if not heading_indexes:
        return {"preamble": content_md, "sections": []}

    preamble = "".join(lines[: heading_indexes[0][0]])
    sections: list[dict[str, Any]] = []
    for section_index, (line_index, heading, heading_newline, heading_raw) in enumerate(heading_indexes):
        next_line_index = (
            heading_indexes[section_index + 1][0]
            if section_index + 1 < len(heading_indexes)
            else len(lines)
        )
        nodes: list[dict[str, Any]] = []
        resource_index = 0
        for node_index, line in enumerate(lines[line_index + 1 : next_line_index]):
            body, newline = _split_newline(line)
            resource_match = _RESOURCE_RE.fullmatch(body)
            if resource_match:
                raw_url = resource_match.group("url")
                url = raw_url[1:-1] if raw_url.startswith("<") and raw_url.endswith(">") else raw_url
                node_id = f"s{section_index}r{resource_index}"
                resource_index += 1
                nodes.append(
                    {
                        "kind": "resource",
                        "id": node_id,
                        "name": _unescape_markdown(resource_match.group("name")),
                        "url": url,
                        "raw": line,
                        "prefix": resource_match.group("prefix"),
                        "suffix": resource_match.group("suffix"),
                        "newline": resource_match.group("newline") or newline,
                    }
                )
            else:
                nodes.append(
                    {"kind": "raw", "id": f"s{section_index}x{node_index}", "raw": line}
                )

        sections.append(
            {
                "id": f"s{section_index}",
                "title": heading.group("title").strip(),
                "original_title": heading.group("title").strip(),
                "heading_raw": heading_raw,
                "heading_prefix": heading.group("prefix"),
                "heading_trailing": heading.group("trailing"),
                "heading_newline": heading_newline,
                "nodes": nodes,
                "resources": [
                    {
                        "id": node["id"],
                        "name": node["name"],
                        "url": node["url"],
                    }
                    for node in nodes
                    if node["kind"] == "resource"
                ],
                "resource_prefix": next(
                    (node["prefix"] for node in nodes if node["kind"] == "resource"), "- "
                ),
                "resource_newline": next(
                    (node["newline"] for node in nodes if node["kind"] == "resource" and node["newline"]),
                    "\n",
                ),
            }
        )

    return {"preamble": preamble, "sections": sections}


def merge_resource_markdown(content_md: str, edits: Any) -> str:
    """Apply structured category/resource edits while keeping untouched source lines verbatim."""
    if not isinstance(edits, list) or len(edits) > 100:
        raise ValueError("分類資料格式錯誤，最多可有 100 個分類。")

    parsed = parse_resource_markdown(content_md)
    source_sections = {section["id"]: section for section in parsed["sections"]}
    used_section_ids: set[str] = set()
    output_sections: list[str] = []
    added_category = False

    for section_edit in edits:
        if not isinstance(section_edit, dict):
            raise ValueError("分類資料格式錯誤。")
        title = section_edit.get("title")
        if not isinstance(title, str):
            raise ValueError("分類名稱必須是文字。")
        title = title.strip()
        if not title or len(title) > 100 or "\n" in title or "\r" in title:
            raise ValueError("分類名稱不可為空或超過 100 字元。")

        resources = section_edit.get("resources", [])
        if not isinstance(resources, list) or len(resources) > 500:
            raise ValueError("資源資料格式錯誤，每個分類最多可有 500 筆資源。")

        section_id = section_edit.get("id")
        if section_id is None:
            added_category = True
            prefix = "# "
            section_text = f"{prefix}{_escape_markdown_text(title)}\n"
            for resource in resources:
                if not isinstance(resource, dict) or resource.get("id") is not None:
                    raise ValueError("新分類中的資源格式錯誤。")
                section_text += _render_resource(
                    resource,
                    {"id": None, "prefix": "- ", "suffix": "", "newline": "\n"},
                    "\n",
                )
            output_sections.append(section_text)
            continue

        if not isinstance(section_id, str) or section_id not in source_sections:
            raise ValueError("找不到對應的原始分類，請重新載入最新內容。")
        if section_id in used_section_ids:
            raise ValueError("分類不可重複。")
        used_section_ids.add(section_id)
        original = source_sections[section_id]

        resource_templates = {
            node["id"]: node for node in original["nodes"] if node["kind"] == "resource"
        }
        ordered_resources: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen_resource_ids: set[str] = set()
        for resource in resources:
            if not isinstance(resource, dict):
                raise ValueError("資源資料格式錯誤。")
            resource_id = resource.get("id")
            if resource_id is None:
                template = {
                    "id": None,
                    "prefix": original["resource_prefix"],
                    "suffix": "",
                    "newline": original["resource_newline"],
                }
            else:
                if not isinstance(resource_id, str) or resource_id not in resource_templates:
                    raise ValueError("找不到對應的原始資源，請重新載入最新內容。")
                if resource_id in seen_resource_ids:
                    raise ValueError("資源不可重複。")
                seen_resource_ids.add(resource_id)
                template = resource_templates[resource_id]
            _validated_url(resource.get("url"))
            name = resource.get("name")
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
                raise ValueError("資源名稱不可為空或超過 200 字元。")
            ordered_resources.append((resource, template))

        if title == original["original_title"]:
            heading_line = original["heading_raw"]
        else:
            heading_line = (
                f"{original['heading_prefix']}{_escape_markdown_text(title)}"
                f"{original['heading_trailing']}{original['heading_newline']}"
            )

        resource_cursor = 0
        last_resource_position = -1
        body_parts: list[str] = []
        for node in original["nodes"]:
            if node["kind"] == "raw":
                body_parts.append(node["raw"])
            else:
                last_resource_position = len(body_parts)
                if resource_cursor < len(ordered_resources):
                    resource, template = ordered_resources[resource_cursor]
                    body_parts.append(_render_resource(resource, template, original["resource_newline"]))
                    resource_cursor += 1

        if resource_cursor < len(ordered_resources):
            extra = "".join(
                _render_resource(resource, template, original["resource_newline"])
                for resource, template in ordered_resources[resource_cursor:]
            )
            if body_parts and body_parts[-1] and not body_parts[-1].endswith(("\n", "\r")):
                extra = original["resource_newline"] + extra
            if last_resource_position >= 0:
                body_parts.insert(last_resource_position + 1, extra)
            else:
                body_parts.append(extra)

        output_sections.append(heading_line + "".join(body_parts))

    existing_order = [section["id"] for section in parsed["sections"]]
    submitted_order = [
        section.get("id")
        for section in edits
        if isinstance(section, dict) and section.get("id") is not None
    ]
    expected_order = [section_id for section_id in existing_order if section_id in used_section_ids]
    if submitted_order != expected_order:
        raise ValueError("分類順序不可變更；請只調整分類中的資源順序。")

    preamble = parsed["preamble"]
    result = preamble + "".join(output_sections)
    if added_category and len(output_sections) > 1:
        first_new_index = next(
            index for index, edit in enumerate(edits) if edit.get("id") is None
        )
        prior = "".join(output_sections[:first_new_index])
        new_and_after = "".join(output_sections[first_new_index:])
        if prior and not prior.endswith(("\n\n", "\r\n\r\n", "\r\r")):
            separator = "\n" if prior.endswith(("\n", "\r")) else "\n\n"
            result = preamble + prior + separator + new_and_after
    elif added_category and not output_sections[:-1]:
        if preamble and not preamble.endswith(("\n\n", "\r\n\r\n", "\r\r")):
            separator = "\n" if preamble.endswith(("\n", "\r")) else "\n\n"
            result = preamble + separator + output_sections[-1]

    return result


def markdown_diff(before_md: str, after_md: str) -> str:
    if before_md == after_md:
        return ""
    return "\n".join(
        unified_diff(
            before_md.splitlines(),
            after_md.splitlines(),
            fromfile="正式版本",
            tofile="提交草稿",
            lineterm="",
        )
    )


_INLINE_LINK_FRAGMENT_RE = re.compile(
    r"\[(?:\\.|[^\]\r\n])*\]\((?:<[^>\r\n]*>|[^)\r\n]*)\)"
)


def utf16_length(text: str) -> int:
    if not isinstance(text, str):
        raise TypeError("文字長度只能計算字串。")
    try:
        return len(text.encode("utf-16-le", errors="strict")) // 2
    except UnicodeEncodeError as error:
        raise ValueError("文字含有無效 Unicode 代理字元。") from error


def _utf16_prefix_lengths(text: str) -> list[int]:
    lengths = [0]
    total = 0
    for character in text:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("Markdown 含有無效 Unicode 代理字元。")
        total += 2 if codepoint > 0xFFFF else 1
        lengths.append(total)
    return lengths


def _markdown_line_spans(content_md: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    start = 0
    fence: tuple[str, int] | None = None
    while start < len(content_md):
        newline_start = start
        while newline_start < len(content_md) and content_md[newline_start] not in "\r\n":
            newline_start += 1
        if newline_start == len(content_md):
            end = newline_start
            newline = ""
        elif content_md[newline_start] == "\r" and content_md[newline_start : newline_start + 2] == "\r\n":
            end = newline_start + 2
            newline = "\r\n"
        else:
            end = newline_start + 1
            newline = content_md[newline_start:end]

        body = content_md[start:newline_start]
        inside_fence = fence is not None
        if fence is not None:
            marker, minimum_length = fence
            if re.fullmatch(rf" {{0,3}}{re.escape(marker)}{{{minimum_length},}}[ \t]*", body):
                fence = None
        else:
            match = _FENCE_RE.match(body)
            if match:
                marker = match.group(1)
                fence = (marker[0], len(marker))

        lines.append(
            {
                "start": start,
                "end": end,
                "body": body,
                "newline": newline,
                "inside_fence": inside_fence,
                "outside_fence_after": fence is None,
                "paragraph_break": not body.strip(),
            }
        )
        start = end
    return lines


def _safe_split_boundary(text: str, index: int) -> bool:
    if index <= 0 or index >= len(text):
        return True
    previous = text[index - 1]
    current = text[index]
    if previous == "\r" and current == "\n":
        return False
    if previous == "‍" or current == "‍":
        return False
    if unicodedata.category(current).startswith("M"):
        return False
    current_codepoint = ord(current)
    if 0x1F3FB <= current_codepoint <= 0x1F3FF:
        return False
    if 0xE0020 <= current_codepoint <= 0xE007F:
        return False
    if 0x1F1E6 <= ord(previous) <= 0x1F1FF and 0x1F1E6 <= current_codepoint <= 0x1F1FF:
        regional_count = 0
        cursor = index - 1
        while cursor >= 0 and 0x1F1E6 <= ord(text[cursor]) <= 0x1F1FF:
            regional_count += 1
            cursor -= 1
        if regional_count % 2:
            return False
    return True


def _protected_markdown_spans(content_md: str, prefix_lengths: list[int], max_chars: int) -> list[tuple[int, int]]:
    spans = sorted(
        (match.start(), match.end())
        for pattern in (_INLINE_CODE_RE, _INLINE_LINK_FRAGMENT_RE)
        for match in pattern.finditer(content_md)
        if prefix_lengths[match.end()] - prefix_lengths[match.start()] <= max_chars
    )
    merged: list[tuple[int, int]] = []
    for span_start, span_end in spans:
        if merged and span_start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
        else:
            merged.append((span_start, span_end))
    return merged


def _inside_protected_span(index: int, spans: list[tuple[int, int]], starts: list[int]) -> bool:
    span_index = bisect_right(starts, index) - 1
    return span_index >= 0 and spans[span_index][0] < index < spans[span_index][1]


def _next_nonblank_positions(text: str) -> list[int]:
    positions = [len(text)] * (len(text) + 1)
    next_position = len(text)
    for index in range(len(text) - 1, -1, -1):
        if not text[index].isspace():
            next_position = index
        positions[index] = next_position
    return positions


def _has_nonblank_within_limit(
    start: int,
    prefix_lengths: list[int],
    next_nonblank: list[int],
    max_chars: int,
) -> bool:
    end = bisect_right(prefix_lengths, prefix_lengths[start] + max_chars) - 1
    return next_nonblank[start] < end


def _choose_split_candidate(
    candidates: list[int],
    content_md: str,
    prefix_lengths: list[int],
    next_nonblank: list[int],
    max_chars: int,
) -> int | None:
    for boundary in sorted(set(candidates), reverse=True):
        if _safe_split_boundary(content_md, boundary) and _has_nonblank_within_limit(
            boundary, prefix_lengths, next_nonblank, max_chars
        ):
            return boundary
    return None


def _reserve_nonblank_cut(
    content_md: str,
    start: int,
    hard_end: int,
    prefix_lengths: list[int],
    next_nonblank: list[int],
    max_chars: int,
) -> int:
    boundary = hard_end - 1
    while boundary >= start and content_md[boundary].isspace():
        boundary -= 1
    while boundary > start and not _safe_split_boundary(content_md, boundary):
        boundary -= 1
    if (
        boundary <= start
        or not content_md[start:boundary].strip()
        or not _has_nonblank_within_limit(boundary, prefix_lengths, next_nonblank, max_chars)
    ):
        raise ValueError("Markdown 無法拆分成非空白的 Discord 訊息。")
    return boundary


def split_resource_markdown(
    content_md: str,
    max_chars: int = MAX_DISCORD_MESSAGE_CHARS,
) -> list[str]:
    if not isinstance(content_md, str):
        raise ValueError("Markdown 內容必須是文字。")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= MAX_MARKDOWN_CHARS:
        raise ValueError(f"單則訊息字元上限必須介於 1 至 {MAX_MARKDOWN_CHARS}。")
    total_chars = utf16_length(content_md)
    if total_chars > MAX_MARKDOWN_CHARS:
        raise ValueError(f"Markdown 不可超過 {MAX_MARKDOWN_CHARS:,} 個 UTF-16 字元。")
    if not content_md:
        return []
    if not content_md.strip():
        raise ValueError("Markdown 無法拆分成非空白的 Discord 訊息。")

    prefix_lengths = _utf16_prefix_lengths(content_md)
    next_nonblank = _next_nonblank_positions(content_md)
    lines = _markdown_line_spans(content_md)
    line_starts = [line["start"] for line in lines]
    protected_spans = _protected_markdown_spans(content_md, prefix_lengths, max_chars)
    protected_starts = [span[0] for span in protected_spans]
    chunks: list[str] = []
    start = 0

    while start < len(content_md):
        remaining_chars = prefix_lengths[-1] - prefix_lengths[start]
        if remaining_chars <= max_chars:
            final_part = content_md[start:]
            if not final_part.strip():
                raise ValueError("Markdown 無法拆分成非空白的 Discord 訊息。")
            chunks.append(final_part)
            break

        target = prefix_lengths[start] + max_chars
        hard_end = bisect_right(prefix_lengths, target) - 1
        if hard_end <= start:
            raise ValueError("字元上限不足以容納下一個 Unicode 字元。")
        safe_end = hard_end
        while safe_end > start and not _safe_split_boundary(content_md, safe_end):
            safe_end -= 1
        if safe_end > start:
            hard_end = safe_end

        line_candidates: list[int] = []
        paragraph_candidates: list[int] = []
        first_line = max(0, bisect_right(line_starts, start) - 1)
        start_line_index = bisect_right(line_starts, start) - 1
        starts_inside_fence = (
            start_line_index >= 0 and lines[start_line_index]["inside_fence"]
        )
        line_index = first_line
        while line_index < len(lines):
            line = lines[line_index]
            line_index += 1
            boundary = line["end"]
            if boundary <= start:
                continue
            if boundary > hard_end:
                break
            if not line["newline"] or not _safe_split_boundary(content_md, boundary):
                continue
            if _inside_protected_span(boundary, protected_spans, protected_starts):
                continue
            if not (line["outside_fence_after"] or starts_inside_fence):
                continue
            if not content_md[start:boundary].strip():
                continue
            line_candidates.append(boundary)
            if line["paragraph_break"]:
                paragraph_candidates.append(boundary)

        split_at = _choose_split_candidate(
            paragraph_candidates,
            content_md,
            prefix_lengths,
            next_nonblank,
            max_chars,
        )
        if split_at is None:
            split_at = _choose_split_candidate(
                line_candidates,
                content_md,
                prefix_lengths,
                next_nonblank,
                max_chars,
            )

        if split_at is None:
            structural_candidates: list[int] = []
            first_span = max(0, bisect_right(protected_starts, start) - 1)
            while first_span < len(protected_spans):
                span_start, span_end = protected_spans[first_span]
                first_span += 1
                if span_start > hard_end:
                    break
                if span_start >= start and start < span_start <= hard_end:
                    structural_candidates.append(span_start)
                if span_end > start and span_end <= hard_end:
                    structural_candidates.append(span_end)

            for boundary in range(start + 1, hard_end + 1):
                if not _safe_split_boundary(content_md, boundary):
                    continue
                if not content_md[boundary - 1].isspace():
                    continue
                if _inside_protected_span(boundary, protected_spans, protected_starts):
                    continue
                if content_md[start:boundary].strip():
                    structural_candidates.append(boundary)

            split_at = _choose_split_candidate(
                structural_candidates,
                content_md,
                prefix_lengths,
                next_nonblank,
                max_chars,
            )
        if split_at is None and _has_nonblank_within_limit(
            hard_end, prefix_lengths, next_nonblank, max_chars
        ):
            split_at = hard_end
        if split_at is None:
            split_at = _reserve_nonblank_cut(
                content_md,
                start,
                hard_end,
                prefix_lengths,
                next_nonblank,
                max_chars,
            )

        part = content_md[start:split_at]
        if not part or not part.strip():
            raise ValueError("Markdown 無法拆分成非空白的 Discord 訊息。")
        chunks.append(part)
        start = split_at

    if "".join(chunks) != content_md:
        raise RuntimeError("Markdown 拆分未能保留完整原文。")
    return chunks
