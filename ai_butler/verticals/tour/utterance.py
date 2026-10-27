"""从用户原话里抽行程参数（U111 生产入口用）。

为什么要它
----------
`TourAgent` 的必填参数是「目的地 / 出行日期 / 出行天数 / 出行人数」（表 11 步骤 1），
缺失时按表 12 **询问用户补齐**。生产入口如果一句都不抽，用户说「帮我规划杭州两日游」
也只能得到「还需要你补充：目的地、出行日期、出行天数、出行人数」——
明明说了目的地和天数，系统却当没听见，等于没接通。

抽取纪律
--------
- **只抽无歧义的模式**：`两日游`、`去杭州`、`明天`、`3 人`。抽不到就留空，
  交给垂类去问。**绝不猜**：猜错的目的地会一路走到 POI 检索和行程方案里，
  用户看到的是一份「别人的行程」。
- 中文数字只支持一到十。`两天` / `2天` / `两日游` 都认。
- 相对日期只认今天/明天/后天：`周末`、`下周` 这类没有唯一日期，留给垂类问。

这里不做地名合法性校验（那是高德 geocode 的事）：本模块只负责「用户明确说了什么」。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

#: 中文数字 → 整数（只到十，够覆盖「两日游」「三个人」）
_CN_NUM: dict[str, int] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_NUM = r"[0-9]{1,2}|[一二两三四五六七八九十]"

#: 天数：`3天` / `三天` / `两日游` / `4日`
_DAYS = re.compile(rf"(?P<n>{_NUM})\s*(?:日游|天|日(?![期程]))")
#: 天数：「杭州两日游」里的「两日游」已被上面覆盖；这里补 `玩三天` 之外的口语
_PARTY = re.compile(rf"(?P<n>{_NUM})\s*(?:个人|人|位)")
#: 日期：绝对日期（2026-11-01 / 11月1日）
_ABS_DATE = re.compile(r"(?P<y>20\d{2})[-/年](?P<m>\d{1,2})[-/月](?P<d>\d{1,2})日?")
_ABS_MD = re.compile(r"(?P<m>\d{1,2})月(?P<d>\d{1,2})日")
#: 目的地候选：`去杭州` / `到杭州玩` / `杭州两日游` / `杭州旅游攻略`
#: 结尾允许标点或句末 —— 「后天去西湖，一日游」的目的地就在逗号前
_TAIL = r"(?:[，,。、；;\s]|$)"
_DEST_VERB = re.compile(
    r"(?:去|到|在|游)(?P<name>[\u4e00-\u9fa5]{2,8}?)(?:市|县|区)?"
    rf"(?:旅游|旅行|玩|游玩|度假|自驾|出差|两日游|三日游|一日游|[0-9]{{1,2}}日游|"
    rf"[0-9]{{1,2}}天|几天|一趟|{_TAIL})")
_DEST_WITH_DAYS = re.compile(
    r"(?P<name>[\u4e00-\u9fa5]{2,6}?)(?:两日游|三日游|一日游|[0-9]{1,2}日游|[0-9]{1,2}天\d?晚?)")
_DEST_WITH_TRIP = re.compile(r"(?P<name>[\u4e00-\u9fa5]{2,6}?)(?:旅游|旅行)(?:攻略|路线|行程)?")
#: 目的地的动词/请求前缀：中文没有空格，「帮我规划杭州」会被整段抓成地名，
#: 必须把这些前缀剥掉（剥不干净的名会一路走到 POI 检索里）
_DEST_PREFIXES: tuple[str, ...] = (
    "帮我", "帮忙", "给我", "请", "麻烦", "想", "要", "打算", "计划",
    "规划", "安排", "定制", "设计", "做一份", "做", "看看", "查一下", "查", "一下",
    "一趟", "个",
)
#: 目的地的玩法后缀：`上海自驾游` → `上海`
_DEST_SUFFIXES: tuple[str, ...] = (
    "自驾游", "自由行", "自驾", "旅游", "旅行", "游玩", "度假", "出差", "玩",
    "市", "县", "区",
)
#: 目的地候选里明显不是地名的词
_DEST_STOPWORDS: frozenset[str] = frozenset({
    "我", "我们", "你", "他", "她", "帮我", "帮忙", "自己", "全家", "一家", "朋友",
    "旅游", "旅行", "行程", "攻略", "周末", "假期", "出差", "景点", "酒店", "民宿",
})


def _to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token)


def _clean_dest(name: str) -> str:
    text = (name or "").strip(" 的，,。.、")
    changed = True
    while changed and text:                  # 前缀可能叠着出现：「帮我规划」
        changed = False
        for prefix in _DEST_PREFIXES:
            if text.startswith(prefix) and len(text) > len(prefix) + 1:
                text = text[len(prefix):].strip()
                changed = True
    changed = True
    while changed and text:
        changed = False
        for suffix in _DEST_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text = text[:-len(suffix)].strip()
                changed = True
    return text.strip(" 的，,。.、")


def _destination(utterance: str) -> str:
    for pattern in (_DEST_WITH_DAYS, _DEST_VERB, _DEST_WITH_TRIP):
        for found in pattern.finditer(utterance):
            name = _clean_dest(found.group("name"))
            if name and name not in _DEST_STOPWORDS and len(name) >= 2:
                return name
    return ""


def _relative_date(utterance: str, today: date) -> str:
    if "后天" in utterance:
        return (today + timedelta(days=2)).isoformat()
    if "明天" in utterance or "明日" in utterance:
        return (today + timedelta(days=1)).isoformat()
    if "今天" in utterance or "今日" in utterance:
        return today.isoformat()
    return ""


def _absolute_date(utterance: str, today: date) -> str:
    found = _ABS_DATE.search(utterance)
    if found:
        try:
            return date(int(found["y"]), int(found["m"]), int(found["d"])).isoformat()
        except ValueError:
            return ""
    found = _ABS_MD.search(utterance)
    if found:
        try:
            candidate = date(today.year, int(found["m"]), int(found["d"]))
        except ValueError:
            return ""
        # 说的日期已经过去 → 当成明年（「12月30日去」说在 1 月，多半是年底）
        if candidate < today - timedelta(days=1):
            try:
                candidate = date(today.year + 1, int(found["m"]), int(found["d"]))
            except ValueError:
                return ""
        return candidate.isoformat()
    return ""


def extract_trip_params(utterance: str, *, today: date | None = None) -> dict[str, Any]:
    """抽出能被 `parse_params` 直接吃下的行程参数。抽不到的不出现（不是填 None）。

    `today` 可注入：测试里「明天」必须是确定日期，否则断言随运行日漂移（E-97）。
    """
    text = (utterance or "").strip()
    if not text:
        return {}
    moment = today or date.today()
    out: dict[str, Any] = {}

    # 日期先抽，抽完把它从文本里挖掉：否则「11月1日到杭州」里的「1日」
    # 会被天数规则当成「1 日游」（实测踩过）。
    start = _absolute_date(text, moment) or _relative_date(text, moment)
    scan = text
    for pattern in (_ABS_DATE, _ABS_MD):
        scan = pattern.sub("\u3000", scan)
    for word in ("今天", "今日", "明天", "明日", "后天"):
        scan = scan.replace(word, "\u3000")
    if start:
        out["start_date"] = start

    days = None
    for found in _DAYS.finditer(scan):
        value = _to_int(found.group("n"))
        if value and 1 <= value <= 30:
            days = value
            break
    if days is not None:
        out["days"] = days

    party = None
    for found in _PARTY.finditer(scan):
        value = _to_int(found.group("n"))
        if value and 1 <= value <= 50:
            party = value
            break
    if party is None and ("独自" in text or "一个人" in text):
        party = 1
    if party is not None:
        out["party_size"] = party

    destination = _destination(text)
    if destination:
        out["destination"] = destination
    return out


__all__ = ["extract_trip_params"]
