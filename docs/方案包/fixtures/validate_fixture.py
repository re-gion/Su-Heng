#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 report-ir-v0.1.fixture.json 与其数据库种子的一致性。

这不是渲染器实现，只是把 05-核心契约 §2.4 里**能纯静态检查**的规则跑一遍，
保证方案包里这份 fixture 本身是自洽的、可以直接当 M0 的第一条测试数据。
需要跑 LLM 的 R14（蕴含校验）与需要原文的 R9（verbatim 子串匹配）不在这里做。

用法：python validate_fixture.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IR = json.load(open(os.path.join(HERE, "report-ir-v0.1.fixture.json"), encoding="utf-8"))
DB = json.load(open(os.path.join(HERE, "report-ir-v0.1.db-seed.json"), encoding="utf-8"))

errors = []
checks = []


def check(name, ok, detail=""):
    checks.append((name, ok, detail))
    if not ok:
        errors.append("%s%s" % (name, (" -> " + detail) if detail else ""))


claims = {c["local_id"]: c for c in DB["claim"]}
evidence = {e["local_id"]: e for e in DB["evidence"]}
binding = {}
for ce in DB["claim_evidence"]:
    binding.setdefault(ce["claim"], set()).add(ce["evidence"])

blocks = IR["blocks"]
by_type = {}
for b in blocks:
    by_type.setdefault(b["type"], []).append(b)


def walk_refs(node, path=""):
    """收集 IR 中出现的全部 claim/evidence 引用，带所属条目上下文。"""
    out = []
    if isinstance(node, dict):
        item_claim = node.get("claim_ref")
        claim_refs = node.get("claim_refs") or ([item_claim] if item_claim else [])
        ev_refs = list(node.get("evidence_refs") or [])
        if node.get("quote_ref"):
            ev_refs.append(node["quote_ref"])
        if node.get("evidence_ref") and "citations" not in node:
            ev_refs.append(node["evidence_ref"])
        for c in node.get("citations") or []:
            if c.get("evidence_ref"):
                ev_refs.append(c["evidence_ref"])
        if claim_refs or ev_refs:
            out.append((path, claim_refs, ev_refs))
        for k, v in node.items():
            out.extend(walk_refs(v, path + "/" + str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(walk_refs(v, path + "[%d]" % i))
    return out


refs = walk_refs(blocks, "blocks")

# --- 结构与版本 ---
check("顶层必填字段齐全",
      all(k in IR for k in ("schema_version", "min_reader_minor", "report_id", "task", "metrics", "blocks")))
check("min_reader_minor 是整数且 <= schema_version 的 minor",
      isinstance(IR["min_reader_minor"], int)
      and IR["min_reader_minor"] <= int(IR["schema_version"].split(".")[1]))
check("block_id 唯一", len({b["block_id"] for b in blocks}) == len(blocks))

# --- R2 悬空引用 ---
bad_c = sorted({c for _, cs, _ in refs for c in cs if c not in claims})
bad_e = sorted({e for _, _, es in refs for e in es if e not in evidence})
check("R2 无悬空 claim_ref", not bad_c, str(bad_c))
check("R2 无悬空 evidence_ref", not bad_e, str(bad_e))

# --- R17 引用闭包：带 claim_ref 的条目，其 evidence_refs 必须是该 claim 绑定集合的子集 ---
violations = []
for path, cs, es in refs:
    if len(cs) == 1 and es:
        allowed = binding.get(cs[0], set())
        extra = set(es) - allowed
        if extra:
            violations.append("%s: %s 不在 %s 的绑定集合内" % (path, sorted(extra), cs[0]))
check("R17 引用闭包成立", not violations, "; ".join(violations))

# --- R3 核查表条目引用卡片非空，且与数据库绑定一致 ---
fc_problems = []
for blk in by_type.get("fact_check_table", []):
    for item in blk["items"]:
        cid = item["claim_ref"]
        cites = [c["evidence_ref"] for c in item.get("citations", [])]
        if not cites:
            fc_problems.append("%s 无引用卡片" % cid)
        if set(cites) != binding.get(cid, set()):
            fc_problems.append("%s 引用卡片 %s != 绑定 %s" % (cid, sorted(cites), sorted(binding.get(cid, set()))))
check("R3 引用卡片非空且等于数据库绑定", not fc_problems, "; ".join(fc_problems))

# --- R15 权威字段与数据库一致（fixture 是回填后的形态，必须完全相等）---
mismatch = []
for blk in by_type.get("fact_check_table", []):
    for item in blk["items"]:
        c = claims[item["claim_ref"]]
        for f in ("text", "statement_kind", "badge", "verdict", "independent_sources",
                  "max_source_tier", "verification_state", "rumor_text", "correction_text"):
            if f in item or c.get(f) is not None:
                if item.get(f) != c.get(f):
                    mismatch.append("%s.%s: IR=%r DB=%r" % (item["claim_ref"], f, item.get(f), c.get(f)))
check("R15 权威字段与数据库逐字段相等", not mismatch, "; ".join(mismatch))

# --- 引用卡片的 relation / quote 与 claim_evidence 一致 ---
ce_index = {(ce["claim"], ce["evidence"]): ce for ce in DB["claim_evidence"]}
cite_bad = []
for blk in by_type.get("fact_check_table", []):
    for item in blk["items"]:
        for c in item.get("citations", []):
            row = ce_index[(item["claim_ref"], c["evidence_ref"])]
            for f in ("relation", "quote_type", "quote", "quote_start", "quote_end"):
                if f in c and c[f] != row.get(f):
                    cite_bad.append("%s/%s.%s" % (item["claim_ref"], c["evidence_ref"], f))
            if c.get("quote_type") == "verbatim" and not (
                    c.get("quote_start") is not None and c.get("quote_end") is not None
                    and row.get("quote_verified") == 1):
                cite_bad.append("%s/%s verbatim 不变量" % (item["claim_ref"], c["evidence_ref"]))
check("引用卡片与 claim_evidence 一致且 verbatim 满足不变量", not cite_bad, "; ".join(cite_bad))

# --- R16 核验完整性：badge 非 unverified 时必须 verification_state=complete ---
r16 = [c["local_id"] for c in DB["claim"]
       if c.get("badge") in ("verified", "refuted", "disputed") and c.get("verification_state") != "complete"]
check("R16 未完成核验的 claim 不带非 unverified 徽章", not r16, str(r16))

# --- R11 局限性必出；R12 limitation_ref 成对 ---
lim = by_type.get("limitations", [])
check("R11 局限性板块存在且非空", len(lim) == 1 and len(lim[0]["items"]) > 0)
lim_ids = {i["id"] for i in lim[0]["items"]} if lim else set()
dangling = [b["block_id"] for b in blocks if b.get("limitation_ref") and b["limitation_ref"] not in lim_ids]
check("R12 limitation_ref 都能在 08 板块找到", not dangling, str(dangling))

# --- R13 证据附录闭包 ---
used_e = {e for _, _, es in refs for e in es}
appendix = by_type.get("evidence_appendix", [])
app_e = {i["evidence_ref"] for i in appendix[0]["items"]} if appendix else set()
check("R13 IR 中用到的证据全部在附录里", used_e <= app_e, str(sorted(used_e - app_e)))
check("附录不含库里不存在的证据", app_e <= set(evidence), str(sorted(app_e - set(evidence))))

# --- R4/R5/R6/R7 ---
check("R4 无 illustrative 块", all(b.get("data_basis") != "illustrative" for b in blocks))
check("R5 编辑判断块的 editorial_basis 非空",
      all(b.get("editorial_basis") for b in blocks if b.get("is_editorial")))
check("R6 section 01/03 无编辑判断",
      all(not b.get("is_editorial") for b in blocks if b.get("section") in ("01", "03")))
check("R7 数值块都有 data_basis",
      all("data_basis" in b for b in blocks if b["type"] in ("kpi_grid", "chart")))

# --- R8 时间线/历史卡的 evidence_refs 非空 ---
r8 = []
for blk in by_type.get("timeline", []):
    r8 += [n["date"] for n in blk["nodes"] if not n.get("evidence_refs")]
for blk in by_type.get("history_compare", []):
    r8 += [c["event_name"] for c in blk["cards"] if not c.get("evidence_refs")]
check("R8 时间线节点与历史卡都有证据", not r8, str(r8))

# --- 执行摘要每句带 claim_ref（R14 的确定性前置条件）---
es_missing = []
for blk in by_type.get("executive_summary", []):
    for key in ("what", "why", "so_what"):
        for s in blk.get(key, []):
            if not s.get("claim_ref"):
                es_missing.append(s.get("text", "")[:20])
check("执行摘要每条结论都带 claim_ref", not es_missing, str(es_missing))

# --- R18 指标恒等式与比率一致性 ---
m = IR["metrics"]
check("R18 candidate == rendered + rejected",
      m["key_claims_candidate"] == m["key_claims_rendered"] + m["key_claims_rejected"],
      "%s != %s + %s" % (m["key_claims_candidate"], m["key_claims_rendered"], m["key_claims_rejected"]))

rendered = [c for c in DB["claim"] if c["local_id"] != "C008"]
check("rendered 数与种子一致", len(rendered) == m["key_claims_rendered"])


def ratio(badge):
    return round(sum(1 for c in rendered if c["badge"] == badge) / float(m["key_claims_rendered"]), 3)


for name, badge in (("verified_rate", "verified"), ("disputed_rate", "disputed"), ("refuted_rate", "refuted")):
    check("%s 与种子一致" % name, abs(m[name] - ratio(badge)) < 0.001,
          "IR=%s 实算=%s" % (m[name], ratio(badge)))

W = {1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.2}
num = sum(W[c["max_source_tier"]] for c in rendered if c["badge"] == "verified")
den = sum(W[c["max_source_tier"]] for c in rendered)
check("weighted_verified_rate 与种子一致", abs(m["weighted_verified_rate"] - round(num / den, 3)) < 0.001,
      "IR=%s 实算=%s" % (m["weighted_verified_rate"], round(num / den, 3)))

check("evidence_total 与种子一致", m["evidence_total"] == len(evidence))
check("evidence_fetched 与种子一致",
      m["evidence_fetched"] == sum(1 for e in evidence.values() if e["fetch_status"] == "fetched"))
check("evidence_snippet_only 与种子一致",
      m["evidence_snippet_only"] == sum(1 for e in evidence.values() if e["fetch_status"] != "fetched"))
check("independent_publishers 与种子一致（按 publisher_entity 归并）",
      m["independent_publishers"] == len({e["publisher_entity"] for e in evidence.values()}),
      "IR=%s 实算=%s" % (m["independent_publishers"], len({e["publisher_entity"] for e in evidence.values()})))

# --- 报告 Agent 原始输出：§0.5 权威字段一个都不能出现 ---
AGENT = json.load(open(os.path.join(HERE, "report-ir-v0.1.agent-output.json"), encoding="utf-8"))
AUTHORITATIVE = {"text_claim", "badge", "verdict", "independent_sources", "max_source_tier",
                 "evidence_grade", "citations", "statement_kind", "rumor_text", "correction_text",
                 "verification_state", "metrics", "task"}
found = []


def scan(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in AUTHORITATIVE:
                found.append(path + "/" + k)
            scan(v, path + "/" + k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            scan(v, path + "[%d]" % i)


scan(AGENT["blocks"], "blocks")
check("§0.5 报告 Agent 输出不含任何权威字段", not found, str(found))
check("Agent 输出不产出 metrics/task/证据附录/局限性",
      "metrics" not in AGENT and "task" not in AGENT
      and not [b for b in AGENT["blocks"] if b["type"] in ("evidence_appendix", "limitations", "report_header", "kpi_grid")])

agent_fc = [b for b in AGENT["blocks"] if b["type"] == "fact_check_table"]
ir_fc = by_type.get("fact_check_table", [])
check("回填前后核查表的 claim_ref 序列一致",
      [i["claim_ref"] for i in agent_fc[0]["items"]] == [i["claim_ref"] for i in ir_fc[0]["items"]])
check("Agent 输出的核查表条目只有 claim_ref 一个键",
      all(set(i.keys()) == {"claim_ref"} for i in agent_fc[0]["items"]))

for name, ok, detail in checks:
    print(("  OK   " if ok else "  FAIL ") + name + ((" | " + detail) if detail and not ok else ""))
print("\n%d/%d 通过" % (sum(1 for _, ok, _ in checks if ok), len(checks)))
sys.exit(1 if errors else 0)
