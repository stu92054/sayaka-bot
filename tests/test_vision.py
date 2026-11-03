"""看圖：篩選與下載圖片、只附在觸發的那則訊息、退回純文字、/deep 附圖。"""
import base64
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import openai
import pytest

from bot.llm import LLMClient
from bot.main import PersonaBot
from bot.persona import Persona
from bot.vision import attach_images, image_mime, is_image, load_images

from .conftest import BOT_ID, TAIPEI, make_config, make_message

CH = 500
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class FakeAttachment:
    def __init__(self, filename="cat.png", data=PNG, content_type="image/png", size=None, error=None):
        self.filename = filename
        self.content_type = content_type
        self.size = len(data) if size is None else size
        self._data = data
        self._error = error
        self.reads = 0

    async def read(self):
        self.reads += 1
        if self._error is not None:
            raise self._error
        return self._data


def message_with(attachments, content="你看", **kw):
    msg = make_message(content, **kw)
    msg.attachments = attachments
    return msg


def last_user_content(call):
    return call["messages"][-1]["content"]


# --- 篩選與格式 ---

@pytest.mark.parametrize("data,mime", [
    (PNG, "image/png"), (JPEG, "image/jpeg"), (b"GIF89a" + b"\x00" * 8, "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"), (b"%PDF-1.7", None),
])
def test_image_mime_from_content(data, mime):
    assert image_mime(data) == mime


def test_is_image_by_content_type_or_extension():
    assert is_image(SimpleNamespace(filename="x.bin", content_type="image/jpeg"))
    assert is_image(SimpleNamespace(filename="Photo.JPG", content_type=None))
    assert not is_image(SimpleNamespace(filename="notes.pdf", content_type="application/pdf"))


def test_attach_images_only_touches_last_user_message():
    prompt = [{"role": "system", "content": "P"}, {"role": "user", "content": "KO: 你看"}]
    out = attach_images(prompt, [PNG_URL], "low")
    assert out[0] == prompt[0] and prompt[1]["content"] == "KO: 你看"  # 不改到原本的 prompt
    assert out[1]["content"] == [{"type": "text", "text": "KO: 你看"},
                                 {"type": "image_url", "image_url": {"url": PNG_URL, "detail": "low"}}]
    assert attach_images(prompt, [], "low") is prompt


async def test_load_images_skips_bad_ones():
    cfg = make_config(vision_max_bytes=100)
    atts = [FakeAttachment("ok.png"), FakeAttachment("big.png", size=1000), FakeAttachment("x.png", data=b"%PDF"),
            FakeAttachment("broken.png", error=RuntimeError("404")), FakeAttachment("doc.pdf", content_type="application/pdf")]
    assert await load_images(atts, cfg) == [PNG_URL]
    assert atts[1].reads == 0  # 太大的不下載
    assert atts[4].reads == 0  # 不是圖片的不下載


async def test_load_images_max_count():
    atts = [FakeAttachment(f"{i}.png") for i in range(6)]
    assert len(await load_images(atts, make_config(vision_max_images=4))) == 4


# --- 一般聊天 ---

async def test_mention_with_image_sends_image(make_service, fake_openai, storage):
    svc = make_service()
    att = FakeAttachment("bento.png")
    reply = await svc.handle_message(message_with([att], mentions=[BOT_ID]), BOT_ID)
    assert reply.recorded
    content = last_user_content(fake_openai.completions.calls[-1])
    assert content[0]["type"] == "text" and content[0]["text"].endswith("KO: 你看 [圖片:bento.png]")
    assert content[1] == {"type": "image_url", "image_url": {"url": PNG_URL, "detail": "low"}}
    # 歷史紀錄只留文字
    assert storage.recent_messages(CH, 2)[0].content == "你看 [圖片:bento.png]"


async def test_detail_configurable(make_service, fake_openai):
    svc = make_service(vision_detail="high")
    await svc.handle_message(message_with([FakeAttachment()], mentions=[BOT_ID]), BOT_ID)
    assert last_user_content(fake_openai.completions.calls[-1])[1]["image_url"]["detail"] == "high"


async def test_untriggered_image_not_downloaded(make_service, fake_openai, storage):
    svc = make_service()
    att = FakeAttachment()
    assert await svc.handle_message(message_with([att]), BOT_ID) is None
    assert att.reads == 0 and fake_openai.completions.calls == []
    assert storage.recent_messages(CH, 1)[0].content == "你看 [圖片:cat.png]"


async def test_earlier_images_are_text_only(make_service, fake_openai):
    svc = make_service()
    await svc.handle_message(message_with([FakeAttachment("old.png")]), BOT_ID)
    await svc.handle_message(make_message("剛剛那張怎麼樣？", mentions=[BOT_ID]), BOT_ID)
    messages = fake_openai.completions.calls[-1]["messages"]
    assert all(isinstance(m["content"], str) for m in messages)
    assert "[圖片:old.png]" in messages[-1]["content"]


@pytest.mark.parametrize("overrides", [dict(vision_enabled=False), dict(model_default="deepseek-v4-pro")])
async def test_no_images_when_disabled_or_model_cannot_see(make_service, fake_openai, overrides):
    svc = make_service(**overrides)
    att = FakeAttachment()
    await svc.handle_message(message_with([att], mentions=[BOT_ID]), BOT_ID)
    assert att.reads == 0
    assert isinstance(last_user_content(fake_openai.completions.calls[-1]), str)


async def test_download_failure_still_replies(make_service, fake_openai):
    svc = make_service()
    reply = await svc.handle_message(message_with([FakeAttachment(error=RuntimeError("x"))], mentions=[BOT_ID]),
                                     BOT_ID)
    assert reply.recorded
    assert isinstance(last_user_content(fake_openai.completions.calls[-1]), str)


async def test_interject_judge_is_text_only(make_service, fake_openai):
    svc = make_service(random_interject_rate=1.0)
    fake_openai.completions.replies = [{"top": [("是", 0.9), ("否", 0.1)], "finish": "length"}, "好可愛的便當！"]
    reply = await svc.handle_message(message_with([FakeAttachment()], content="今天的便當"), BOT_ID)
    assert reply is not None and reply.chunks == ["好可愛的便當！"]
    judge, answer = fake_openai.completions.calls
    assert isinstance(last_user_content(judge), str)
    assert isinstance(last_user_content(answer), list)


# --- 圖片被拒時退回純文字 ---

REQ = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")


def status_error(cls, code):
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


async def test_bad_request_with_image_falls_back_to_text(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.replies = [status_error(openai.BadRequestError, 400), "看不到圖，不過聽起來不錯"]
    reply = await svc.handle_message(message_with([FakeAttachment()], mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == ["看不到圖，不過聽起來不錯"]
    first, second = fake_openai.completions.calls
    assert isinstance(last_user_content(first), list) and isinstance(last_user_content(second), str)


async def test_other_errors_do_not_trigger_text_fallback(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.default = status_error(openai.AuthenticationError, 401)
    reply = await svc.handle_message(message_with([FakeAttachment()], mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == [svc.replies["error"]]
    assert len(fake_openai.completions.calls) == 1


async def test_bad_request_without_image_is_not_retried(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.default = status_error(openai.BadRequestError, 400)
    reply = await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == [svc.replies["error"]]
    assert len(fake_openai.completions.calls) == 1


# --- /deep ---

async def test_deep_with_image(make_service, fake_openai, storage):
    svc = make_service()
    await svc.deep(CH, "KO", "這是什麼料理？", [FakeAttachment("dish.jpg", data=JPEG, content_type="image/jpeg")])
    call = fake_openai.completions.calls[-1]
    assert call["extra_body"] == {"thinking": {"type": "enabled"}}
    content = last_user_content(call)
    assert content[0]["text"].endswith("KO: 這是什麼料理？ [圖片:dish.jpg]")
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert storage.recent_messages(CH, 2)[0].content == "這是什麼料理？ [圖片:dish.jpg]"


async def test_deep_with_pro_model_does_not_send_image(make_service, fake_openai):
    svc = make_service(model_deep="deepseek-v4-pro")
    att = FakeAttachment()
    await svc.deep(CH, "KO", "這是什麼？", [att])
    assert att.reads == 0
    assert isinstance(last_user_content(fake_openai.completions.calls[-1]), str)


async def test_deep_command_passes_image(storage, fake_openai, persona_file):
    config = make_config()
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI))
    bot = PersonaBot(config, storage, llm, Persona(persona_file))
    def interaction(user_id):  # 不同使用者，避開每人冷卻
        return SimpleNamespace(
            guild=SimpleNamespace(id=111),
            user=SimpleNamespace(id=user_id, display_name="KO", name="KO"),
            channel_id=CH,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
    try:
        await bot.tree.get_command("deep").callback(interaction(1), question="這是什麼？", image=FakeAttachment())
        assert isinstance(last_user_content(fake_openai.completions.calls[-1]), list)
        await bot.tree.get_command("deep").callback(interaction(2), question="沒有圖片的問題")
        assert len(fake_openai.completions.calls) == 2
        assert isinstance(last_user_content(fake_openai.completions.calls[-1]), str)
    finally:
        await bot.close()


# --- 與查資料工具一起使用 ---

async def test_image_with_web_tools(make_service, fake_openai):
    from bot.tools import build_web_tools
    from bot.web import DeepSeekWeb

    from .test_tools import KEY, SEARCH_OK, FakeWebAPI

    svc = make_service(web_tools_enabled=True)
    svc.web_tools = build_web_tools(DeepSeekWeb(KEY, post=FakeWebAPI(search=[(200, SEARCH_OK)]),
                                                on_billable=svc.llm.record_web_request))
    fake_openai.completions.replies = [{"tool_calls": [("web_search", {"query": "這張海報的活動"})]}, "查到了"]
    msg = message_with([FakeAttachment("poster.png")], content="這張海報的活動是什麼時候？ https://example.com/live6",
                       mentions=[BOT_ID])
    reply = await svc.handle_message(msg, BOT_ID)
    assert reply.chunks == ["查到了"]
    first, second = fake_openai.completions.calls
    assert "tools" in first
    # 兩輪都帶著圖片（在 user 訊息裡），工具結果接在後面
    for call in (first, second):
        user_msgs = [m for m in call["messages"] if m["role"] == "user"]
        assert isinstance(user_msgs[-1]["content"], list)
    assert [m["role"] for m in second["messages"][-2:]] == ["assistant", "tool"]
