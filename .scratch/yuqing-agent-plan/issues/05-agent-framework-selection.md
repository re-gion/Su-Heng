# 05 多智能体框架选型调研

Type: research
Status: resolved

## Question

本项目需要：论坛式多agent协作、小Loop（计划-搜索-总结-反思）、大Loop（主持人评审-引导-放行）、多agent并行、多LLM接入、流式输出到前端、与FastAPI集成。2026年8月当前，LangGraph、自研Pi式框架、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI、MetaGPT 等选项各自的现状、维护度、学习成本（使用者是入门开发者）如何？BettaFish 用的是什么方案？给出选型建议与理由，特别评估"LangGraph vs 轻量自研"这条分叉。

调研成果写入：`.scratch/yuqing-agent-plan/research/05-agent-framework-selection.md`

## Answer

完整报告：`../research/05-agent-framework-selection.md`（含来源URL与不确定项标注；版本号经PyPI/GitHub API于2026-08-12实测）。要点：

1. **选型倾向：轻量自研（asyncio + AsyncOpenAI工厂 + FastAPI SSE），备选LangGraph 1.x。** 本拓扑规模小且固定（3+1+1 agent、双层循环、一个黑板），核心编排估算200-400行；框架杠杆集中在用不到的能力上，摩擦集中在硬需求（自定义论坛事件协议、逐agent流式、国产多厂商）上。
2. BettaFish实证了自研路线：零框架，openai SDK + base_url接多厂商；但其"论坛"是多进程逼出来的"日志文件+正则tail"黑板，不应继承——单FastAPI进程内存论坛+asyncio并发即可现代化替代。其业务分解（三引擎+主持人+报告引擎）与本需求同构。
3. 多LLM接入已逐家验证：deepseek/kimi/硅基流动/gemini/claude/本地vLLM/中转全部官方支持OpenAI兼容base_url直连，一个AsyncOpenAI工厂全覆盖；litellm非必需（2026-03有过PyPI供应链投毒事件）。openai SDK 3.0.0昨日刚发major，建议锁2.x。
4. LangGraph 1.2.11（1.0已GA，承诺1.x无破坏性变更，39.5k stars）：对"双层循环+黑板"拓扑原生表达最佳（子图共享state、Send并行、defer汇合、Command循环），本地免费Studio，中文资料最丰富——唯一值得认真考虑的框架备选。
5. 判决理由：入门者最大成本是调试；自研只需排查"自己的代码+LLM输出"两层，上框架多出第三层（reducer/superstep/子图命名空间），且并行子图流式事件还需namespace分流再翻译成自家前端协议。与2025-2026工程共识一致（Anthropic《Building Effective Agents》、12-factor agents、Cognition《Don't Build Multi-Agents》）。
6. 预设升级触发条件：需要断点续跑/人工干预持久化、图结构频繁演化、多人协作统一约定时改投LangGraph；自研保持"纯函数节点+provider工厂+单一State模型"三纪律，迁移成本可控在半天级。
7. OpenAI Agents SDK 0.20.0不选：仍0.x且minor即breaking（已20个），无黑板/图原语，官方对复杂编排的答案就是"自己写while+asyncio.gather"。
8. Claude Agent SDK 0.2.136不选作主框架：本质是Claude Code CLI运行时，仅认Anthropic协议端点（openai/gemini不能直连），无编排抽象。
9. CrewAI 1.15.15不选：抽象错位（内循环得下沉Flow手写），token流式是事件旁路、多agent流式区分长期未决——恰撞本项目硬需求。AutoGen 0.7.5排除：官方维护模式，2025-09-30后零发版。AG2 1.0.1观察：新Network/Discussion模型与"论坛"语义贴合度最高，但上线仅两周生态近零。MetaGPT 0.8.2排除：停滞17个月；仅"共享消息池+订阅"思想值得参考。
10. 落地建议：Python 3.11 asyncio + openai SDK 2.x工厂 + 进程内ForumBoard（append-only list+订阅Queue）+ 统一SSE事件协议 + 每轮state落盘；先做1-agent流式竖切再横向复制。
11. 未实测项已在报告§7明示："200-400行"为估算、各框架拓扑结论基于文档核对未编码验证。
