from __future__ import annotations

import ast
import datetime
import pathlib
import re
from types import SimpleNamespace

import pytest

import lolka
from lolka import utils
from lolka.asset import LOLKA_CDN, Asset
from lolka.emoji import BaseEmoji
from lolka.http import LOLKA_API_BASE, Route
from lolka.object import Object
from lolka.partial_emoji import PartialEmoji
from lolka.user import BaseUser
from lolka.utils import LOLKA_INVITE, LOLKA_WEB

UTC = datetime.timezone.utc


def _dt() -> datetime.datetime:
    return datetime.datetime(2026, 3, 15, 12, 30, 45, 123000, tzinfo=UTC)


def test_snowflake_roundtrip():
    dt = _dt()
    assert utils.snowflake_time(utils.time_snowflake(dt)) == dt


def test_time_snowflake_high_sets_low_bits():
    dt = _dt()
    low = utils.time_snowflake(dt)
    high = utils.time_snowflake(dt, high=True)
    assert low & ((1 << utils.LOLKA_SNOWFLAKE_SHIFT) - 1) == 0
    assert high - low == (1 << utils.LOLKA_SNOWFLAKE_SHIFT) - 1


def test_generate_snowflake_roundtrip():
    dt = _dt()
    decoded = utils.snowflake_time(utils.generate_snowflake(dt))
    assert abs((decoded - dt).total_seconds()) < 0.001


def test_current_snowflake_fits_js_safe_integer():
    assert utils.generate_snowflake() < 2**53


def test_epoch_and_shift():
    assert utils.DISCORD_EPOCH == 1735689600000
    assert utils.LOLKA_SNOWFLAKE_SHIFT == 14


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [
        (1, 4),
        (123456789012345, 3),
        (8053231871942656, 5),
    ],
)
def test_lolka_default_avatar_index(user_id, expected):
    # Ожидаемые значения посчитаны формулой из client/common/ui/Avatar/utils/color.ts
    assert utils._lolka_default_avatar_index(user_id) == expected


def test_discord_snowflake_time():
    assert utils._discord_snowflake_time(175928847299117063) == datetime.datetime(
        2016, 4, 30, 11, 18, 25, 796000, tzinfo=UTC
    )


def test_object_snowflake_parts():
    node, step = 0b1010, 0b1100110011
    obj = Object((((123456 << 4) | node) << 10) | step)
    assert obj.worker_id == node
    assert obj.process_id == 0
    assert obj.increment_id == step


def test_asset_full_url_is_used_as_is():
    url = "https://cdn.lolka.app/x/y.gif"
    asset = Asset._from_avatar(None, 1, url)
    assert asset.url == url
    assert asset.is_animated()


def test_asset_full_url_static():
    url = "https://cdn.lolka.app/x/y.png?v=1"
    asset = Asset._from_guild_icon(None, 1, url)
    assert asset.url == url
    assert not asset.is_animated()


def test_asset_hash_builds_regular_url():
    asset = Asset._from_avatar(None, 42, "abcdef")
    assert asset.url == f"{Asset.BASE}/avatars/42/abcdef.png?size=1024"
    assert not asset.is_animated()


def test_default_avatar_url():
    assert (
        Asset._from_default_avatar(None, 3).url == f"{LOLKA_CDN}/system/avatars/3.jpg"
    )


def test_route_uses_lolka_api_base():
    route = Route("GET", "/users/@me")
    assert route.url.startswith(LOLKA_API_BASE + "/v10")
    assert route.url.endswith("/users/@me")


def test_escape_mentions_short_ids():
    assert utils.escape_mentions("@123456789012345") == "@​123456789012345"
    assert utils.escape_mentions("<@123456789012345>") == "<@​123456789012345>"


def test_package_identity():
    assert lolka.__title__ == "lolka"
    assert lolka.version_info[:3] == (2, 8, 1)


def test_interaction_created_at_uses_discord_layout():
    class FakeInteraction(lolka.Interaction):
        def __init__(self):
            self.id = 175928847299117063

    assert lolka.Interaction.created_at.fget(FakeInteraction()) == datetime.datetime(
        2016, 4, 30, 11, 18, 25, 796000, tzinfo=UTC
    )


def test_member_converter_is_registered_for_lolka_member():
    from lolka.ext.commands import converter

    assert converter.CONVERTER_MAPPING[lolka.Member] is converter.MemberConverter


_UID = 663859382844416
_UUID = "8a355639-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def test_asset_base_is_lolka_cdn():
    assert Asset.BASE == LOLKA_CDN


def test_avatar_relative_key_goes_to_cdn():
    # Бек может прислать относительный ключ вместо полного URL
    key = f"avatars/{_UID}/{_UUID}.webp"
    asset = Asset._from_avatar(None, _UID, key)
    assert asset.url == f"{LOLKA_CDN}/{key}"
    assert asset.key == key
    assert not asset.is_animated()


def test_avatar_leading_slash_is_stripped():
    key = f"/avatars/{_UID}/{_UUID}.webp"
    assert Asset._from_avatar(None, _UID, key).url == f"{LOLKA_CDN}/{key[1:]}"


def test_avatar_full_url_is_kept():
    url = f"https://cdn.lolka.app/avatars/{_UID}/{_UUID}.webp"
    assert Asset._from_avatar(None, _UID, url).url == url


@pytest.mark.parametrize(
    "key",
    [
        f"avatars/{_UID}/{_UUID}_animated.webp",
        f"https://cdn.lolka.app/avatars/{_UID}/{_UUID}_animated.webp?v=2",
        f"avatars/{_UID}/{_UUID}.gif",
        f"avatars/{_UID}/{_UUID}.apng",
    ],
)
def test_animated_avatar_detection(key):
    assert Asset._from_avatar(None, _UID, key).is_animated()


def test_avatar_decoration_full_url():
    url = f"https://cdn.lolka.app/decorations/{_UUID}.webp"
    asset = Asset._from_avatar_decoration(None, _UID, url)
    assert asset.url == url
    assert not asset.is_animated()


def test_avatar_decoration_relative_key():
    asset = Asset._from_avatar_decoration(
        None, _UID, f"decorations/{_UUID}_animated.webp"
    )
    assert asset.url == f"{LOLKA_CDN}/decorations/{_UUID}_animated.webp"
    assert asset.is_animated()


@pytest.mark.parametrize(
    "build",
    [
        lambda v: Asset._from_guild_avatar(None, 1, 2, v),
        lambda v: Asset._from_guild_banner(None, 1, 2, v),
        lambda v: Asset._from_user_banner(None, 2, v),
        lambda v: Asset._from_icon(None, 1, v, "role"),
        lambda v: Asset._from_guild_icon(None, 1, v),
        lambda v: Asset._from_guild_image(None, 1, v, "banners"),
        lambda v: Asset._from_user_primary_guild_tag(None, 1, v),
        lambda v: Asset._from_scheduled_event_image(None, 1, v),
        lambda v: Asset._from_cover_image(None, 1, v),
        lambda v: Asset._from_sticker_banner(None, v),
    ],
)
def test_all_factories_resolve_lolka_keys(build):
    assert build("images/1/x.webp").url == f"{LOLKA_CDN}/images/1/x.webp"
    full = "https://cdn.lolka.app/images/1/x.webp"
    assert build(full).url == full


def test_soundboard_url():
    assert (
        Asset._from_soundboard_sound(None, 77).url == f"{LOLKA_CDN}/soundboard/77.ogg"
    )


def test_legacy_hash_still_builds_path_on_cdn():
    asset = Asset._from_guild_icon(None, 5, "a_abc")
    assert asset.url == f"{LOLKA_CDN}/icons/5/a_abc.gif?size=1024"
    assert asset.is_animated()


def test_asset_resizing_does_not_change_url():
    asset = Asset._from_avatar(None, _UID, f"avatars/{_UID}/{_UUID}.webp")
    assert asset.with_size(256).url == asset.url
    assert asset.replace(format="png").url == asset.url
    assert asset.replace(size=128, static_format="jpg").url == asset.url
    assert asset.with_static_format("png").url == asset.url
    assert asset.with_format("png").url == asset.url


def test_asset_resizing_still_validates_arguments():
    asset = Asset._from_avatar(None, _UID, f"avatars/{_UID}/{_UUID}.webp")
    with pytest.raises(lolka.InvalidArgument):
        asset.with_size(100)
    with pytest.raises(lolka.InvalidArgument):
        asset.replace(size=3)
    with pytest.raises(lolka.InvalidArgument):
        asset.with_format("gif")
    with pytest.raises(lolka.InvalidArgument):
        asset.replace(format="bmp")
    animated = Asset._from_avatar(None, _UID, f"avatars/{_UID}/{_UUID}_animated.webp")
    assert animated.with_format("gif").url == animated.url


def test_partial_emoji_url():
    static = PartialEmoji(name="x", id=123)
    animated = PartialEmoji(name="x", id=123, animated=True)
    assert static.url == f"{LOLKA_CDN}/emojis/123.webp"
    assert animated.url == f"{LOLKA_CDN}/emojis/123.webp"


def _role_payload(**extra):
    return {"id": "5", "name": "r", **extra}


@pytest.mark.parametrize("extra", [{}, {"colors": None}])
def test_role_without_colors_uses_legacy_color(extra):
    role = lolka.Role(
        guild=None, state=None, data=_role_payload(color=0xFF8800, **extra)
    )
    assert role.colours.primary.value == 0xFF8800
    assert role.colours.secondary is None


def test_role_with_colors_payload():
    role = lolka.Role(
        guild=None,
        state=None,
        data=_role_payload(color=1, colors={"primary_color": 0xABCDEF}),
    )
    assert role.colours.primary.value == 0xABCDEF


def test_role_without_any_color():
    assert (
        lolka.Role(guild=None, state=None, data=_role_payload()).colours.primary.value
        == 0
    )


def test_guild_jump_url():
    assert lolka.Guild.jump_url.fget(SimpleNamespace(id=7)) == f"{LOLKA_WEB}/servers/7"


def test_guild_channel_jump_url():
    channel = SimpleNamespace(id=9, guild=SimpleNamespace(id=7))
    assert (
        lolka.abc.GuildChannel.jump_url.fget(channel)
        == f"{LOLKA_WEB}/servers/7/channels/9"
    )


def test_thread_jump_url():
    thread = SimpleNamespace(id=11, guild=SimpleNamespace(id=7))
    assert lolka.Thread.jump_url.fget(thread) == f"{LOLKA_WEB}/servers/7/channels/11"


def test_dm_and_group_jump_url():
    dm = SimpleNamespace(id=3)
    assert lolka.DMChannel.jump_url.fget(dm) == f"{LOLKA_WEB}/me/channels/3"
    assert lolka.GroupChannel.jump_url.fget(dm) == f"{LOLKA_WEB}/me/channels/3"


def test_message_jump_url():
    guild_msg = SimpleNamespace(
        id=4, channel=SimpleNamespace(id=3), guild=SimpleNamespace(id=7)
    )
    assert (
        lolka.Message.jump_url.fget(guild_msg) == f"{LOLKA_WEB}/servers/7/channels/3/4"
    )
    dm_msg = SimpleNamespace(id=4, channel=SimpleNamespace(id=3), guild=None)
    assert lolka.Message.jump_url.fget(dm_msg) == f"{LOLKA_WEB}/me/channels/3/4"


def test_message_reference_jump_url():
    ref = SimpleNamespace(guild_id=7, channel_id=3, message_id=4)
    assert (
        lolka.MessageReference.jump_url.fget(ref)
        == f"{LOLKA_WEB}/servers/7/channels/3/4"
    )
    ref = SimpleNamespace(guild_id=None, channel_id=3, message_id=4)
    assert lolka.MessageReference.jump_url.fget(ref) == f"{LOLKA_WEB}/me/channels/3/4"


def test_user_jump_url():
    assert (
        lolka.User.jump_url.fget(SimpleNamespace(name="vasya")) == f"{LOLKA_WEB}/@vasya"
    )


def test_scheduled_event_url():
    event = SimpleNamespace(id=2, guild=SimpleNamespace(id=7))
    assert lolka.ScheduledEvent.url.fget(event) == f"{LOLKA_WEB}/servers/7"


def test_invite_url():
    assert lolka.Invite.BASE == LOLKA_INVITE
    invite = SimpleNamespace(BASE=lolka.Invite.BASE, code="abc", scheduled_event=None)
    assert lolka.Invite.url.fget(invite) == f"{LOLKA_INVITE}/abc"


@pytest.mark.parametrize(
    "value",
    [
        "https://lolka.gg/abc",
        "http://lolka.gg/abc",
        "lolka.gg/abc",
        "https://lolka.app/i/abc",
        "lolka.app/i/abc",
        "abc",
    ],
)
def test_resolve_invite(value):
    assert utils.resolve_invite(value) == "abc"


def test_oauth_url():
    url = utils.oauth_url(123)
    assert url.startswith(f"{LOLKA_WEB}/bot-authorize?client_id=123")
    assert "scope=bot" in url


def test_hash_uses_lolka_shift():
    uid = (5 << utils.LOLKA_SNOWFLAKE_SHIFT) | 3
    assert hash(Object(uid)) == 5
    assert BaseUser.__hash__(SimpleNamespace(id=uid)) == 5
    assert BaseEmoji.__hash__(SimpleNamespace(id=uid)) == 5


def test_member_update_from_message_keeps_missing_profile_fields():
    member = SimpleNamespace(
        _avatar="old_a", _banner="old_b", _avatar_decoration={"asset": "old_d"}
    )
    base = {"roles": []}
    lolka.Member._update_from_message(member, dict(base))
    assert (member._avatar, member._banner) == ("old_a", "old_b")
    assert member._avatar_decoration == {"asset": "old_d"}

    lolka.Member._update_from_message(
        member,
        {
            **base,
            "avatar": "avatars/1/new.webp",
            "banner": None,
            "avatar_decoration_data": {"asset": "x/y.webp"},
        },
    )
    assert member._avatar == "avatars/1/new.webp"
    assert member._banner is None
    assert member._avatar_decoration == {"asset": "x/y.webp"}


# Любое упоминание Discord-хостов (в том числе внутри регэкспов: discord(?:app)?\\.com)
_DISCORD_HOST_RX = re.compile(
    r"discord(?:\(\?:app\)\?|app)?\\?\.(?:com|gg|net|new|media)|discordapp|discord\(\?:",
    re.IGNORECASE,
)
# (файл, подстрока) осознанно оставленных Discord-упоминаний вне docstring
_ALLOWED = (
    ("client.py", "discord.com/application-directory"),
    # templates, widget и monetization в Lolka отсутствуют и остались как в upstream
    ("template.py", "discord.new"),
    ("widget.py", "discord.com/api/guilds"),
    ("monetization.py", "application-directory"),
    ("ext/commands/converter.py", "discord(?:app)?"),
    ("utils.py", "discord(?:\\.new|(?:app)?\\.com\\/template)"),
    ("", "docs.discord.com"),
    ("", "support-dev.discord.com"),
)


def _non_docstring_strings(tree: ast.AST):
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def test_no_discord_hosts_in_sources():
    root = pathlib.Path(lolka.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for value in _non_docstring_strings(tree):
            if not _DISCORD_HOST_RX.search(value):
                continue
            if any(rel.endswith(f) and sub in value for f, sub in _ALLOWED):
                continue
            offenders.append(f"{rel}: {value[:80]!r}")
    assert not offenders, offenders


def test_legacy_collectible_keeps_prefix_contract():
    static = Asset._from_collectible(None, "nameplates/foo/")
    assert static.url == f"{Asset.BASE}/assets/collectibles/nameplates/foo/static.png"
    moving = Asset._from_collectible(None, "nameplates/foo/", animated=True)
    assert moving.url.endswith("nameplates/foo/asset.webm")
    assert moving.is_animated()


def test_protocol_relative_asset_url():
    assert (
        Asset._from_avatar(None, 1, "//cdn.example/avatars/1/x.webp").url
        == "https://cdn.example/avatars/1/x.webp"
    )


def test_asset_base_override_applies_everywhere(monkeypatch):
    monkeypatch.setattr(Asset, "BASE", "http://cdn.test")
    assert Asset._from_avatar(None, 1, "avatars/1/x.webp").url == (
        "http://cdn.test/avatars/1/x.webp"
    )
    assert (
        Asset._from_soundboard_sound(None, 5).url == "http://cdn.test/soundboard/5.ogg"
    )
    assert PartialEmoji(name="x", id=9).url == "http://cdn.test/emojis/9.webp"


def test_user_jump_url_quotes_name():
    assert (
        lolka.User.jump_url.fget(SimpleNamespace(name="a b/c"))
        == f"{LOLKA_WEB}/@a%20b%2Fc"
    )


def test_resolve_invite_round_trip_and_tails():
    invite = SimpleNamespace(BASE=lolka.Invite.BASE, code="abc", scheduled_event=None)
    assert utils.resolve_invite(lolka.Invite.url.fget(invite)) == "abc"
    assert utils.resolve_invite("https://lolka.gg/abc/") == "abc"
    assert utils.resolve_invite("https://lolka.gg/abc?event=5") == "abc"
    assert utils.resolve_invite("https://www.lolka.gg/abc") == "abc"
    assert utils.resolve_invite("HTTPS://LOLKA.GG/abc") == "abc"


def test_resolve_invite_custom_hosts(monkeypatch):
    monkeypatch.setattr(utils, "LOLKA_INVITE", "http://localhost:5173/i")
    monkeypatch.setattr(utils, "LOLKA_WEB", "http://localhost:5173")
    assert utils.resolve_invite("http://localhost:5173/i/xyz") == "xyz"
    assert utils.resolve_invite("localhost:5173/i/xyz?event=1") == "xyz"
    assert utils.resolve_invite("https://lolka.gg/xyz") == "xyz"


def _ctx(guild_id=None, channel_id=500000000000000):
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
        channel=SimpleNamespace(id=channel_id),
    )


def test_message_converter_guild_link_round_trip():
    from lolka.ext.commands.converter import PartialMessageConverter as C

    msg = SimpleNamespace(
        id=300000000000003,
        channel=SimpleNamespace(id=200000000000002),
        guild=SimpleNamespace(id=100000000000001),
    )
    url = lolka.Message.jump_url.fget(msg)
    assert C._get_id_matches(_ctx(999999999999999), url) == (
        100000000000001,
        300000000000003,
        200000000000002,
    )


def test_message_converter_dm_link_does_not_use_ctx_guild():
    from lolka.ext.commands.converter import PartialMessageConverter as C

    msg = SimpleNamespace(
        id=300000000000003, channel=SimpleNamespace(id=200000000000002), guild=None
    )
    url = lolka.Message.jump_url.fget(msg)
    assert C._get_id_matches(_ctx(999999999999999), url) == (
        None,
        300000000000003,
        200000000000002,
    )


def test_message_converter_discord_link_still_works():
    from lolka.ext.commands.converter import PartialMessageConverter as C

    url = "https://discord.com/channels/100000000000001/200000000000002/300000000000003"
    assert C._get_id_matches(_ctx(), url) == (
        100000000000001,
        300000000000003,
        200000000000002,
    )


def test_message_converter_custom_web(monkeypatch):
    from lolka.ext.commands.converter import PartialMessageConverter as C

    monkeypatch.setattr(utils, "LOLKA_WEB", "http://localhost:5173")
    url = "http://localhost:5173/servers/100000000000001/channels/200000000000002/300000000000003"
    assert C._get_id_matches(_ctx(), url)[0] == 100000000000001


def _make_message(ref, guild_id="100000000000001", chan=200000000000002, cached=True):
    from unittest.mock import MagicMock

    from lolka.channel import PartialMessageable

    state = MagicMock()
    state._get_guild.return_value = (
        Object(int(guild_id)) if (guild_id and cached) else None
    )
    state.store_user.side_effect = lambda d: lolka.User(state=state, data=d)
    channel = PartialMessageable(state=state, id=chan)
    data = {
        "id": "300000000000003",
        "channel_id": str(chan),
        "type": 19,
        "content": "x",
        "author": {"id": "1", "username": "u", "discriminator": "0", "avatar": None},
        "attachments": [],
        "embeds": [],
        "mentions": [],
        "mention_roles": [],
        "pinned": False,
        "mention_everyone": False,
        "tts": False,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "edited_timestamp": None,
        "message_reference": ref,
    }
    if guild_id:
        data["guild_id"] = guild_id
    return lolka.Message(state=state, channel=channel, data=data)


_SAME = {"message_id": "300000000000001", "channel_id": "200000000000002"}


@pytest.mark.parametrize("cached", [True, False])
def test_reply_reference_gets_guild_id(cached):
    msg = _make_message(dict(_SAME), cached=cached)
    assert msg.reference.guild_id == 100000000000001
    web = f"{LOLKA_WEB}/servers/100000000000001/channels/200000000000002"
    assert msg.reference.jump_url == f"{web}/300000000000001"
    assert msg.jump_url == f"{web}/300000000000003"


@pytest.mark.parametrize("cached", [True, False])
def test_forward_from_other_channel_keeps_no_guild_id(cached):
    ref = {"type": 1, "message_id": "300000000000001", "channel_id": "200000000000009"}
    assert _make_message(ref, cached=cached).reference.guild_id is None


def test_explicit_reference_guild_id_is_kept():
    msg = _make_message({**_SAME, "guild_id": "100000000000077"})
    assert msg.reference.guild_id == 100000000000077


def test_dm_reference_has_no_guild_id():
    msg = _make_message(dict(_SAME), guild_id=None)
    assert msg.reference.guild_id is None
    assert msg.jump_url == f"{LOLKA_WEB}/me/channels/200000000000002/300000000000003"
