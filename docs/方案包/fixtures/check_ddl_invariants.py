#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 02-系统架构设计.md §6 的建表 SQL 原样抽出来，在 SQLite 内存库里执行，
再逐条验证 05-核心契约 §0.3 的写入期不变量确实由 DDL 兜住了。

复审 P0-4 的原话是"三种非法记录均被接受"——这个脚本就是那条结论的对照实验。
用法：python check_ddl_invariants.py（从仓库根目录跑）
"""
import re, sqlite3, sys, io, os
root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
src = io.open(os.path.join(root, "docs", "方案包", "02-系统架构设计.md"), encoding="utf-8").read()
# 抽取 §6 的 SQL 代码块
m = re.search(r"```sql\n(.*?)```", src, re.S)
sql = m.group(1)
con = sqlite3.connect(":memory:")
con.execute("PRAGMA foreign_keys=ON")
con.executescript(sql)
print("DDL 执行: OK, 表数 =", len(con.execute("select name from sqlite_master where type='table'").fetchall()))

def must_fail(label, stmts):
    try:
        for s, p in stmts: con.execute(s, p)
        con.rollback(); print("  !! 未被拒绝:", label); return False
    except sqlite3.IntegrityError as e:
        con.rollback(); print("  被拒绝 OK:", label, "|", str(e)[:60]); return True

def must_pass(label, stmts):
    try:
        for s, p in stmts: con.execute(s, p)
        con.rollback(); print("  接受 OK:", label); return True
    except Exception as e:
        con.rollback(); print("  !! 合法数据被拒:", label, "|", e); return False

T = ("INSERT INTO task(id,event_query,status,created_at,updated_at) VALUES(?,?,?,?,?)",
     ("t1","e","running","2026-08-12","2026-08-12"))
EV = "INSERT INTO evidence(pk,task_id,local_id,url,url_hash,title,source_domain,source_tier,discovered_at,fetch_status,fetched_at,snippet,content_text,snapshot_path,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
CL = "INSERT INTO claim(pk,task_id,local_id,text,statement_kind,rumor_text,correction_text,agent,round,is_key,is_key_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
CE = "INSERT INTO claim_evidence(claim_pk,evidence_pk,quote,quote_type,quote_start,quote_end,quote_verified) VALUES(?,?,?,?,?,?,?)"

ok = []
ok.append(must_fail("fetched 但正文/快照/hash 全空",
   [T, (EV, ("e1","t1","E001","u","h","t","d.com",2,"2026-08-12","fetched",None,"s",None,None,None))]))
ok.append(must_fail("discovered 但无 snippet",
   [T, (EV, ("e2","t1","E002","u2","h2","t","d.com",2,"2026-08-12","discovered",None,None,None,None,None))]))
ok.append(must_fail("fact 却带 correction_text",
   [T, (CL, ("c1","t1","C001","x","fact",None,"更正","a",1,1,None,"2026-08-12"))]))
ok.append(must_fail("rumor 却带 rumor_text",
   [T, (CL, ("c2","t1","C002","x","rumor","传言",None,"a",1,1,None,"2026-08-12"))]))
ok.append(must_fail("is_key=0 但无理由",
   [T, (CL, ("c3","t1","C003","x","fact",None,None,"a",1,0,None,"2026-08-12"))]))
ok.append(must_fail("verbatim 无偏移且未校验",
   [T, (EV, ("e3","t1","E003","u3","h3","t","d.com",2,"2026-08-12","fetched","2026-08-12","s","正文","p","sha")),
       (CL, ("c4","t1","C004","x","fact",None,None,"a",1,1,None,"2026-08-12")),
       (CE, ("c4","e3","q","verbatim",None,None,0))]))
ok.append(must_fail("非法 event_type",
   [T, ("INSERT INTO event_log(task_id,seq,event_type,ts,payload) VALUES('t1',1,'heartbeat','x','{}')", ())]))
ok.append(must_fail("非法 task_state.status",
   [T, ("INSERT INTO task_state(task_id,step_key,kind,status,created_at,updated_at) VALUES('t1','k','search','setled','x','x')", ())]))
ok.append(must_pass("合法 fetched + verbatim 引用",
   [T, (EV, ("e4","t1","E004","u4","h4","t","d.com",2,"2026-08-12","fetched","2026-08-12","s","正文","p","sha")),
       (CL, ("c5","t1","C005","x","fact","流传说法",None,"a",1,1,None,"2026-08-12")),
       (CE, ("c5","e4","正文","verbatim",0,2,1))]))
ok.append(must_pass("合法 discovered + snippet 引用",
   [T, (EV, ("e5","t1","E005","u5","h5","t","d.com",3,"2026-08-12","discovered",None,"摘要",None,None,None)),
       (CL, ("c6","t1","C006","x","rumor",None,"真实情况","a",1,0,"辅助陈述","2026-08-12")),
       (CE, ("c6","e5","摘要","snippet",None,None,0))]))
print("\n全部通过:", all(ok))
sys.exit(0 if all(ok) else 1)
