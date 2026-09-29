import logging
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

from bot.cogs.resource_library import ResourceLibraryCog


@pytest.mark.asyncio
async def test_activity_serves_bundled_discord_sdk(monkeypatch):
    monkeypatch.setenv("RESOURCE_STANDALONE_ENABLED", "0")
    monkeypatch.setenv("RESOURCE_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("RESOURCE_WEB_PORT", "0")
    cog = ResourceLibraryCog(SimpleNamespace(logger=logging.getLogger(__name__)))
    await cog._start_web_server()
    try:
        site = next(iter(cog._web_runner.sites))
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/activity") as response:
                assert response.status == 200
                index = await response.text()
                assert "app.js?v=sta-theme-4" in index
                assert "登入後會載入五份文件。" in index
                assert 'rel="icon"' in index

            async with session.get(f"http://127.0.0.1:{port}/activity/app.js") as response:
                assert response.status == 200
                script = await response.text()
                assert 'import("/activity/discord-sdk.js?v=2.5.0")' in script
                assert "https://esm.sh" not in script

            async with session.get(f"http://127.0.0.1:{port}/activity/discord-sdk.js") as response:
                assert response.status == 200
                assert "javascript" in response.content_type
                bundle = await response.text()
                assert "DiscordSDK" in bundle
                assert "https://esm.sh" not in bundle
    finally:
        await cog._web_runner.cleanup()


def test_sdk_license_is_distributed_with_bundle():
    activity_dir = Path(__file__).resolve().parents[1] / "activity"
    assert (activity_dir / "discord-sdk.js").is_file()
    assert "Copyright (c) 2024 Discord Inc." in (
        activity_dir / "discord-sdk.LICENSE.md"
    ).read_text(encoding="utf-8")
