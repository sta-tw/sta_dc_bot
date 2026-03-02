from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def test_all_cogs_import():
    from bot.cogs.welcome import Welcome
    from bot.cogs.tickets import TicketCog
    from bot.cogs.admin_tools import AdminTools
    from bot.cogs.moderation import ModerationCog
    from bot.cogs.role_setup import Role_Setup
    from bot.cogs.manage_application import Manage_Application
    from bot.cogs.delete_channel import Delete_Channel
    from bot.cogs.set_category import Set_Category
    from bot.cogs.exchange_setup import Exchange_Setup
    from bot.cogs.role_button import Role_Button

    assert Welcome is not None
    assert TicketCog is not None
    assert AdminTools is not None
    assert ModerationCog is not None
    assert Role_Setup is not None
    assert Manage_Application is not None
    assert Delete_Channel is not None
    assert Set_Category is not None
    assert Exchange_Setup is not None
    assert Role_Button is not None

def test_all_ui_modules_import():
    from utils.role_ui import Verfication_View, setup_persistent_views_role
    from utils.exchange_ui import Exchange_View, setup_persistent_views_exchange
    from utils.role_button_ui import Gay, Crown, Cat, setup_persistent_views_role_button

    assert Verfication_View is not None
    assert Exchange_View is not None
    assert Gay is not None
    assert Crown is not None
    assert Cat is not None
    assert setup_persistent_views_role is not None
    assert setup_persistent_views_exchange is not None
    assert setup_persistent_views_role_button is not None

def test_database_manager_import():
    from database.db_manager import DatabaseManager

    assert DatabaseManager is not None

    required_methods = [
        'init_db',
        'save_application_channel',
        'get_application_channel',
        'get_all_applications',
        'update_application_status',
        'get_applications_by_status',
        'save_verification_role',
        'get_verification_roles',
        'load_guild_settings',
        'save_guild_settings',
        'get_available_roles',
        'save_emoji',
        'load_emoji',
        'get_application_category',
        'save_application_category',
        'register_bot_created_channel'
    ]

    for method_name in required_methods:
        assert hasattr(DatabaseManager, method_name), f"DatabaseManager missing method: {method_name}"

def test_bot_build():
    from bot import build_bot
    from pathlib import Path

    bot = build_bot(Path("config/bot.json"))

    assert hasattr(bot, 'logger')
    assert hasattr(bot, 'emoji')
    assert hasattr(bot, 'get_emoji')
    assert hasattr(bot, 'settings')

    assert isinstance(bot.emoji, dict)
    assert len(bot.emoji) > 0

    print(f"✅ Bot built successfully with {len(bot.emoji)} emojis loaded")

def test_config_files_exist():
    from pathlib import Path

    required_files = [
        'config/bot.json',
        'config/emoji.json',
        'main.py',
        'requirements.txt'
    ]

    for file_path in required_files:
        path = Path(file_path)
        assert path.exists(), f"Required file not found: {file_path}"
        print(f"Found: {file_path}")

def test_emoji_config_valid():
    import json
    from pathlib import Path

    emoji_file = Path("config/emoji.json")
    assert emoji_file.exists(), "emoji.json not found"

    with open(emoji_file, 'r', encoding='utf-8') as f:
        emoji_data = json.load(f)

    assert 'emojis' in emoji_data, "emoji.json missing 'emojis' key"
    assert isinstance(emoji_data['emojis'], dict), "'emojis' should be a dictionary"

    essential_emojis = ['F', 'arrow', 'verify_check', 'send', 'cheers']
    for emoji_name in essential_emojis:
        assert emoji_name in emoji_data['emojis'], f"Missing essential emoji: {emoji_name}"

    print(f" emoji.json valid with {len(emoji_data['emojis'])} emojis")

if __name__ == "__main__":
    print("Running integration tests...\n")

    tests = [
        ("Cog imports", test_all_cogs_import),
        ("UI module imports", test_all_ui_modules_import),
        ("DatabaseManager", test_database_manager_import),
        ("Config files", test_config_files_exist),
        ("Emoji config", test_emoji_config_valid),
        ("Bot build", test_bot_build),
    ]

    passed = 0
    failed = 0

    for test_name, test_func in tests:
        try:
            print(f"Testing: {test_name}...")
            test_func()
            print(f"✅ {test_name} passed\n")
            passed += 1
        except Exception as e:
            print(f"❌ {test_name} failed: {e}\n")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Test Results: {passed} passed, {failed} failed")
    print(f"{'='*50}")

    if failed > 0:
        sys.exit(1)
