import logging

from bot.config import Config
from bot.main import RedactSecretsFilter


def test_config_from_env_parses_lists_and_comments():
    cfg = Config.from_env({
        "DISCORD_TOKEN": "tok",
        "DEEPSEEK_API_KEY": "key",
        "ALLOWED_GUILD_IDS": "1, 2,3",
        "ADMIN_USER_IDS": "9",
        "BOT_NAMES": "大小姐, Ojou",
        "HISTORY_MAX_MESSAGES": "30     # 每個頻道保留的近期訊息數",
        "RANDOM_INTERJECT_RATE": "0.05",
        "PRICE_TABLE": "custom-model:1:2:3",
    })
    assert cfg.allowed_guild_ids == {1, 2, 3}
    assert cfg.admin_user_ids == {9}
    assert cfg.bot_names == ("大小姐", "Ojou")
    assert cfg.history_max_messages == 30
    assert cfg.random_interject_rate == 0.05
    assert cfg.model_default == "deepseek-v4-flash" and cfg.model_deep == "deepseek-v4-pro"
    assert cfg.price_for("custom-model").output == 3
    assert cfg.validate() == []


def test_config_repr_hides_secrets():
    cfg = Config.from_env({"DISCORD_TOKEN": "super-secret-token", "DEEPSEEK_API_KEY": "sk-abcdef123456"})
    assert "super-secret-token" not in repr(cfg) and "sk-abcdef123456" not in repr(cfg)
    assert any("ALLOWED_GUILD_IDS" in p for p in cfg.validate())


def test_redact_filter():
    f = RedactSecretsFilter(["super-secret-token", "sk-abcdef123456"])
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s key=%s", ("super-secret-token", "sk-abcdef123456"), None)
    f.filter(rec)
    assert rec.getMessage() == "token=*** key=***"
