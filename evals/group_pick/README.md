# 群組聊天「誰回話」評測

網頁群組聊天（`bot/group.py`）每次要決定接下來誰說話。這裡用 30 段標註好的群組對話，比較不同的判斷方式。

## 用法

在專案根目錄執行：

```
python -m evals.group_pick.run <判斷方式>
```

| 判斷方式 | 說明 | 需要 |
|---|---|---|
| `deepseek` | 目前 `bot/group.py` 的流程（點名＋DeepSeek 判斷） | `.env` 的 DeepSeek 金鑰 |
| `gemini` | 同一個流程，模型換成 Gemini | `GEMINI_API_KEY` |
| `clef-flash`、`clef` | Cloudflare Workers AI 的決策模型直接判斷（每位成員 noul＋誰先說的 choice） | `CF_ACCOUNT_ID`、`CF_API_TOKEN`（或 `run.py` 裡 `CF_ENV` 指的檔案） |
| `clef-flash+rules`、`clef+rules` | 點名規則＋決策模型 | 同上 |

環境變數 `PROFILE` 決定判斷模型看到多少角色資料：`short`（預設，`character.toml` 的 `group_profile`）、`none`、`core`（persona 的她是誰／說話方式／情緒與反應）、`full`（persona 全文）。
結果寫到同資料夾的 `result_<判斷方式>[_<PROFILE>].md`（不進 git）。

## 標註（`cases.jsonl`，一行一題）

- `members`：群組成員；`history`：`[說話者, 內容]`，說話者是成員 key 或 `"user"`（使用者，暱稱 KO）
- `label`：每位成員接下來會不會說話（`yes`／`maybe`／`no`）
- `first`：可以接受的第一個說話的人；空陣列＝應該沒人說話（等使用者）

評分：第一個說話的人是否在 `first` 裡；`label` 是 yes 的人分數低於 no 的人算一對「排序錯」。

新角色加進群組時，請補她的題目：被點名、她的專屬話題、她和其他人的組合。

## 2026-11-08 的比較（第一個說話的人答對幾題／30）

| 判斷方式 | short | core | full |
|---|---|---|---|
| deepseek | 26 | 26 | **27** |
| gemini | 21 | | 23 |
| clef-flash＋規則 | 21 | | 23 |
| clef＋規則 | 21 | | 19 |
| clef | 18 | 16 | 20 |
| clef-flash | 13 | 4 | 4 |

- 維持 DeepSeek。給 persona 全文多答對 1 題，可能只是每次跑的隨機差異。
- Clef 系列太照字面讀角色簡介：「常搶著回話」的小鈴每題都高分，「不太搶話」的綴理就算被點名也很低。它的優點是能分辨「被問」和「只被提到」，速度約 0.2 秒。
- 剩下的錯誤：分不出被問和只被提到的人（name-not-addressee、injection）、話題收尾後還有人接話。
