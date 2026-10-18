from __future__ import annotations

from types import SimpleNamespace

import pytest
from yarl import URL

from lolka.errors import InvalidArgument
from lolka.http import LOLKA_API_BASE
from lolka.webhook import SyncWebhook, Webhook

WEBHOOK_ID = "123456789012345"
TOKEN = "aB3-_." * 10 + "abcd"  # 64 characters

URLS = [
    f"https://lolka.app/api/webhooks/{WEBHOOK_ID}/{TOKEN}",
    f"http://localhost/api/webhooks/{WEBHOOK_ID}/{TOKEN}",
    f"http://localhost:8080/api/webhooks/{WEBHOOK_ID}/{TOKEN}",
    f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{TOKEN}",
    f"https://discordapp.com/api/webhooks/{WEBHOOK_ID}/{TOKEN}",
]


def _make(kind, url):
    if kind is Webhook:
        return Webhook.from_url(url, session=SimpleNamespace())
    return SyncWebhook.from_url(url)


@pytest.mark.parametrize("kind", [Webhook, SyncWebhook])
@pytest.mark.parametrize("url", URLS)
def test_from_url_accepts_any_host(kind, url):
    if kind is SyncWebhook:
        pytest.importorskip("requests")
    webhook = _make(kind, url)
    assert webhook.id == int(WEBHOOK_ID)
    assert webhook.token == TOKEN


@pytest.mark.parametrize("kind", [Webhook, SyncWebhook])
def test_from_url_rejects_garbage(kind):
    with pytest.raises(InvalidArgument):
        _make(kind, "https://lolka.app/api/bot/webhooks/1/2")


@pytest.mark.parametrize("kind", [Webhook, SyncWebhook])
def test_url_is_built_from_the_api_origin(kind):
    if kind is SyncWebhook:
        pytest.importorskip("requests")
    webhook = _make(kind, URLS[0])
    assert webhook.url == f"{URL(LOLKA_API_BASE).origin()}/api/webhooks/{WEBHOOK_ID}/{TOKEN}"
    assert webhook.url.startswith(str(URL(LOLKA_API_BASE).origin()))
