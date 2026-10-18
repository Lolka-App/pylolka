from __future__ import annotations

import datetime

import pytest

import lolka
from lolka import utils
from lolka.asset import LOLKA_CDN, Asset
from lolka.http import LOLKA_API_BASE, Route
from lolka.object import Object

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
