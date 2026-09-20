from __future__ import annotations

import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


DEFAULT_USER_AGENT = "sta-dc-bot/1.0 (+public-instagram-profile)"
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_PROFILE_BYTES = MAX_FEED_BYTES
MAX_PREVIEW_TEXT_CHARS = 1000
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
_PROFILE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._]+$")
_POST_PATH_RE = re.compile(
    r"(?:https?:)?//(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_RELATIVE_POST_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)
_SHORTCODE_RE = re.compile(
    r"[\"']shortcode[\"']\s*:\s*[\"']([A-Za-z0-9_-]{5,})[\"']"
)
_TIMESTAMP_RE = re.compile(
    r"[\"'](?:taken_at_timestamp|taken_at|timestamp)[\"']\s*:\s*(\d{9,})"
)
_IMAGE_RE = re.compile(
    r"[\"'](?:display_url|display_uri|thumbnail_src|thumbnail_url|image_url)[\"']\s*:\s*[\"']([^\"']+)[\"']"
)
_ACCESSIBILITY_CAPTION_RE = re.compile(
    r'"accessibility_caption"\s*:\s*"((?:\\.|[^"\\])*)"'
)
_CAPTION_TEXT_RE = re.compile(
    r'"caption"\s*:\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"'
)


@dataclass(frozen=True, slots=True)
class InstagramPost:
    """A post extracted from a public Instagram page or feed."""

    source_key: str
    link: str
    title: str = ""
    preview_text: str = ""
    thumbnail_url: str | None = None
    published_at: datetime | None = None


FeedItem = InstagramPost


def is_valid_feed_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def is_valid_instagram_profile_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if (parsed.hostname or "").lower() not in _INSTAGRAM_HOSTS:
        return False

    segments = [segment for segment in parsed.path.split("/") if segment]
    return (
        bool(segments)
        and segments[0].lower() not in {"p", "reel", "tv", "accounts", "explore"}
        and bool(_PROFILE_SEGMENT_RE.fullmatch(segments[0]))
    )


def normalise_instagram_profile_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        if value.startswith(("instagram.com/", "www.instagram.com/")):
            return f"https://{value}"
        if "/" not in value:
            return f"https://www.instagram.com/{value.lstrip('@')}/"
    return value


def fetch_feed(
    url: str,
    *,
    timeout: float = 20.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """Fetch one public feed without cookies, authentication, or browser emulation."""

    url = (url or "").strip()
    if not is_valid_feed_url(url):
        raise ValueError("Instagram feed URL must use http or https")

    request = Request(
        url,
        headers={
            "User-Agent": user_agent.strip() or DEFAULT_USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        },
    )
    with urlopen(request, timeout=max(1.0, float(timeout))) as response:
        payload = response.read(MAX_FEED_BYTES + 1)

    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("Instagram feed response is too large")
    return payload


def fetch_public_profile(
    url: str,
    *,
    timeout: float = 20.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """Fetch one public profile page without login state or anti-bot workarounds."""

    url = (url or "").strip()
    if not is_valid_instagram_profile_url(url):
        raise ValueError("Instagram profile URL must point to a public profile")

    request = Request(
        url,
        headers={
            "User-Agent": user_agent.strip() or DEFAULT_USER_AGENT,
            "Accept": "text/html, application/xhtml+xml",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        },
    )
    with urlopen(request, timeout=max(1.0, float(timeout))) as response:
        payload = response.read(MAX_PROFILE_BYTES + 1)

    if len(payload) > MAX_PROFILE_BYTES:
        raise ValueError("Instagram profile response is too large")
    return payload


def parse_public_profile(
    payload: bytes | str,
    profile_url: str,
) -> list[InstagramPost]:
    """Extract post links exposed in the public profile HTML.

    Instagram changes its server-rendered markup regularly. This parser only
    consumes ordinary public HTML/embedded metadata; it does not call private
    endpoints or try to defeat login, CAPTCHA, or rate-limit responses.
    """

    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="replace")
    else:
        text = payload
    text = html.unescape(text).replace("\\/", "/")
    base_url = profile_url.rstrip("/") + "/"

    candidates: list[tuple[str, int]] = []
    for match in _POST_PATH_RE.finditer(text):
        candidates.append((match.group(0), match.start()))

    for match in _RELATIVE_POST_RE.finditer(text):
        candidates.append((urljoin(base_url, match.group(0)), match.start()))

    for match in _SHORTCODE_RE.finditer(text):
        candidates.append((f"https://www.instagram.com/p/{match.group(1)}/", match.start()))

    candidates.sort(key=lambda item: item[1])
    posts: list[InstagramPost] = []
    seen_keys: set[str] = set()
    for link, position in candidates:
        link = _normalise_post_link(link)
        source_key = _normalise_link(link)
        if not link or not source_key or source_key in seen_keys:
            continue

        context = text[max(0, position - 600) : position + 1200]
        metadata_context = _metadata_context(text, _post_shortcode(link))
        if metadata_context:
            context = f"{metadata_context}\n{context}"
        published_at = _timestamp_from_context(context)
        thumbnail_url = _image_from_context(context)
        preview_text = _preview_from_context(context)
        seen_keys.add(source_key)
        posts.append(
            InstagramPost(
                source_key=source_key,
                link=link,
                title="Instagram 新貼文",
                preview_text=preview_text,
                thumbnail_url=thumbnail_url,
                published_at=published_at,
            )
        )

    return posts


def _normalise_post_link(link: str) -> str:
    link = (link or "").strip().replace("\\/", "/")
    if link.startswith("//"):
        link = f"https:{link}"
    if link.startswith("/"):
        link = urljoin("https://www.instagram.com/", link)

    parsed = urlparse(link)
    if (parsed.hostname or "").lower() not in _INSTAGRAM_HOSTS:
        return ""
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or segments[0].lower() not in {"p", "reel", "tv"}:
        return ""
    shortcode = segments[1]
    return f"https://www.instagram.com/{segments[0].lower()}/{shortcode}/"


def normalise_instagram_post_url(value: str) -> str:
    """Return a canonical public Instagram post URL, or an empty string."""

    return _normalise_post_link(value)


def _post_shortcode(link: str) -> str:
    segments = [segment for segment in urlparse(link).path.split("/") if segment]
    return segments[1] if len(segments) >= 2 else ""


def _metadata_context(text: str, shortcode: str) -> str:
    if not shortcode:
        return ""
    pattern = re.compile(
        rf"[\"'](?:code|shortcode)[\"']\s*:\s*[\"']{re.escape(shortcode)}[\"']"
    )
    match = pattern.search(text)
    if not match:
        return ""
    return text[max(0, match.start() - 200) : match.start() + 5000]


def _timestamp_from_context(context: str) -> datetime | None:
    match = _TIMESTAMP_RE.search(context)
    if not match:
        return None
    try:
        return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _image_from_context(context: str) -> str | None:
    match = _IMAGE_RE.search(context)
    if not match:
        return None
    candidate = html.unescape(match.group(1)).replace("\\/", "/")
    return candidate if is_valid_feed_url(candidate) else None


def _preview_from_context(context: str) -> str:
    for pattern in (_CAPTION_TEXT_RE, _ACCESSIBILITY_CAPTION_RE):
        match = pattern.search(context)
        if match:
            return _decode_embedded_text(match.group(1))
    return ""


def _decode_embedded_text(value: str) -> str:
    try:
        decoded = json.loads(f'"{value}"')
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = html.unescape(value).replace("\\/", "/")
    return _clean_preview_text(str(decoded))


def _clean_preview_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = " ".join(value.split())
    return value[:MAX_PREVIEW_TEXT_CHARS]


def parse_feed(payload: bytes | str) -> list[InstagramPost]:
    """Parse RSS 2.0 or Atom XML while ignoring namespaced tag prefixes."""

    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    root = ET.fromstring(payload)
    posts: list[InstagramPost] = []
    seen_keys: set[str] = set()

    for record in root.iter():
        if _local_name(record.tag) not in {"item", "entry"}:
            continue

        link = _extract_link(record)
        if not link:
            continue

        title = _clean_text(_find_text(record, {"title"}))
        preview_text = _clean_preview_text(
            _find_text(record, {"description", "summary", "subtitle", "content"})
        )
        published_at = _parse_datetime(
            _find_text(record, {"pubdate", "published", "updated", "date"})
        )
        thumbnail_url = _extract_thumbnail(record)
        source_key = _normalise_link(link)
        if not source_key:
            source_key = _fallback_key(title, published_at)

        if source_key in seen_keys:
            continue
        seen_keys.add(source_key)
        posts.append(
            InstagramPost(
                source_key=source_key,
                link=link,
                title=title,
                preview_text=preview_text,
                thumbnail_url=thumbnail_url,
                published_at=published_at,
            )
        )

    return posts


def save_instagram_configuration(
    path: Path,
    *,
    enabled: bool,
    profile_url: str,
    guild_id: int,
    channel_id: int,
    role_id: int,
) -> None:
    """Persist the Instagram block without replacing unrelated bot settings."""

    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Bot configuration must contain a JSON object")

    configured = data.get("instagram_feed")
    if not isinstance(configured, dict):
        configured = {}
    configured.update(
        {
            "enabled": bool(enabled),
            "profile_url": profile_url,
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "role_id": int(role_id),
        }
    )
    data["instagram_feed"] = configured

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def empty_state(source_url: str = "") -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_url": source_url,
        "initialized": False,
        "watermark": None,
        "seen": {},
    }


def load_state(path: Path) -> dict[str, object]:
    """Load state defensively; a missing or corrupt file starts cleanly."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
        return empty_state()

    if not isinstance(data, dict):
        return empty_state()

    seen_data = data.get("seen")
    if not isinstance(seen_data, dict):
        return empty_state()

    seen = {
        str(key): str(value)
        for key, value in seen_data.items()
        if str(key).strip() and str(value).strip()
    }
    return {
        "schema_version": int(data.get("schema_version", 1) or 1),
        "source_url": str(data.get("source_url", data.get("feed_url", "")) or ""),
        "initialized": bool(data.get("initialized", bool(seen))),
        "watermark": data.get("watermark"),
        "seen": seen,
    }


def save_state(path: Path, state: dict[str, object]) -> None:
    """Atomically persist duplicate state so a restart cannot leave a partial JSON file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def prune_seen(
    state: dict[str, object],
    *,
    retention_days: int = 30,
    max_items: int = 5000,
    now: datetime | None = None,
) -> None:
    """Keep the duplicate watermark bounded while retaining recent post IDs."""

    seen_data = state.get("seen")
    if not isinstance(seen_data, dict):
        state["seen"] = {}
        return

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    retained: list[tuple[str, str, datetime]] = []
    for key, raw_timestamp in seen_data.items():
        timestamp = _parse_datetime(str(raw_timestamp))
        if timestamp is None or timestamp >= cutoff:
            if timestamp is None:
                timestamp = now
            retained.append((str(key), str(raw_timestamp), timestamp))

    retained.sort(key=lambda item: item[2], reverse=True)
    state["seen"] = {
        key: raw_timestamp for key, raw_timestamp, _ in retained[: max(1, int(max_items))]
    }


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].split(":", 1)[-1].lower()


def _find_text(record: ET.Element, names: set[str]) -> str:
    for child in record.iter():
        if _local_name(child.tag) in names:
            text = _clean_text("".join(child.itertext()))
            if text:
                return text
    return ""


def _extract_link(record: ET.Element) -> str:
    candidates: list[tuple[bool, str]] = []
    for child in record.iter():
        if _local_name(child.tag) != "link":
            continue

        href = str(child.attrib.get("href", "")).strip()
        text = _clean_text("".join(child.itertext()))
        value = href or text
        if not is_valid_feed_url(value):
            continue
        relation = str(child.attrib.get("rel", "alternate")).lower()
        candidates.append((relation != "self", value))

    candidates.sort(key=lambda item: item[0], reverse=True)
    if candidates:
        return candidates[0][1]

    for name in ("guid", "id"):
        value = _find_text(record, {name})
        if is_valid_feed_url(value):
            return value
    return ""


def _extract_thumbnail(record: ET.Element) -> str | None:
    candidate_names = {"thumbnail", "content", "enclosure", "image"}
    for child in record.iter():
        name = _local_name(child.tag)
        if name not in candidate_names:
            continue

        candidates = [child.attrib.get("url"), child.attrib.get("href")]
        if name == "image":
            candidates.append(_find_text(child, {"url"}))
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if is_valid_feed_url(candidate):
                return candidate
    return None


def _parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None

    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_link(link: str) -> str:
    parsed = urlparse((link or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            netloc,
            path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def _fallback_key(title: str, published_at: datetime | None) -> str:
    published = published_at.isoformat() if published_at else ""
    digest = hashlib.sha256(f"{title}|{published}".encode("utf-8")).hexdigest()
    return f"fallback:{digest}"


def _clean_text(value: str) -> str:
    return " ".join((value or "").split())
