# 傲嬌大小姐 Discord Bot（DeepSeek 驅動）

在 Discord 群組裡聊天的傲嬌大小姐。使用 DeepSeek API（`openai` SDK），對話歷史、摘要與用量存在 SQLite。
人設全部寫在 [`persona.md`](persona.md)，程式只負責載入。

## 功能概要

- **觸發**：被 @mention、有人回覆 bot 的訊息、私訊（限管理員或同伺服器成員）、訊息包含 `BOT_NAMES` 裡的名字；
  可選的隨機插話（`RANDOM_INTERJECT_RATE > 0`，插話前會先用 flash 模型判斷值不值得插嘴）。
- **一律忽略**：其他 bot、不在 `ALLOWED_GUILD_IDS` 的伺服器、冷卻時間內的同一使用者。
- **脈絡**：每個頻道保留最近 `HISTORY_MAX_MESSAGES` 則訊息（包含別人之間的對話，格式 `KO: 今天好累`）。
  超過上限時，較舊的訊息會被 flash 模型壓成摘要（一次壓到剩一半上限，以減少摘要次數）。
- **Prompt 順序固定**：`persona.md` 全文 → 頻道摘要 → 近期訊息 → 當前訊息，persona 永遠在最前面，有利 DeepSeek 前綴快取。
- **成本控管**：每次呼叫記錄 input / output / cache-hit tokens；超過 `DAILY_TOKEN_BUDGET` 就只回固定訊息；`/usage` 依高峰／空閒時段估算人民幣費用。
- **安全**：不允許 @everyone / @here / 角色提及；回覆超過 2000 字元自動分段；API 錯誤指數退避重試（最多 3 次）；
  log 中的 token / API key 會被遮蔽。

### Slash commands

| 指令 | 說明 | 權限 |
| --- | --- | --- |
| `/deep <問題>` | 用 `MODEL_DEEP`（預設 `deepseek-v4-pro`，開啟思考模式）回答 | 所有人 |
| `/reset` | 清除本頻道歷史與摘要 | 管理員 |
| `/persona_reload` | 重新讀取 `persona.md` | 管理員 |
| `/usage` | 今日 token 用量與預估費用 | 管理員 |

「管理員」指 `ADMIN_USER_IDS` 中的使用者。

---

## 1. 在 Discord Developer Portal 建立 bot

1. 到 <https://discord.com/developers/applications>，按 **New Application**，取個名字。
2. 左側選 **Bot**：
   - 按 **Reset Token** 取得 token，填到 `.env` 的 `DISCORD_TOKEN`（只會顯示一次，不要提交到 git）。
   - 在 **Privileged Gateway Intents** 中 **開啟 `Message Content Intent`**。
     ⚠️ 沒開的話 bot 讀不到訊息內容，名字觸發與對話歷史都會失效。
   - （不需要開啟 Server Members Intent 或 Presence Intent。）
   - 建議關閉 **Public Bot**，避免別人把 bot 拉進其他伺服器。
3. 取得 ID：在 Discord 設定 → 進階 → 開啟 **開發者模式**，之後右鍵伺服器 / 使用者 → **複製 ID**，
   分別填入 `ALLOWED_GUILD_IDS`、`ADMIN_USER_IDS`。

## 2. 邀請 bot 進伺服器

左側選 **OAuth2 → URL Generator**：

- **Scopes**：`bot`、`applications.commands`
- **Bot Permissions**：
  - View Channels（讀取訊息 / 檢視頻道）
  - Send Messages（傳送訊息）
  - Read Message History（讀取訊息歷史）
  - Use Application Commands（使用 slash commands）
  - （選用）Send Messages in Threads，若要在討論串中使用

產生的網址形如（把 `YOUR_CLIENT_ID` 換成 Application ID）：

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=2147552256
```

`2147552256` = View Channels + Send Messages + Read Message History + Use Application Commands。

Slash commands 會在啟動時同步到 `ALLOWED_GUILD_IDS` 中的每個伺服器（伺服器指令會立即生效）。

## 3. 設定

```bash
cp .env.example .env
```

編輯 `.env`，至少填 `DISCORD_TOKEN`、`DEEPSEEK_API_KEY`、`ALLOWED_GUILD_IDS`、`ADMIN_USER_IDS`。

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Discord bot token |
| `DEEPSEEK_API_KEY` | — | DeepSeek API key |
| `ALLOWED_GUILD_IDS` | — | 逗號分隔，只在這些伺服器運作 |
| `ADMIN_USER_IDS` | — | 可用管理指令的使用者 |
| `BOT_NAMES` | `大小姐` | 訊息出現這些名字就會觸發（逗號分隔，不分大小寫） |
| `MODEL_DEFAULT` | `deepseek-flash` | 一般對話、摘要、插話判斷（舊名 `deepseek-v4-flash` 仍可用） |
| `MODEL_DEEP` | `deepseek-v4-pro` | `/deep` 使用 |
| `THINKING_DEFAULT` | `disabled` | 一般對話的思考模式：`enabled` / `disabled` / `omit`（不送參數） |
| `THINKING_DEEP` | `enabled` | `/deep` 的思考模式 |
| `DEEP_MAX_TOKENS` | `8000` | `/deep` 的 max_tokens（推理內容也算在內） |
| `HISTORY_MAX_MESSAGES` | `30` | 每頻道保留的近期訊息數 |
| `DAILY_TOKEN_BUDGET` | `2000000` | 每日 token 上限（input + output） |
| `USER_COOLDOWN_SECONDS` | `5` | 同一使用者觸發的冷卻秒數 |
| `MAX_REPLY_TOKENS` | `600` | 每次回覆的最大 tokens |
| `RANDOM_INTERJECT_RATE` | `0` | 主動插話機率（0～1） |
| `PERSONA_PATH` | `persona.md` | 人設檔路徑 |
| `DB_PATH` | `data/bot.sqlite3` | SQLite 檔路徑 |
| `BOT_TIMEZONE` | `Asia/Taipei` | 計算「今日」用量的時區 |
| `LOG_LEVEL` | `INFO` | log 等級 |
| `PRICE_TABLE` | 內建 | 覆寫高峰時段價目表，格式 `model:命中:未命中:輸出;...`（元 / 百萬 tokens） |
| `PRICE_OFFPEAK_RATIO` | `0.5` | 空閒時段價格比例 |
| `CN_HOLIDAYS` | — | 中國法定節假日（整天算空閒時段），逗號分隔 `YYYY-MM-DD` |

### 費用估算

`/usage` 的費用依 DeepSeek 官方價目表（人民幣），**每次呼叫當下**判斷高峰／空閒時段後計算並存進 SQLite：

| 模型 | 輸入（快取命中） | 輸入（未命中） | 輸出 |
| --- | --- | --- | --- |
| `deepseek-flash` | 0.04 元 | 2 元 | 8 元 |
| `deepseek-v4-pro` | 0.30 元 | 9 元 | 27 元 |

以上是高峰時段每百萬 tokens 的價格；空閒時段減半。高峰時段為北京時間週一至週五 9:00–12:00、14:00–18:00，
週末與中國法定節假日全天算空閒。法定節假日無法自動判斷，請用 `CN_HOLIDAYS` 設定。

### 思考模式

兩個模型預設都開啟思考模式。一般聊天、摘要、插話判斷都會送出 `thinking: {"type": "disabled"}` 關閉思考，
以節省 token、降低延遲；`/deep` 則開啟思考，並使用較大的 `DEEP_MAX_TOKENS`。

## 4. 執行方式

### 本機執行

需要 Python 3.11 以上。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

### Docker

```bash
docker build -t tsundere-bot .
docker run -d --name tsundere-bot --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/persona.md:/app/persona.md:ro" \
  tsundere-bot
```

把 `persona.md` 掛載進容器，修改後就能直接 `/persona_reload`，不必重建映像。
（`data/` 目錄需要容器內 uid 1000 可寫入：`mkdir -p data && sudo chown 1000 data`。）

### systemd 常駐

```bash
sudo useradd --system --create-home botuser
sudo git clone <this repo> /opt/tsundere-bot
cd /opt/tsundere-bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env
sudo mkdir -p data && sudo chown -R botuser:botuser /opt/tsundere-bot
sudo cp deploy/bot.service /etc/systemd/system/tsundere-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now tsundere-bot
journalctl -u tsundere-bot -f
```

## 5. 修改人設

1. 編輯 `persona.md`（純文字 / Markdown，整份會原封不動放進 system prompt 的最前面）。
2. 在 Discord 輸入 `/persona_reload`（管理員），bot 會重新讀檔，下一則回覆起生效，不用重啟。
3. 若讀檔失敗（檔案不存在或是空的），會保留舊的人設並回報錯誤。

小提醒：persona 內容一改，DeepSeek 的前綴快取就要重新累積，短時間內快取命中率會下降，這是正常的。

## 測試

```bash
pip install -r requirements-dev.txt
pytest
```

測試完全使用 mock，不會連到 Discord 或 DeepSeek。涵蓋：觸發規則、冷卻、歷史裁切與摘要觸發、訊息分段、預算超限、
重試、用量紀錄、log 遮蔽等。

## 專案結構

```
bot/
  main.py       # 進入點、Discord client
  config.py     # .env 設定
  triggers.py   # 觸發判斷、冷卻
  chat.py       # 訊息處理流程（觸發 → 冷卻 → 呼叫模型 → 回覆）
  history.py    # 歷史、摘要、prompt 組裝
  llm.py        # DeepSeek 呼叫、重試、預算、用量紀錄
  commands.py   # slash commands
  storage.py    # SQLite
  persona.py    # persona.md 載入
  text.py       # 2000 字元分段
persona.md
tests/
deploy/bot.service
Dockerfile
```
