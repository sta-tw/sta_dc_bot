from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from utils.exchange_ui import SubmitApplicationView, _add_review_members, _get_support_roles


class FakeMember:
    def __init__(self, member_id):
        self.id = member_id
        self.display_name = "申請人"
        self.mention = f"<@{member_id}>"


class FakeRole:
    def __init__(self, role_id, name, members):
        self.id = role_id
        self.name = name
        self.members = members
        self.mention = f"<@&{role_id}>"


class FakeGuild:
    def __init__(self, roles):
        self.roles = roles
        self._roles = {role.id: role for role in roles}

    def get_role(self, role_id):
        return self._roles.get(role_id)


class FakeThread:
    def __init__(self, name):
        self.name = name
        self.added_members = []
        self.sent = []

    async def add_user(self, member):
        self.added_members.append(member)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeChannel:
    def __init__(self, thread, message):
        self.threads = [thread]
        self._message = message

    def history(self, *, limit):
        async def messages():
            yield self._message

        return messages()


class FakeInteraction:
    def __init__(self, guild, channel, user):
        self.guild = guild
        self.channel = channel
        self.user = user
        self.created_at = datetime.now(timezone.utc)
        self.response = SimpleNamespace(edit_message=self._edit_message)
        self.followup = SimpleNamespace(send=self._followup_send)
        self.edited_view = None
        self.followups = []

    async def _edit_message(self, *, view):
        self.edited_view = view

    async def _followup_send(self, *args, **kwargs):
        self.followups.append((args, kwargs))


@pytest.fixture
def review_bot():
    return SimpleNamespace(
        emoji={},
        settings=SimpleNamespace(support_role_ids=[100]),
    )


def test_get_support_roles_ignores_admin_role(review_bot):
    support_role = FakeRole(100, "審核員", [])
    admin_role = FakeRole(200, "管理員", [])
    guild = FakeGuild([support_role, admin_role])

    assert _get_support_roles(review_bot, guild) == [support_role]


@pytest.mark.asyncio
async def test_add_review_members_uses_only_support_roles(review_bot):
    support_member = FakeMember(1)
    admin_member = FakeMember(2)
    duplicate_member = FakeMember(1)
    support_role = FakeRole(100, "審核員", [support_member])
    admin_role = FakeRole(200, "管理員", [admin_member, duplicate_member])
    guild = FakeGuild([support_role, admin_role])
    thread = FakeThread("審核-thread")

    await _add_review_members(thread, review_bot, guild)

    assert [member.id for member in thread.added_members] == [1]


@pytest.mark.asyncio
async def test_existing_review_thread_adds_missing_support_member(review_bot):
    reviewer = FakeMember(10)
    support_role = FakeRole(100, "審核員", [reviewer])
    guild = FakeGuild([support_role])
    thread = FakeThread("審核-申請人交換備審申請")
    user = FakeMember(42)
    message = SimpleNamespace(author=user, attachments=[object()], content="")
    interaction = FakeInteraction(guild, FakeChannel(thread, message), user)
    view = SubmitApplicationView(user.id, review_bot)

    await view.submit_callback(interaction)

    assert [member.id for member in thread.added_members] == [10]
