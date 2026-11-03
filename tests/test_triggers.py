from bot.triggers import Cooldown, Trigger, detect_trigger

from .conftest import BOT_ID, OTHER_GUILD_ID, FakeClock, make_config, make_message


def detect(msg, **cfg):
    rng = cfg.pop("rng", lambda: 0.99)
    dm_allowed = cfg.pop("dm_allowed", False)
    return detect_trigger(msg, BOT_ID, make_config(**cfg), dm_allowed=dm_allowed, rng=rng)


def test_mention_triggers():
    assert detect(make_message("hi", mentions=[BOT_ID])) is Trigger.MENTION


def test_mention_of_someone_else_does_not_trigger():
    assert detect(make_message("hi", mentions=[123])) is None


def test_reply_to_bot_triggers():
    assert detect(make_message("真的嗎", reply_to_author_id=BOT_ID)) is Trigger.REPLY


def test_reply_to_other_user_does_not_trigger():
    assert detect(make_message("真的嗎", reply_to_author_id=5)) is None


def test_name_triggers_case_insensitive():
    assert detect(make_message("欸大小姐你覺得呢")) is Trigger.NAME
    assert detect(make_message("hey OJOU!")) is Trigger.NAME


def test_plain_message_does_not_trigger():
    assert detect(make_message("今天好累")) is None


def test_ignores_bots():
    assert detect(make_message("大小姐", bot=True, mentions=[BOT_ID])) is None


def test_ignores_unlisted_guild():
    assert detect(make_message("大小姐", guild_id=OTHER_GUILD_ID, mentions=[BOT_ID])) is None


def test_dm_requires_permission():
    assert detect(make_message("hi", guild_id=None)) is None
    assert detect(make_message("hi", guild_id=None), dm_allowed=True) is Trigger.DM


def test_random_interject_disabled_by_default():
    assert detect(make_message("今天好累"), rng=lambda: 0.0) is None


def test_random_interject_uses_rate():
    assert detect(make_message("今天好累"), random_interject_rate=0.1, rng=lambda: 0.05) is Trigger.INTERJECT
    assert detect(make_message("今天好累"), random_interject_rate=0.1, rng=lambda: 0.5) is None


def test_cooldown():
    clock = FakeClock(100.0)
    cd = Cooldown(5, clock=clock)
    assert cd.try_acquire(1)
    assert not cd.try_acquire(1)
    assert cd.try_acquire(2)  # 不同使用者互不影響
    clock.t = 104.9
    assert not cd.try_acquire(1)
    clock.t = 105.0
    assert cd.try_acquire(1)


def test_cooldown_zero_disables():
    cd = Cooldown(0, clock=FakeClock())
    assert cd.try_acquire(1) and cd.try_acquire(1)
