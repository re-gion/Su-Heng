"""陈述对象的保守识别；发布记录不等于发布内容属实。"""

from __future__ import annotations

import re


def publication_actor(text: str) -> str | None:
    """Only recognize an explicit issuer followed by a publication action."""
    value = re.sub(r"^\d{4}年\d{1,2}月\d{1,2}日(?:夜间|晚间|上午|下午|晚)?[，,\s]*", "", text)
    match = re.match(
        r"(?P<actor>[^，,。；;：:]{2,40}?)(?:于|在)?"
        r"(?:\d{4}年\d{1,2}月\d{1,2}日(?:夜间|晚间|上午|下午|晚)?)?"
        r"(?:发布|公布|通报称|声明称|回应称|宣布)(?P<body>.+)",
        value,
    )
    if not match:
        return None
    body = match.group("body")
    substantive = re.search(r"证实|证明|确实|属实|事实是", body)
    attribution = re.search(r"称|表示|记载|载明", body)
    if substantive and (not attribution or attribution.start() > substantive.start()):
        return None
    actor = match.group("actor").rstrip("于在").strip()
    if any(word in actor for word in ("网传", "据", "报道称", "网友", "消息", "有人")):
        return None
    return actor


def compound_publication_details(text: str) -> bool:
    """Reject a release's substance joined to separate layout/credit assertions."""
    return bool(
        re.search(
            r"[，,](?:通报|报道|页面|文章|文件)(?:落款|署名|标注来源|来源标注|发表于|发布于)",
            text,
        )
    )
