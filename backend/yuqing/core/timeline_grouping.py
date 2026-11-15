"""Conservative event grouping; authoritative claims remain separate inside each card."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from yuqing.core.claim_semantics import publication_actor

_DATE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
_ACTION = re.compile(r"发布|公布|发文|发帖|作出|成立|启动|宣布|提交|提起")
_DOCUMENT = re.compile(r"测试视频|检测报告|测试报告|情况说明|通报|声明|评测|测评|判决|裁定")
_POST_DOCUMENT = re.compile(r"(?:自述|求助|公开)?(?:文章|帖文|帖子)")
_INCIDENT_DATE = re.compile(r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?:(?P<day>\d{1,2})日)?")
_ROLE = re.compile(r"(?:该校|同校)?(?:一名|涉事|当事)?(?:女学生|男学生|女生|男生|学生)")
_NUMBER = re.compile(
    r"\d+(?:\.\d+)?(?:\s*(?:km/h|mm|N|%|％|台|次|人|份|辆|项))?"
    r"|[零〇一二两三四五六七八九十百千万亿]+(?:台|次|人|份|辆|项|部门)"
)


def _compact(text: str) -> str:
    return re.sub(r"[\W_]", "", text).lower()


def _profile(node: dict) -> dict:
    text = node["text"]
    dated = _DATE.search(text)
    timing = (
        re.match(r"\s*(上午|下午|晚间|夜间|晚|\d{1,2}时)", text[dated.end() :]) if dated else None
    )
    without_date = _DATE.sub("", text, count=1).lstrip("，, ")
    without_date = re.sub(r"^(?:上午|下午|晚间|夜间|晚|\d{1,2}时)[，,\s]*", "", without_date)
    actor = publication_actor(text)
    if not actor:
        attributed = re.search(
            r"报道(?:称)?[，,]\s*(.{2,40}?)(?:当日|同日)(?:发布|公布)", without_date
        )
        actor = attributed[1] if attributed else None
    if not actor:
        prefix = re.match(
            r"(.{2,60}?)(?=于|在|就|作出|成立|启动|发文|发帖|宣布|提交|提起)", without_date
        )
        actor = prefix[1] if prefix else ""
    actor = re.sub(r"(?:相关账号|官方账号|账号|方面|官方网站|官网|网站)$", "", actor)
    actor = re.sub(r"在(?:社交平台|微信公众号|微博|网络平台)$", "", actor)
    actor = re.sub(r"(?<=大学)党委(?=.{2,12}(?:部|处|办公室)$)", "", actor)
    action = _ACTION.search(without_date)
    body = without_date[action.end() :] if action else without_date
    document = _DOCUMENT.search(body[:60]) if action and action[0] in {"发布", "公布"} else None
    title = re.match(r"\s*《([^》]+)》", body)
    kind = document[0] if document else action[0] if action else ""
    content = body[document.end() :] if document else body
    direct_event = False
    if action is None and dated is not None and dated.start() == 0:
        direct = re.search(r"吃出|丢弃|发现|终止|解除", without_date)
        if direct and not re.search(r"报道|回顾|转述|援引", without_date[: direct.start()]):
            prefix = without_date[: direct.start()].split("在", 1)[0].strip("，, ")
            actor = "学生" if prefix.endswith("学生") else prefix
            kind, body = direct[0], without_date[direct.end() :]
            content = body
            direct_event = True
    if (
        dated is not None
        and dated.start() == 0
        and "视频" in without_date
        and re.search(r"传播|出现", without_date)
    ):
        # A public video disclosure is an event; it is not another occurrence
        # of the incident described in that video or a new publication source.
        actor, kind = "公开视频", "视频公开传播"
        direct_event = False
        content = re.sub(r"一段|一则|社交平台|网络|视频|广泛|传播|出现|并|称", "", without_date)
        content = re.sub(r"疑(?=吃出|发现)", "疑似", content)
        body = content
    post_document = _POST_DOCUMENT.match(body)
    if kind in {"发文", "发帖"} or (kind == "发布" and post_document):
        kind = "发帖"
        if post_document:
            content = body[post_document.end() :]
        female = re.search(r"女生|女学生", actor)
        male = re.search(r"男生|男学生", actor)
        if female or male:
            institution = re.match(r"(.{2,30}?(?:大学|学院|学校))", actor)
            actor = (institution[1] if institution else "") + ("女生" if female else "男生")
    year = str(node["date"])[:4]
    content = re.sub(r"(?:同年|当年)(?=\d{1,2}月)", year + "年", content)
    dates = [
        (m["year"], str(int(m["month"])), str(int(m["day"])) if m["day"] else "")
        for m in _INCIDENT_DATE.finditer(content)
    ]
    quantities = _INCIDENT_DATE.sub("", content)
    step = (
        re.search(r"另一份|另行|第二份|第二次|再次|随后", body[: document.start()])
        if document
        else None
    )
    topic = re.search(r"(?:针对|关于)([^，,。；;《》]{2,30}?)(?:的|问题|事件|事项)", body)
    # Normalize presentation fillers only. Numbers and negation are compared separately.
    normalized = _ROLE.sub("学生", content)
    normalized = normalized.replace("受到", "遭").replace("遭到", "遭")
    normalized = (
        normalized.replace("疑似为", "疑似").replace("事发当日", "当日").replace("涉事", "")
    )
    normalized = _compact(
        re.sub(r"相关账号|全部|测试中|制动时|宣布|称|[的在中时其]", "", normalized)
    )
    return {
        "actor": _compact(actor),
        "actor_root": _compact(m[1])
        if (m := re.match(r"(.{2,30}?(?:大学|学院|公司|集团|医院|学校))", actor))
        else "",
        "subjects": sorted(
            set(
                re.sub(r"^(?:(?:一名|涉事)?学生在|称|据|在|由|于|至)+", "", name)
                for name in re.findall(
                    r"(?:^|[，,。；;]|在|由|于|至|称)([\u4e00-\u9fff]{2,18}?(?:大学|学院|医院|公司|学校))",
                    without_date if direct_event else content,
                )
            )
        ),
        "object_genders": sorted(
            {
                "女" if "女" in role else "男"
                for role in re.findall(r"女学生|男学生|女生|男生", content)
            }
        ),
        "kind": kind,
        "title": _compact(title[1]) if title else "",
        "document": bool(document),
        "timing": timing[1] if timing else "",
        "body": normalized,
        "step": step[0] if step else "",
        "topic": _compact(topic[1]) if topic else "",
        "numbers": _NUMBER.findall(quantities),
        "dates": dates,
        "negative": re.findall(r"未|没有|否认|不|无|否", content),
        "outcomes": re.findall(r"撤销|撤回|维持|驳回|终止|召回|暂停|恢复|启动|成立", content),
        "scope": re.findall(r"部分|个别|全部|均|仅|所有", content),
        "condition": re.findall(r"静态|动态|纵向|侧向", content),
        "text": _compact(without_date),
    }


def _similar(left: str, right: str) -> bool:
    if left == right:
        return True
    match = SequenceMatcher(None, left, right, autojunk=False)
    shorter = min(len(left), len(right))
    longest = max((part.size for part in match.get_matching_blocks()), default=0)
    return (
        match.ratio() >= 0.86
        or (shorter >= 24 and longest >= 20 and longest / shorter >= 0.65 and match.ratio() >= 0.7)
        or (shorter >= 16 and longest == shorter)
    )


def _compatible(left: dict, right: dict, a: dict, b: dict) -> bool:
    if left["date"] != right["date"]:
        return False
    if a["title"] and b["title"] and a["title"] != b["title"]:
        return False
    if a["timing"] and b["timing"] and a["timing"] != b["timing"]:
        return False
    if a["step"] != b["step"]:
        return False
    if a["topic"] and b["topic"] and a["topic"] != b["topic"]:
        return False
    if a["subjects"] and b["subjects"] and a["subjects"] != b["subjects"]:
        return False
    if a["object_genders"] and b["object_genders"] and a["object_genders"] != b["object_genders"]:
        return False
    if a["numbers"] and b["numbers"] and a["numbers"] != b["numbers"]:
        return False
    if a["dates"] and b["dates"]:
        if len(a["dates"]) != len(b["dates"]):
            return False
        for left_date, right_date in zip(a["dates"], b["dates"], strict=True):
            if left_date[:2] != right_date[:2] or (
                left_date[2] and right_date[2] and left_date[2] != right_date[2]
            ):
                return False
    for field in ("outcomes", "scope", "condition"):
        if a[field] and b[field] and a[field] != b[field]:
            return False
    if a["negative"] != b["negative"]:
        # A release overview can accompany its substance. Conflicting assertions stay apart.
        outcome = r"发生|存在|发现|断裂|撤销|合格|通过"
        if re.search(outcome, left["text"]) and re.search(outcome, right["text"]):
            return False
    return True


def _same_event(left: dict, right: dict, a: dict, b: dict) -> bool:
    if not left.get("claim_ref") or not right.get("claim_ref"):
        return False
    if not _compatible(left, right, a, b):
        return False
    if a["text"] == b["text"]:
        return True
    actors_match = a["actor"] == b["actor"] or bool(
        a["actor_root"]
        and b["actor_root"]
        and (a["actor"] == b["actor_root"] or b["actor"] == a["actor_root"])
    )
    if a["kind"] == b["kind"] == "发帖":
        # A role may be written with or without the already-named institution.
        actors_match = (
            actors_match
            or (a["actor"] in {"女生", "男生"} and b["actor"].endswith(a["actor"]))
            or (b["actor"] in {"女生", "男生"} and a["actor"].endswith(b["actor"]))
        )
    if not a["actor"] or not actors_match or not a["kind"] or a["kind"] != b["kind"]:
        return False
    if (
        a["negative"] == b["negative"]
        and a["numbers"] == b["numbers"]
        and (
            _similar(a["body"], b["body"])
            or (
                a["kind"] == "发帖"
                and min(len(a["body"]), len(b["body"])) >= 10
                and sum(
                    m.size
                    for m in SequenceMatcher(
                        None, a["body"], b["body"], autojunk=False
                    ).get_matching_blocks()
                )
                >= 0.95 * min(len(a["body"]), len(b["body"]))
                and SequenceMatcher(None, a["body"], b["body"], autojunk=False)
                .find_longest_match()
                .size
                >= 5
            )
        )
    ):
        return True
    return bool(
        a["document"]
        and b["document"]
        and set(left["evidence_refs"]) & set(right["evidence_refs"])
        and SequenceMatcher(None, a["body"], b["body"], autojunk=False).find_longest_match().size
        >= 8
    )


def group_timeline_events(nodes: list[dict]) -> list[dict]:
    """Group confident matches without deleting members or aggregating their badges."""
    groups: list[list[tuple[dict, dict]]] = []
    seen = set()
    for node in [member for item in nodes for member in item.get("related_claims", [item])]:
        key = node.get("claim_ref") or (
            node["date"],
            node["text"],
            tuple(node.get("claim_refs", [])),
            tuple(node.get("evidence_refs", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        profile = _profile(node)
        group = next(
            (
                g
                for g in groups
                if all(_compatible(node, old, profile, p) for old, p in g)
                and any(_same_event(node, old, profile, p) for old, p in g)
            ),
            None,
        )
        if group is None:
            groups.append([(node, profile)])
        else:
            group.append((node, profile))
    result = []
    for group in groups:
        members = [node for node, _ in group]
        representative = max(members, key=lambda node: len(_compact(node["text"])))
        item = dict(representative)
        if len(members) > 1:
            item["claim_refs"] = list(
                dict.fromkeys(ref for node in members for ref in node["claim_refs"])
            )
            item["evidence_refs"] = list(
                dict.fromkeys(ref for node in members for ref in node["evidence_refs"])
            )
            item["related_claims"] = members
        result.append(item)
    return result


def event_display_claims(item: dict) -> list[dict]:
    """Use original sentences; fold only overviews or overlapping restatements."""
    members = item.get("related_claims", [item])
    displayed: list[dict] = []
    for member in sorted(members, key=lambda node: len(_compact(node["text"])), reverse=True):
        profile = _profile(member)
        body = _DATE.sub("", member["text"])
        generic = bool(
            re.search(r"(?:回应|涉及).{0,30}(?:相关|有关)?问题[。\s]*$", body)
            and not re.search(r"未|不|宣布|决定|升级|撤销|维持|驳回|\d", profile["body"])
        )
        if displayed and (
            generic
            or any(
                profile["numbers"] == _profile(old)["numbers"]
                and profile["negative"] == _profile(old)["negative"]
                and _similar(profile["body"], _profile(old)["body"])
                for old in displayed
            )
        ):
            continue
        displayed.append(member)
    return displayed


_DATED_TIME = (
    r"(?:\d{4}年\d{1,2}月\d{1,2}日)?(?:凌晨|早上|上午|中午|下午|晚间|夜间|晚|"
    r"\d{1,2}时(?:\d{1,2}分)?)?"
)
_PUBLISHED_MATERIAL = re.compile(
    rf"^(?P<actor>[\w]{{2,32}}?)(?:相关账号|官方账号|有关人员|官网|网站)?(?:于)?"
    rf"{_DATED_TIME}(?:当日|同日)?(?:(?P<video>发布(?:的)?(?:场地|封闭场地)?(?:测试|实测|试验|评测|测评)?视频|(?:测试|实测|试验)视频(?=称|显示))|"
    r"(?P<notice>发布(?:了)?(?:情况|调查复核)?通报|(?:情况|调查复核)?通报(?=称|载明|公布|显示|已))|"
    r"(?P<reply>发布(?:了)?(?:情况说明|声明)|情况说明(?=称|载明|公布|显示)|(?:在回应中|回应|声明)(?=称)))"
)
_REPORTED = re.compile(
    r"^(?:据)?[^，,。]{2,32}?(?:(?:综合)?报道(?:称|指出|显示)?|转载的|转述|稿件[，,]\s*(?:报道称|称))"
    r"[，,\s]*"
)
_DIFFERENT_RELEASE = re.compile(r"另行|另一份|第二份|第二次|再次|补充通报|[《》]")


def publication_reading_parts(text: str) -> tuple[str, str, str, str] | None:
    """Identify explicitly named published materials for reading, keeping assertions intact."""
    leading = _DATE.match(text)
    timing = re.search(
        r"\d{1,2}日(凌晨|早上|上午|中午|下午|晚间|夜间|晚|\d{1,2}时(?:\d{1,2}分)?)", text
    )
    candidate = text[leading.end() :] if leading else text
    candidate = re.sub(
        r"^(?:凌晨|早上|上午|中午|下午|晚间|夜间|晚|\d{1,2}时(?:\d{1,2}分)?)", "", candidate
    )
    candidate = _REPORTED.sub("", candidate.lstrip("，, "), count=1)
    candidate = (
        candidate.removeprefix("“").replace("”", "", 1) if candidate.startswith("“") else candidate
    )
    if _DIFFERENT_RELEASE.search(candidate[:48]):
        return None
    match = _PUBLISHED_MATERIAL.match(candidate)
    if not match:
        return None
    actor = re.sub(r"^(?:汽车|科技|财经|体育)?(?:资讯|评测|测评)平台(?=.{2})", "", match["actor"])
    if actor.startswith(("某", "据", "一段", "一则", "网传")) or actor in {
        "校方",
        "学校",
        "官方",
        "机构",
        "企业",
        "公司",
    }:
        return None
    kind = "测试视频" if match["video"] else "通报" if match["notice"] else "回应"
    detail = re.sub(r"^[，,：:\s]*(?:称|载明|显示|公布)?[，,：:\s]*", "", candidate[match.end() :])
    slot = timing[1] if timing else ""
    return actor, kind, slot, detail


def publication_fact_key(parts: tuple[str, str, str, str]) -> str:
    """Normalize attribution and equivalent units in the displayed proposition only."""
    detail = parts[3].casefold()
    detail = re.sub(
        r"(\d+(?:\.\d+)?)\s*km/h\s*(?:至|[-—~－])\s*(?:零|静止|0)(?:km/h)?"
        r"|(\d+(?:\.\d+)?)\s*[-—~－]\s*0\s*km/h",
        lambda match: (match[1] or match[2]) + "km/h至0",
        detail,
    )
    counts = dict(zip("一二两三四五六七八九", "1223456789", strict=True))
    detail = re.sub(r"([一二两三四五六七八九])(?:台|辆)", lambda m: counts[m[1]] + "辆", detail)
    detail = re.sub(r"(\d+)台", r"\1辆", detail)
    detail = re.sub(r"(制动)(?:测试)?(?:中|时)", r"\1", detail)
    detail = detail.replace("km/h至0的", "km/h至0")
    if re.match(r"\s*\d+辆", detail):
        detail = re.sub(r"(?<!平)均(?![值衡等匀数分])", "", detail)
    detail = re.sub(r"(?<!\d)[.:-]|[.:-](?!\d)", "", detail).replace("％", "%")
    return re.sub(r"[^\w.%:-]", "", detail)


def same_publication_fact(a: tuple[str, str, str, str], b: tuple[str, str, str, str]) -> bool:
    """Keep the date and all decision, number and polarity markers while grouping."""
    if a[:3] != b[:3] or not a[3] or not b[3]:
        return False
    left, right = publication_fact_key(a), publication_fact_key(b)
    if left == right:
        return True
    markers = (
        r"\d+(?:\.\d+)?|撤销|维持|驳回|批准|召回|未|没有|否认|不|无|否|仅|部分|全部|均|先后|"
        r"自购|自费购买|侧向|纵向|静态|动态"
    )
    if re.findall(markers, left) != re.findall(markers, right):
        return False
    return min(len(left), len(right)) >= 24 and _similar(left, right)


def timeline_reading_groups(nodes: list[dict]) -> list[dict]:
    """Present one publication with its separate details, without synthesizing claims."""
    result: list[dict] = []
    publications: dict[tuple, list[dict]] = {}
    for item in group_timeline_events(nodes):
        parts = publication_reading_parts(item["text"])
        if not parts or not item.get("claim_ref"):
            result.append(item)
            continue
        actor, kind, slot, _ = parts
        key = (item["date"], actor, kind)
        buckets = publications.setdefault(key, [])
        group = next(
            (
                old
                for old in buckets
                if not slot or not old["publication_time"] or old["publication_time"] == slot
            ),
            None,
        )
        if group is None:
            group = {
                **item,
                "publication_title": f"{actor}{'回应' if kind == '回应' else '发布' + kind}",
                "publication_time": slot,
                "related_claims": list(item.get("related_claims", [item])),
            }
            buckets.append(group)
            result.append(group)
        else:
            group["publication_time"] = group["publication_time"] or slot
            for field in ("claim_refs", "evidence_refs"):
                group[field] = list(dict.fromkeys([*group[field], *item[field]]))
            group["related_claims"].extend(item.get("related_claims", [item]))
    return result


def timeline_publication_details(item: dict) -> list[tuple[str, dict]]:
    """Show each distinct detail with the status of its own underlying claim."""
    details = {}
    for member in item["related_claims"]:
        parts = publication_reading_parts(member["text"])
        if not parts or not parts[3]:
            details.setdefault(_compact(member["text"]), (member["text"], member))
            continue
        details.setdefault(publication_fact_key(parts), (parts[3], member))
    return list(details.values())
