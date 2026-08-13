#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 05-核心契约 §4.3 的 D1–D11 决策表照抄成断言，穷举全部合法输入组合，
验证两件事：①每组恰好命中一行（互斥且穷举）；②不存在"有反证或内部矛盾却判 verified"的组合。

这不是判定器实现——真正的实现要先做主体立场归并与时序覆盖，这里只验证归并之后那张表本身没有洞。
M0-8 的 tests/unit/test_badge_decision.py 应当把这段扩成参数化用例并接上真实判定器。
用法：python check_badge_decision_table.py
"""
rows = {
 "D2": lambda s,u,c,aS,aU: s>=1 and u>=1,
 "D3": lambda s,u,c,aS,aU: s>=1 and u==0 and c,
 "D4": lambda s,u,c,aS,aU: s==0 and u>=1 and aU>=1,
 "D5": lambda s,u,c,aS,aU: s==0 and u>=2 and aU==0,
 "D6": lambda s,u,c,aS,aU: s==0 and u==1 and aU==0,
 "D7": lambda s,u,c,aS,aU: s>=2 and u==0 and not c,
 "D8": lambda s,u,c,aS,aU: s==1 and u==0 and not c and aS==1,
 "D9": lambda s,u,c,aS,aU: s==0 and u==0 and c,
 "D10":lambda s,u,c,aS,aU: s==1 and u==0 and not c and aS==0,
 "D11":lambda s,u,c,aS,aU: s==0 and u==0 and not c,
}
badge = {"D2":"disputed","D3":"disputed","D4":"refuted","D5":"refuted","D6":"unverified",
         "D7":"verified","D8":"verified","D9":"disputed","D10":"unverified","D11":"unverified"}
combos, bad, green_with_neg = 0, [], []
for s in (0,1,2):
  for u in (0,1,2):
    for aS in range(0, min(s,1)+1):
      for aU in range(0, min(u,1)+1):
        for c in (True, False):
          combos += 1
          hits = [k for k,f in rows.items() if f(s,u,c,aS,aU)]
          if len(hits) != 1: bad.append((s,u,c,aS,aU,hits))
          elif badge[hits[0]]=="verified" and (u>=1 or c):
            green_with_neg.append((s,u,c,aS,aU,hits))
print("合法组合数(含 verification_complete=T/F 两倍):", combos*2)
print("非唯一命中:", bad)
print("有反证/冲突却判绿:", green_with_neg)
