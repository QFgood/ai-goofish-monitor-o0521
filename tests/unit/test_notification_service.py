import asyncio

import pytest

from src.infrastructure.external.notification_clients.base import NotificationClient
from src.infrastructure.external.notification_clients.pushplus_client import PushPlusClient
from src.infrastructure.external.notification_clients.webhook_client import WebhookClient
from src.services.notification_service import NotificationService


class _OkClient(NotificationClient):
    channel_key = "ok"
    display_name = "OK"

    async def send(self, product_data, reason):
        return None


class _FailClient(NotificationClient):
    channel_key = "fail"
    display_name = "FAIL"

    async def send(self, product_data, reason):
        raise RuntimeError("boom")


def test_notification_service_collects_success_and_failure_results():
    service = NotificationService([_OkClient(enabled=True), _FailClient(enabled=True)])

    results = asyncio.run(
        service.send_notification({"商品标题": "Sony A7M4"}, "价格合适")
    )

    assert results["ok"]["success"] is True
    assert results["ok"]["message"] == "发送成功"
    assert results["fail"]["success"] is False
    assert results["fail"]["message"] == "boom"


def test_webhook_client_renders_json_templates(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    def _fake_post(url, headers=None, json=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["data"] = data
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)

    client = WebhookClient(
        webhook_url="https://hooks.example.com/notify",
        webhook_method="POST",
        webhook_headers='{"Authorization":"Bearer token"}',
        webhook_content_type="JSON",
        webhook_query_parameters='{"task":"{{title}}"}',
        webhook_body='{"message":"{{content}}","link":"{{desktop_link}}"}',
        pcurl_to_mobile=False,
    )

    asyncio.run(
        client.send(
            {
                "商品标题": "Sony A7M4",
                "当前售价": "9999",
                "商品链接": "https://www.goofish.com/item/123",
            },
            "价格合适",
        )
    )

    assert "task=%F0%9F%9A%A8+%E6%96%B0%E6%8E%A8%E8%8D%90%21+Sony+A7M4" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["message"].startswith("价格: 9999")
    assert captured["json"]["link"] == "https://www.goofish.com/item/123"
    assert captured["data"] is None


def _product():
    return {
        "商品标题": "Sony A7M4",
        "当前售价": "9999",
        "商品链接": "https://www.goofish.com/item/123",
    }


def test_pushplus_client_sends_text_template_and_requires_business_code(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功"}

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("requests.post", _fake_post)
    client = PushPlusClient("test-token", "group-code", pcurl_to_mobile=False)

    asyncio.run(client.send(_product(), "价格合适"))

    assert captured["url"] == "https://www.pushplus.plus/send"
    assert captured["json"]["token"] == "test-token"
    assert captured["json"]["topic"] == "group-code"
    assert captured["json"]["template"] == "txt"
    assert captured["json"]["content"].startswith("价格: 9999\n原因: 价格合适")


def test_pushplus_client_rejects_business_error_with_http_200(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 903, "msg": "无效的用户token"}

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: _FakeResponse(),
    )
    client = PushPlusClient("bad-token", pcurl_to_mobile=False)

    with pytest.raises(RuntimeError, match="无效的用户token"):
        asyncio.run(client.send(_product(), "价格合适"))


@pytest.mark.parametrize('channel',['wecom','telegram','bark','ntfy','gotify','pushplus','webhook'])
def test_price_summary_channels_have_four_metrics_without_recommendations_or_links(monkeypatch,channel):
    from src.infrastructure.external.notification_clients.wecom_bot_client import WeComBotClient
    from src.infrastructure.external.notification_clients.telegram_client import TelegramClient
    from src.infrastructure.external.notification_clients.bark_client import BarkClient
    from src.infrastructure.external.notification_clients.ntfy_client import NtfyClient
    from src.infrastructure.external.notification_clients.gotify_client import GotifyClient
    captured={}
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'ok':True,'errcode':0,'code':200}
    def post(*args,**kwargs):
        captured.update(kwargs);return Response()
    monkeypatch.setattr('requests.post',post)
    clients={
        'wecom':lambda:WeComBotClient('https://example.com/fixture'),
        'telegram':lambda:TelegramClient('fixture-token','fixture-chat'),
        'bark':lambda:BarkClient('https://example.com/fixture'),
        'ntfy':lambda:NtfyClient('https://example.com/fixture'),
        'gotify':lambda:GotifyClient('https://example.com','fixture-token'),
        'pushplus':lambda:PushPlusClient('fixture-token'),
        'webhook':lambda:WebhookClient(webhook_url='https://example.com/fixture',
            webhook_body='{"content":"{{content}}","link":"{{desktop_link}}"}'),
    }
    metrics='当前均价：¥5200.00\n历史均价：¥5300.00\n已售出均价：¥4800.00\n售出最低价：¥4500.00'
    asyncio.run(clients[channel]().send({'notification_type':'price_summary','商品标题':'价格摘要 · Fixture'},metrics))
    import json
    text=json.dumps(captured,ensure_ascii=False,default=lambda value:value.decode() if isinstance(value,bytes) else str(value))
    for label in ['当前均价','历史均价','已售出均价','售出最低价']:assert label in text
    for forbidden in ['新推荐','N/A','手机端链接','电脑端链接','goofish.com','href=','原因:']:assert forbidden not in text
    if channel=='bark':assert 'url' not in captured['json']
    if channel=='telegram':assert captured['json']['disable_web_page_preview'] is True
