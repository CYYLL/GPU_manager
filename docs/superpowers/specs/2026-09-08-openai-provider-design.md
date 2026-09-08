# OpenAI Provider + 多 Provider 抽象层 — 设计

日期：2026-09-08
状态：已批准（brainstorming 流程）

## 1. 目标

项目当前只支持 Anthropic Messages 协议（[llm_client.py](../../backend/app/agent/llm_client.py) 写死 `anthropic.Anthropic`）。本设计：

1. 新增 **OpenAI provider**（OpenAI 官方 + 通用 OpenAI 兼容端点，如 vLLM / 方舟 `/api/v3`），以 `LLM_BASE_URL` 式可配置 base_url 接入。
2. 引入 **抽象基类** 统一两家共用的参数设定与接口契约，Anthropic / OpenAI 作为两条**可选路径**，由环境变量二选一。
3. **向后完全兼容**：Anthropic 路径与现有 env、行为、测试不变。

## 2. 已确认决策（brainstorming）

| # | 问题 | 结论 |
|---|---|---|
| 1 | OpenAI 覆盖范围 | OpenAI 官方 + 通用 OpenAI 兼容端点（base_url 可配） |
| 2 | 配置方式 | `LLM_PROVIDER` 选择；各 provider 独立变量（anthropic 沿用 `LLM_*`/`ANTHROPIC_*`，openai 用 `OPENAI_*`），互不干扰 |
| 3 | 流式策略 | OpenAI 实现**原生流式**（解析 delta 文本 + tool_calls 增量），可关闭走伪流式降级 |
| 4 | 架构方案 | A：边界转换 —— 内部会话格式保持现状，差异收敛在各 provider 出入转换 |
| 5 | 抽象层 | 用抽象基类统一两家公共参数设定与 `complete/stream_complete` 契约 |

## 3. 架构与文件

```
backend/app/agent/
├── llm_client.py            # 保留：LLMResult；新增：BaseLLMClient、AnthropicClient、兼容别名、工厂
├── openai_client.py         # 新增：OpenAIClient(BaseLLMClient)
```

```text
llm_client.py
  ├─ LLMResult                          # 中立结果 dataclass（text / tool_calls / stop_reason）不变
  ├─ BaseLLMClient(ABC)                 # 抽象基类：两家共用参数设定 + 接口契约
  │     __init__(api_key, base_url, model, streaming,
  │               timeout=60, max_retries=2)
  │        · stream_enabled()  解析 auto/true/false（auto→原生流式）
  │        · _emulated_stream() 伪流式：complete() 一次拿全量再分段 yield（两家共用降级）
  │        · abstract complete(...) -> LLMResult
  │        · abstract stream_complete(...) -> Iterator[dict]
  │
  ├─ AnthropicClient(BaseLLMClient)     # 现有逻辑迁入；读 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL
  │                                     #   (+ LLM_STREAMING, ANTHROPIC_AUTH_TOKEN/BASE_URL/
  │                                     #      DEFAULT_SONNET_MODEL 兜底)；内部 anthropic.Anthropic
  ├─ LLMClient = AnthropicClient        # 兼容别名：现有 import/测试零改动
  └─ create_llm_client()                # 读 LLM_PROVIDER；openai 时惰性 import OpenAIClient

openai_client.py
  └─ OpenAIClient(BaseLLMClient)        # 读 OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL/
                                        #   OPENAI_STREAMING；协议转换全部在内部
```

### 兼容性要点

- `from app.agent.llm_client import LLMClient, LLMResult` 依然成立（`LLMClient = AnthropicClient` 别名）→ `test_llm_client*.py` 不改。
- 实例化点改为工厂：路由 [routers/agent.py:22](../../backend/app/routers/agent.py) `llm_client = create_llm_client()`；清理引擎 [agent/cleanup.py:281](../../backend/app/agent/cleanup.py) 同样。两处测试均为 monkeypatch 模块属性/`run_agent`，不受影响。
- `openai` 依赖仅在 `LLM_PROVIDER=openai` 时惰性 import；不强制所有环境安装。
- 基类参数统一：`api_key / base_url / model / streaming / timeout=60 / max_retries=2` 在 `BaseLLMClient.__init__` 收口为同一组属性，子类复用 `stream_enabled()` / `_emulated_stream()`。

## 4. 配置（env）

| 变量 | 含义 |
|---|---|
| `LLM_PROVIDER` | `anthropic`(默认) \| `openai`；未知值启动即 `ValueError`（fail-fast） |
| `OPENAI_API_KEY` | OpenAI 路径**必填** |
| `OPENAI_BASE_URL` | 可选；默认官方 `https://api.openai.com/v1`；兼容端点填这里 |
| `OPENAI_MODEL` | OpenAI 路径**必填**（无内置默认，防误扣费） |
| `OPENAI_STREAMING` | `auto`(默认)/`true`/`false`，语义同 `LLM_STREAMING` |
| 现有 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_STREAMING` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_DEFAULT_SONNET_MODEL` | **只属于 anthropic 路径**，行为不变 |

- OpenAI 路径**不读** `LLM_*`/`ANTHROPIC_*`，防止误把当前 Ark 的 anthropic key 当 OpenAI key 用。
- `.env.example` 增加 `LLM_PROVIDER` + `OPENAI_*` 段；两套 provider 配置可在 `.env` 共存，切 provider 只改 `LLM_PROVIDER`。

## 5. 协议转换（全部封装在 `OpenAIClient` 内部）

### 出向工具

规范 `{name, description, input_schema}` → OpenAI：
```python
{"type": "function", "function": {"name": ..., "description": ..., "parameters": <input_schema>}}
```
`tools` 为空则不发 `tools`（末轮强制回答路径）。

### 出向消息

历史可能是「普通文本」或「Anthropic block」（后者仅出现在单次请求内 agent_loop 回填的工具反馈）：
- `{role, content: "文本"}` → 直接透传。
- `assistant` + `tool_use` block → assistant 消息 + `tool_calls: [{id, type:"function", function:{name, arguments: json.dumps(input)}}]`。
- `user` + `tool_result` block → `{role:"tool", tool_call_id, content}`。

### 入向结果

OpenAI 响应 → 现有 `LLMResult(text, tool_calls=[{id,name,input}], stop_reason)`：
- `message.content`（str）→ text；`tool_calls[].function.arguments` 为 JSON 字符串 → `json.loads`，失败给 `{}`。
- 文本与工具并存的轮次两者都回传；agent_loop 有工具调用时只把 tool_use 记回历史，叙事文本丢弃 —— 与 Anthropic 现状行为一致。

### 原生流式

`chat.completions.create(..., stream=True)`：
- `content` delta → 实时 yield `{"type":"text","delta"}`。
- `tool_calls` delta 按 `index` 累积：`id` 首批到达、`function.name` 可能被拆分、`arguments` 逐段拼接。
- 流结束时将累积的每条工具调用 yield 一个 `{"type":"tool_use","id","name","input"}` 再返回 —— 与 [agent_loop.py](../../backend/app/agent/agent_loop.py) “先收齐、流结束后才执行”的循环语义兼容。
- `OPENAI_STREAMING=false` 时走基类 `_emulated_stream()`：一次 `complete()` 拿全量再分段 yield。

## 6. 错误处理

- 构造期：OpenAI 路径缺 `OPENAI_API_KEY` 或 `OPENAI_MODEL` → `ValueError`（启动即现形）。
- 运行期 SDK 异常（鉴权/限流/网络/订阅等）**不拦截**，向上抛 → 由路由既有 `except Exception` + 兜底文案 + 已加的 `logger.exception` 统一处理（与 Anthropic 行为一致）。

## 7. 测试

- `test_llm_client*.py`（Anthropic）不动，全量回归。
- 新增 `test_llm_client_openai.py`：
  - 工具/消息转换、`tool_calls` 解析；
  - 缺 env 报错；`create_llm_client()` 按 `LLM_PROVIDER` 选型；
  - 原生流式（分片 delta 组装 tool_calls）、`false` 时伪流式降级；
  - 风格对齐现有测试：monkeypatch `openai.OpenAI` 返回 fake。

## 8. 非目标（YAGNI）

- 不做 Gemini 等第三家 provider（类边界已留好，后续可加）。
- 不做中立会话表示的彻底重构（方案 B 暂缓）。
- 不改前端 / agent_loop / 现有 Anthropic env 语义。
