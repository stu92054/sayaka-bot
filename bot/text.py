"""文字處理工具。"""
from __future__ import annotations

DISCORD_LIMIT = 2000


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """把超過 limit 字元的回覆分段，優先在換行、其次在空白處切開。"""
    if limit <= 0:
        raise ValueError("limit must be positive")
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            space = window.rfind(" ")
            cut = space if space >= limit // 2 else cut
        if cut <= 0:
            cut = limit
        chunk = text[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks

# 模型偶爾會在繁體回覆裡混進簡體字（例：「听到的时候」）。只收「繁體中文和日文都不會用」的簡體字，
# 一對一換成繁體；和日文新字體或繁體共用的字（学、会、来、没、画、体、机、后、里、着、几等）不動，免得改壞日文專有名詞。
_S2T = str.maketrans(
    "们这说还讲两个么吗时对为过让发见觉现开关门问间东车长头买卖钱爱听话语认识谁读练习级经给红绿线结终难欢乐动劳务应该边远进运选适样极实层岁师帮带归录总惊戏战扫拥择换无显晓术杂权构树桥检气汉汤测济热爷牵环电疗盖监确种稳穷笔简类紧纪约纯纸细织绍络统继续维网罗职联肠脑节苏药获虽补规视亲观计订记许论设访证评词试诗诚诉详请课调谈谢贵费资赶跃转软轻较辆辈达迟递释钟铁银错锅闪闭闲闹阳际陆队阶随页顶项顺须顾预领题颜风饭饮饱饿馆驾验鱼鸟鸡龙齐",
    "們這說還講兩個麼嗎時對為過讓發見覺現開關門問間東車長頭買賣錢愛聽話語認識誰讀練習級經給紅綠線結終難歡樂動勞務應該邊遠進運選適樣極實層歲師幫帶歸錄總驚戲戰掃擁擇換無顯曉術雜權構樹橋檢氣漢湯測濟熱爺牽環電療蓋監確種穩窮筆簡類緊紀約純紙細織紹絡統繼續維網羅職聯腸腦節蘇藥獲雖補規視親觀計訂記許論設訪證評詞試詩誠訴詳請課調談謝貴費資趕躍轉軟輕較輛輩達遲遞釋鐘鐵銀錯鍋閃閉閒鬧陽際陸隊階隨頁頂項順須顧預領題顏風飯飲飽餓館駕驗魚鳥雞龍齊",
)


def to_traditional(text: str) -> str:
    """把回覆裡零星的簡體字換成繁體。"""
    return text.translate(_S2T)
