# 硅基流动 GLM-5.3-Flash 接入核对

核对日期：2026-10-13。仅查阅硅基流动与 Z.ai 官方公开页面，并检查本仓库调用代码及已安装 SDK 的签名。未读取密钥、Cookie 或浏览器登录态，未调用带鉴权的模型 API，未进行付费生成。

## 结论

`zai-org/GLM-5.3-Flash` 确实出现在硅基流动官方国内模型中心，页面标示 1024K 上下文和工具调用；不是根据相近型号推测出的 ID。[国内模型中心](https://www.siliconflow.cn/models)

本项目已有 OpenAI 兼容的异步客户端、Chat Completions 和 JSON 对象解析路径，基础接入无须更换 SDK。迁移应覆盖实际生效的默认及各角色配置，并单独处理供应商相关的推理参数判断。公开文档核对只证明接入契约有依据，不能证明当前账号有调用权限或真实任务已经可用。

主项目与 `溯衡-Windows试用包-含Key与登录态-20261005` 的真实配置切换待提供新的硅基流动 Key 后完成。现阶段可以先补齐配置读取的一致性、迁移操作与本地验证；不得把旧供应商 Key 配到新地址后声称切换成功。CLI 连通检查应与系统运行时使用同一配置优先级，并在结束时关闭客户端；是否提高最小 probe 的输出预算，应根据该端点的真实返回决定。

## 官方接口核对

| 项目 | 查证结果 | 接入处理 |
| --- | --- | --- |
| 模型 ID | `zai-org/GLM-5.3-Flash`；国内模型中心发布时间为 2026-08-26。[官方模型中心](https://www.siliconflow.cn/models) | 保留大小写与命名空间，不使用 Z.ai 直连 ID `glm-5.3-flash`。 |
| 中国区 Base URL | `https://api.siliconflow.cn/v1`；官方示例使用 OpenAI Python SDK。[快速上手](https://api-docs.siliconflow.cn/docs/userguide/quickstart) | `AsyncOpenAI` 的 `base_url` 使用该地址；使用硅基流动 Key。 |
| 对话接口 | `POST /chat/completions`，Bearer Key 鉴权。[Chat Completions](https://docs.siliconflow.cn/docs/api/chat-completions-post) | 沿用当前 `chat.completions.create()` 调用。 |
| 工具调用 | 国内模型中心标示支持工具调用；平台 Function Calling 指南说明通过 OpenAI 兼容的 `tools` 参数声明函数。[模型中心](https://www.siliconflow.cn/models)、[Function Calling](https://docs.siliconflow.cn/docs/userguide/guides/function-calling) | 模型支持工具调用不等于本项目已用原生函数调用；当前公共网关主要请求 JSON 对象。 |
| JSON 对象输出 | 国际站该模型专页标示 JSON Mode 支持；平台指南用 `response_format={"type":"json_object"}`。[模型专页](https://www.siliconflow.com/models/glm-5-3-flash)、[JSON 模式](https://api-docs.siliconflow.cn/docs/userguide/guides/json-mode) | 保留现有 JSON 模式、提示要求与本地解析校验。 |
| 严格 JSON Schema 输出 | 国际站该模型专页标示 Structured Outputs 不支持；通用 API 有 `json_schema` 参数，但只适用于支持它的模型。[模型专页](https://www.siliconflow.com/models/glm-5-3-flash)、[API 参数](https://docs.siliconflow.cn/docs/api/chat-completions-post) | 不因通用参数存在，就将该模型切到 `json_schema` 或宣称支持严格 Schema。中国区实例未实测。 |
| 上下文与最大输出 | 国内模型中心写 1024K 上下文，未列最大输出；国际站专页写 1049K 上下文、262K Max Tokens。[国内中心](https://www.siliconflow.cn/models)、[国际站专页](https://www.siliconflow.com/models/glm-5-3-flash) | 中国区最大输出的准确数值未独立核实，不把国际站的数值直接当作中国区承诺。 |

Z.ai 的模型文档写 1M 上下文、128K 最大输出，与硅基流动国际站的 262K 标示不同。接入硅基流动时应核对其部署端能力，不能用模型原厂端点的额度覆盖托管端点。[Z.ai 模型文档](https://docs.z.ai/guides/vlm/glm-5.3-flash)

## 推理参数存在供应商差异

硅基流动 Chat Completions 文档提供 `enable_thinking`、`thinking_budget`，后者范围为 128–32768；`reasoning_effort` 当前明确列举的适用模型为 DeepSeek V4 系列及 `Pro/zai-org/GLM-5.2`，可选 `high`、`max`，未列出本次目标模型。[API 参数](https://docs.siliconflow.cn/docs/api/chat-completions-post)

国际站本型号专页把 Reasoning 标为 No；Z.ai 原厂文档则明确写本型号只能开启 `thinking.type=enabled`，并推荐 `reasoning_effort=max`、`temperature=1`、`top_p=0.95`。这些官方页面不能合并成同一个端点的保证。[硅基流动专页](https://www.siliconflow.com/models/glm-5-3-flash)、[Z.ai 文档](https://docs.z.ai/guides/vlm/glm-5.3-flash)

因此，硅基流动中国区该模型的思考开关、推理强度档位及默认行为仍需带鉴权的语义 probe 确认。当前迁移不应擅自发送 Z.ai 的 `thinking` 对象，不应仅凭 `GLM-5.3` 名称复用直连的 `low/high` 参数。未明确支持的参数宜暂不发送；保留失败诊断与正常质量门。

硅基流动推理指南把 `max_tokens` 定义为最终正文长度，把 `thinking_budget` 定义为推理长度，并通过 OpenAI SDK 的 `extra_body` 传非标准参数；指南也提醒部分模型未必会在达到思考预算时立即停止。[推理指南](https://api-docs.siliconflow.cn/docs/userguide/capabilities/reasoning)

## 当前代码与 SDK 的对应关系

核对时的本地实现如下；这是迁移前快照，不代表本次随后修改的最终代码。

- `backend/pyproject.toml` 依赖 `openai>=2,<3`；本机已安装 `openai 2.54.0`。通过本地 `inspect.signature(AsyncCompletions.create)` 确认 `model`、`messages`、`max_tokens`、`response_format`、`reasoning_effort`、`extra_body`、`tools` 参数均存在。这只证明 SDK 可序列化对应参数，不证明供应商支持。
- `backend/yuqing/core/llm/factory.py` 已用 `AsyncOpenAI(api_key=..., base_url=...)`；无须为硅基流动另装 Z.ai SDK。
- `backend/yuqing/core/llm/gateway.py` 发 `response_format={"type":"json_object"}`，读取 `message.content`，保留 JSON 解析与长度截断检查；当前重试输出上限为 32768。没有必要为迁移自动扩大到官方页面的最大窗口。
- `backend/yuqing/core/llm/roles.py` 有 7 个角色，角色环境变量覆盖默认值。配置迁移不能只换默认值后假设所有角色都会继承；还需检查设置数据库的更高优先级配置。
- `backend/yuqing/agents/comment_quick_read.py` 和 `backend/yuqing/services/institution_scope.py` 当前通过 `model.lower().startswith("glm-5.3")` 选择直连参数。准确 ID 含 `zai-org/` 时不会进入这些分支；不能只移除命名空间就直接启用原有推理参数。

上面与官方接口契约的兼容判断，是基于本地代码及官方文档作出的推断；没有真实上游返回作为证据。

## 待完成的在线核验

取得硅基流动中国区 Key 后，可以先检查账号返回的模型列表，再做最小 JSON 语义 probe，最后运行实际调查场景。官方模型列表接口为 `GET https://api.siliconflow.cn/v1/models?sub_type=chat`，需要 Bearer Key。[模型列表 API](https://api-docs.siliconflow.cn/docs/api/models-get)

仍未核实：该账号权限与余额、目标模型当前限流、真实 JSON 完整率与延迟、中国区最大输出、思考参数、推理 Token 计量字段、完整任务结果与核验质量。不能用官方网页、SDK 签名检查或一次连通成功替代这些验证。

## 本轮已完成的修改与本地验证

- `.env.example` 的示例地址与型号改为硅基流动；已有部署的 `.env` 不会因此自动切换。
- 修正 `llm_ping`：读取系统设置数据库与环境变量的有效配置，支持 `--data-dir`，成功或失败时关闭模型客户端与数据库，输出实际使用的模型名。
- 新增 8 项模拟 HTTP 回归测试：七个角色均使用数据库保存的硅基流动地址、密钥与准确模型 ID，继续发送 `json_object`，不附加供应商未明确支持的思考参数；另验证独立角色覆盖和鉴权失败后的资源关闭。没有真实上游调用。
- 全量后端测试 612 项、前端测试 30 项通过；产品源码与测试的 Ruff 检查/格式检查、依赖分层检查、前端类型检查与构建通过。按 `ruff check .` / `ruff format --check .` 扫全目录时，历史忽略目录 `backend/tmp-runtime/source-recovery-20261004/` 的临时脚本有既存 lint/格式问题，本次没有修改它们。
- Windows 试用包仅同步诊断命令、配置模板、切换说明与校验清单；257 个文件的大小/SHA256 已核对，原 `.env`、数据库与浏览器登录资料共 116 个文件保持原哈希。2026-10-05 的在线验收记录保留为历史记录，不用于证明新供应商可用。

以上代码与切换准备已可使用，主项目与试用包的真实默认配置仍保留原供应商。待新的硅基流动 Key 后才能完成实际配置切换与在线语义检查；当前没有运行新的真实调查。
