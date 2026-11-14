---
name: cross-language-preparation
label: 跨语言检索准备
description: 保留原主体与行为的检索表达、可回溯的名称对应
version: 1.0.0
roles: ["utility"]
stages: ["search_query_translation","cross_language_subject_relevance"]
output: 保留原主体与行为的检索表达、可回溯的名称对应
---

适用：需要跨语言检索或确认材料中的主体对应。
输入：原主题、目标语言和指定材料。
步骤：保留主体、对象、行为与不确定性；专名保留原名或材料中确有的译名；主体对应须来自给定材料并能逐字定位；检查语种与原有任务范围。
工具：现有检索翻译与主体匹配服务。
产物：符合原接口的检索词或有依据的主体别名。
停止与降级：检索词翻译本身不证明主体对应；相似名字、同国同类机构不能直接放行；无法确认时返回不足。
