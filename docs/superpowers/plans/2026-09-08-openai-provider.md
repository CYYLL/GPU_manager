# OpenAI Provider + 多 Provider 抽象层 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Agent LLM 客户端支持第二个 provider —— OpenAI（官方 + 通用兼容端点），并在一个抽象基类下统一 Anthropic / OpenAI 两家的参数设定与 `complete/stream_complete` 契约。

**Architecture:** 把现有 `LLMClient`（写死 `anthropic.Anthropic`）重构为三件套：`BaseLLMClient(ABC)`（持有两家共用的 `api_key/base_url/model/streaming/timeout/max_retries` + `stream_enabled()` + `_emulated_stream()` 降级）、`AnthropicClient(BaseLLMClient)`（原逻辑迁入，`LLMClient = AnthropicClient` 兼容别名）、`OpenAIClient(BaseLLMClient)`（`backend/app/agent/openai_client.py`，所有协议转换封装在内部）。`create_llm_client()` 工厂按 `LLM_PROVIDER`（默认 `anthropic`）二选一，`openai` 分支惰性 import 依赖。会话内部格式（Anthropic block 风格）与 `agent_loop.py` **完全不动**，转换只发生在 OpenAI 边界。

**Tech Stack:** Python 3.8 / FastAPI；`anthropic==0.72.0`（已装，SDK `__init__` 支持 `timeout`/`max_retries`）；新增 `openai>=1.0.0`（惰性依赖）；`pytest`（在 `backend/` 下以 `/opt/anaconda3/bin/python -m pytest` 运行）。

## Global Constraints

- **后端 Python 3.8.5**，一律用 `/opt/anaconda3/bin/python` 运行 pytest。
- **内部中立格式不改**：messages 为 `{role, content}`（content 可为 str 或 Anthropic block dict 列表）；`LLMResult(text, tool_calls=[{id,name,input}], stop_reason)`；流事件 `{"type":"text","delta"}` / `{"type":"tool_use","id","name","input"}`。`backend/app/agent/agent_loop.py` **不修改**。
- **Anthropic 路径向后完全兼容**：env `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`/`LLM_STREAMING` + `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL`/`ANTHROPIC_DEFAULT_SONNET_MODEL` 语义不变；`from app.agent.llm_client import LLMClient` 依然可用（`LLMClient` 是别名）；`test_llm_client.py` / `test_llm_client_stream.py` **不改**、全量回归。
- **OpenAI 路径不读任何 `LLM_*`/`ANTHROPIC_*`**（防止把 Ark 的 anthropic key 误当 OpenAI key）。
- `openai` 只在 `LLM_PROVIDER=openai` 时于 `OpenAIClient.__init__` 内 `from openai import OpenAI` 惰性导入；不强制所有环境安装。`requirements.txt` 追加 `openai>=1.0.0` 并安装。
- **fail-fast**：`LLM_PROVIDER` 未知值 → 构造即 `ValueError`；OpenAI 路径缺 `OPENAI_API_KEY` 或 `OPENAI_MODEL` → 构造即 `ValueError`。
- 外部 LLM 调用从不发生在 DB 事务内（沿用现有约定）；错误不拦截，向上抛，由既有路由 `except Exception` + `logger.exception` 兜底。
- 前端 / `.env`（真实 secrets）不修改；只改 `.env.example` 模板。
- 每个 task 结束时 commit。

---

### Task 1: BaseLLMClient 抽象 + AnthropicClient 重构（行为不变）

把 `llm_client.py` 拆成 `BaseLLMClient(ABC)` + `AnthropicClient(BaseLLMClient)`，保留 `LLMResult`，末尾给兼容别名 `LLMClient = AnthropicClient`。native 流式/伪流式逻辑从旧 `LLMClient` 原样迁入（伪流式改用基类 `_emulated_stream()`，行为等价）。

**Files:**
- Modify: `backend/app/agent/llm_client.py`（整体重写，完整内容见 Step 3）
- Test: `backend/tests/test_llm_client_base.py`（新建，Task 2 也往里加测试）

**Interfaces:**
- Consumes: 现有 `LLMResult`；`anthropic` SDK；env 读取规则（与现 `LLMClient.__init__` 相同）。
- Produces: `BaseLLMClient(ABC)`，具象方法 `__init__(api_key, base_url, model, streaming="auto", timeout=60.0, max_retries=2)`、`stream_enabled() -> bool`、`_emulated_stream(system, messages, tools=None, max_tokens=1024)`，抽象方法 `complete(...) -> LLMResult`、`stream_complete(...) -> Iterator[dict]`。`AnthropicClient(BaseLLMClient)`。`LLMClient = AnthropicClient`（别名）。Task 2 的 `create_llm_client` 与既有测试依赖这些名字。

- [ ] **Step 1: 写失败契约测试**

创建 `backend/tests/test_llm_client_base.py`：

```python
"""BaseLLMClient contract: shared abstraction, back-compat alias (provider-agnostic)."""
import pytest

from app.agent.llm_client import (
    BaseLLMClient, AnthropicClient, LLMClient, LLMResult,
)


def test_llmclient_is_anthropic_alias():
    assert LLMClient is AnthropicClient
    assert issubclass(AnthropicClient, BaseLLMClient)
    assert issubclass(BaseLLMClient, object)
    # the dataclass is untouched by the refactor
    r = LLMResult(text="t", tool_calls=[{"id": "a", "name": "x", "input": {}}])
    assert r.text == "t" and r.stop_reason is None


def test_stream_disabled_shares_emulated_fallback(monkeypatch):
    # _emulated_stream lives on the base class and is reachable via the alias.
    monkeypatch.setattr(LLMClient, "__init__", lambda self: None)
    monkeypatch.setattr(LLMClient, "stream_enabled", lambda self: False)
    c = LLMClient()
    c.complete = lambda *a, **k: LLMResult(text="整段", tool_calls=[])
    events = list(c.stream_complete("s", [{"role": "user", "content": "hi"}]))
    assert {"type": "text", "delta": "整段"} in events
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest tests/test_llm_client_base.py -v`

Expected: FAIL 且报 `ImportError: cannot import name 'AnthropicClient'` / `'BaseLLMClient'`。

- [ ] **Step 3: 重写 `backend/app/agent/llm_client.py`**

完整新内容（原子覆盖整个文件）：

```python
"""Configurable LLM clients behind one abstraction (Anthropic default, OpenAI optional).

Provider chosen by LLM_PROVIDER:
  - anthropic (default) -> AnthropicClient (back-compat alias LLMClient): env LLM_*/ANTHROPIC_*
  - openai            -> OpenAIClient (openai_client.py): env OPENAI_*
Env precedence (anthropic path): LLM_BASE_URL/LLM_API_KEY/LLM_MODEL override ANTHROPIC_*.
BaseLLMClient owns the shared params (api_key/base_url/model/streaming/timeout/
max_retries) and the common emulated-stream fallback; each subclass implements
complete()/stream_complete() for its own protocol and returns the neutral shapes
agent_loop consumes. External calls never run inside a DB transaction.
"""
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, List, Dict, Optional

import anthropic


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: List[Dict] = field(default_factory=list)  # [{id,name,input}]
    stop_reason: Optional[str] = None


class BaseLLMClient(ABC):
    """Provider-agnostic parameter set + streaming contract.

    Subclasses resolve their own env vars, then call
    super().__init__(api_key, base_url, model, streaming, timeout, max_retries)
    and build their SDK client.
    """

    def __init__(self, api_key: Optional[str], base_url: Optional[str],
                 model: Optional[str], streaming: str = "auto",
                 timeout: float = 60.0, max_retries: int = 2):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._stream = (streaming or "auto").lower()
        self.timeout = timeout
        self.max_retries = max_retries

    def stream_enabled(self) -> bool:
        """auto → True (native streaming); true/false override."""
        if self._stream == "true":
            return True
        if self._stream == "false":
            return False
        return True

    def _emulated_stream(self, system: str, messages: List[Dict],
                         tools: Optional[List[Dict]] = None, max_tokens: int = 1024):
        """Stream-off fallback shared by all providers: one complete() call,
        replayed as a single text delta plus one tool_use per call."""
        result = self.complete(system, messages, tools, max_tokens=max_tokens)
        if result.text:
            yield {"type": "text", "delta": result.text}
        for tc in result.tool_calls:
            yield {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}

    @abstractmethod
    def complete(self, system: str, messages: List[Dict],
                 tools: Optional[List[Dict]] = None, max_tokens: int = 1024) -> LLMResult:
        """One non-streaming turn -> LLMResult."""

    @abstractmethod
    def stream_complete(self, system: str, messages: List[Dict],
                        tools: Optional[List[Dict]] = None,
                        max_tokens: int = 1024) -> Iterator[dict]:
        """Yield events: {'type':'text','delta'} / {'type':'tool_use','id','name','input'}."""


class AnthropicClient(BaseLLMClient):
    """Anthropic Messages API (also any Anthropic-compatible gateway, e.g. Ark Coding Plan)."""

    def __init__(self):
        super().__init__(
            api_key=os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            base_url=os.environ.get("LLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL"),
            model=(os.environ.get("LLM_MODEL")
                   or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                   or "claude-sonnet-4-6"),
            streaming=os.environ.get("LLM_STREAMING", "auto"),
        )
        self.client = anthropic.Anthropic(
            api_key=self.api_key, base_url=self.base_url,
            timeout=self.timeout, max_retries=self.max_retries)

    def complete(self, system, messages, tools=None, max_tokens=1024) -> LLMResult:
        kwargs = dict(model=self.model, max_tokens=max_tokens,
                      system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        resp = self.client.messages.create(**kwargs)
        text_parts, tool_calls = [], []
        for block in resp.content:
            if getattr(block, "type", "") == "text":
                text_parts.append(block.text)
            elif getattr(block, "type", "") == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
        return LLMResult(text="".join(text_parts), tool_calls=tool_calls,
                         stop_reason=getattr(resp, "stop_reason", None))

    def stream_complete(self, system, messages, tools=None, max_tokens=1024):
        if not self.stream_enabled():
            yield from self._emulated_stream(system, messages, tools, max_tokens=max_tokens)
            return
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        with self.client.messages.stream(**kwargs) as stream:
            tool_acc = None  # {'id','name','input_json'} accumulating partial_json
            for event in stream:
                et = getattr(event, "type", "")
                if et == "content_block_start":
                    block = getattr(event, "content_block", None) or getattr(event, "block", None)
                    if getattr(block, "type", "") == "tool_use":
                        tool_acc = {"id": block.id, "name": block.name, "input_json": ""}
                elif et == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dt = getattr(delta, "type", "")
                    if dt == "text_delta":
                        yield {"type": "text", "delta": getattr(delta, "text", "")}
                    elif dt == "input_json_delta" and tool_acc is not None:
                        tool_acc["input_json"] += getattr(delta, "partial_json", "")
                elif et == "content_block_stop" and tool_acc is not None:
                    try:
                        inp = json.loads(tool_acc["input_json"] or "{}")
                    except Exception:
                        inp = {}
                    yield {"type": "tool_use", "id": tool_acc["id"],
                           "name": tool_acc["name"], "input": inp}
                    tool_acc = None


# Back-compat alias: existing imports/tests use LLMClient for the anthropic client.
LLMClient = AnthropicClient
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest tests/test_llm_client_base.py tests/test_llm_client.py tests/test_llm_client_stream.py -v`

Expected: 全部 PASS（既有 `test_llm_client.py` / `test_llm_client_stream.py` 因别名而原样通过）。

- [ ] **Step 5: Commit**

```bash
cd /amax/gpu_manager_v2 && git add backend/app/agent/llm_client.py backend/tests/test_llm_client_base.py && git commit -m "refactor(llm): BaseLLMClient abstraction + AnthropicClient, keep LLMClient alias"
```

---

### Task 2: `create_llm_client()` 工厂 + 接线两处实例化点

新增工厂读取 `LLM_PROVIDER`（默认 `anthropic`；`openai` 惰性 import；未知值 `ValueError`），把路由与清理引擎两处 `LLMClient()` 直构改为走工厂。二者测试均为 monkeypatch 模块属性 / 注入参数，不感知工厂。

**Files:**
- Modify: `backend/app/agent/llm_client.py`（末尾追加工厂）
- Modify: `backend/app/routers/agent.py:14` 与 `:22`
- Modify: `backend/app/agent/cleanup.py:277-282`（`_get_llm`）
- Test: `backend/tests/test_llm_client_base.py`（追加 3 个测试）

**Interfaces:**
- Consumes: Task 1 的 `BaseLLMClient` / `AnthropicClient` / `LLMClient` 别名。
- Produces: `create_llm_client() -> BaseLLMClient`（读 `LLM_PROVIDER`，默认 `"anthropic"`）。Task 3+ 的 `OpenAIClient` 由工厂 `openai` 分支惰性返回。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_llm_client_base.py` 末尾：

```python
def test_create_llm_client_defaults_to_anthropic(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    client = create_llm_client()
    assert isinstance(client, AnthropicClient)
    assert isinstance(client, LLMClient)


def test_create_llm_client_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(ValueError, match="anthropic.*openai"):
        create_llm_client()
```

并同步 import 行改为：

```python
from app.agent.llm_client import (
    BaseLLMClient, AnthropicClient, LLMClient, LLMResult, create_llm_client,
)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest tests/test_llm_client_base.py -v`

Expected: FAIL，报 `ImportError: cannot import name 'create_llm_client'`。

- [ ] **Step 3: 实现工厂**

在 `backend/app/agent/llm_client.py` 文件末尾（`LLMClient = AnthropicClient` 之后）追加：

```python
def create_llm_client() -> BaseLLMClient:
    """Build the client selected by LLM_PROVIDER (default 'anthropic').

    Unknown values fail fast at construction time. The openai module is imported
    lazily so its dependency is only required when that provider is selected.
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicClient()
    if provider == "openai":
        from .openai_client import OpenAIClient  # lazy import
        return OpenAIClient()
    raise ValueError("LLM_PROVIDER must be 'anthropic' or 'openai', got %r" % provider)
```

- [ ] **Step 4: 接线两处实例化点**

`backend/app/routers/agent.py` —— 第 14 行 import 改为：

```python
from ..agent.llm_client import create_llm_client
```

第 22 行改为：

```python
llm_client = create_llm_client()
```

`backend/app/agent/cleanup.py` —— `_get_llm()`（原 279-281 行）改为：

```python
    if llm_client is None:
        from .llm_client import create_llm_client
        llm_client = create_llm_client()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest tests/test_llm_client_base.py tests/test_agent_route.py tests/test_agent_stream_route.py tests/test_chat_control_e2e.py tests/test_cleanup_control.py -v`

Expected: 全部 PASS（agent 路由 / 清理测试 monkeypatch 的是 `agent_router.llm_client` 与注入的 `llm_client`，工厂换构造不影响）。

- [ ] **Step 6: Commit**

```bash
cd /amax/gpu_manager_v2 && git add backend/app/agent/llm_client.py backend/app/routers/agent.py backend/app/agent/cleanup.py backend/tests/test_llm_client_base.py && git commit -m "feat(llm): create_llm_client factory by LLM_PROVIDER; wire router + cleanup"
```

---

### Task 3: 追加并安装 `openai` 依赖

**Files:**
- Modify: `backend/requirements.txt`（末尾追加一行）
- （无 Python 测试 —— 纯依赖/基建步骤，验证方式是 import 冒烟）

**Interfaces:**
- Consumes: 无。
- Produces: 环境里可 `from openai import OpenAI`；Task 4/5 的测试与实现依赖它。

- [ ] **Step 1: requirements.txt 追加依赖**

在 `backend/requirements.txt` 的 `anthropic>=0.40.0` 行后追加：

```
openai>=1.0.0
```

- [ ] **Step 2: 安装**

Run: `/opt/anaconda3/bin/python -m pip install "openai>=1.0.0"`

Expected: 输出 `Successfully installed openai-...`。

- [ ] **Step 3: import 冒烟验证**

Run: `/opt/anaconda3/bin/python -c "from openai import OpenAI; print(OpenAI)"`

Expected: 打印 `<class 'openai.OpenAI'>`（无 traceback）。

- [ ] **Step 4: Commit**

```bash
cd /amax/gpu_manager_v2 && git add backend/requirements.txt && git commit -m "chore(deps): add openai>=1.0.0 (lazy, provider-selected)"
```

---

### Task 4: OpenAIClient —— env 解析 + 出/入向转换 + `complete()`

新建 `backend/app/agent/openai_client.py`，实现 OpenAI Chat Completions 的非流式完整路径：tools 包装、Anthropic block 消息拆解为 `assistant(tool_calls)` / `tool` 消息、响应解析成 `LLMResult`。`stream_complete()` 本任务先做成仅伪流式（走基类 `_emulated_stream()`），保证可实例化（基类抽象要求实现它）；native 流式留到 Task 5 替换。本任务同时包含 `create_llm_client` 对 `openai` 分支的选型测试。

**Files:**
- Create: `backend/app/agent/openai_client.py`
- Test: `backend/tests/test_llm_client_openai.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `BaseLLMClient` / `LLMResult`；`_require()` 读取的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_STREAMING`；agent_loop 的 Anthropic block dict 形状（`tool_use` 的 `id/name/input`，`tool_result` 的 `tool_use_id/content`）。
- Produces: `OpenAIClient(BaseLLMClient)`（`complete()` 完整可用，`stream_complete()` 伪流式占位）；静态方法 `_to_openai_tools(tools)`、`_to_openai_messages(messages)`、`_parse_choice(choice)`。Task 5 只替换 `stream_complete` 为 native。Task 2 的工厂 `openai` 分支返回它。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_llm_client_openai.py`：

```python
"""OpenAIClient: env resolution, Anthropic<->OpenAI conversion, complete()."""
import json
from unittest import mock

import pytest

from app.agent.llm_client import LLMResult, create_llm_client
from app.agent.openai_client import OpenAIClient, DEFAULT_OPENAI_BASE_URL


def _set_env(monkeypatch, **over):
    base = {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "m",
            "OPENAI_BASE_URL": "http://openai.local/v1", "OPENAI_STREAMING": "auto"}
    base.update(over)
    for key, value in base.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _mk_openai(monkeypatch, create_retval=None):
    sdk = mock.Mock()
    if create_retval is not None:
        sdk.chat.completions.create.return_value = create_retval
    monkeypatch.setattr("openai.OpenAI", lambda **kw: sdk)
    return sdk


def _choice(content, tool_calls=None, finish_reason="stop"):
    tc_mocks = []
    for tc in (tool_calls or []):
        t = mock.Mock()
        t.id = tc["id"]
        t.function = mock.Mock()
        t.function.name = tc["name"]
        t.function.arguments = tc["arguments"]
        tc_mocks.append(t)
    msg = mock.Mock()
    msg.content = content
    msg.tool_calls = tc_mocks
    choice = mock.Mock()
    choice.message = msg
    choice.finish_reason = finish_reason
    return choice


def _response(*choices):
    resp = mock.Mock()
    resp.choices = list(choices)
    return resp


def test_openai_requires_key_and_model(monkeypatch):
    _set_env(monkeypatch, OPENAI_API_KEY=None)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIClient()
    _set_env(monkeypatch)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        OpenAIClient()


def test_openai_ignores_llm_env_and_defaults_base_url(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "anthropic-key")  # must NOT satisfy openai
    monkeypatch.setenv("LLM_MODEL", "anthropic-model")
    _set_env(monkeypatch, OPENAI_BASE_URL=None, OPENAI_API_KEY="openai-key",
             OPENAI_MODEL="openai-model")
    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    client = OpenAIClient()
    assert client.api_key == "openai-key"
    assert client.model == "openai-model"
    assert captured["base_url"] == DEFAULT_OPENAI_BASE_URL
    assert captured["api_key"] == "openai-key"
    assert captured["timeout"] == 60.0 and captured["max_retries"] == 2


def test_complete_converts_tools_and_messages_and_parses(monkeypatch):
    _set_env(monkeypatch)
    sdk = _mk_openai(monkeypatch, create_retval=_response(
        _choice("好的", tool_calls=[{"id": "c1", "name": "get_gpu_status",
                                    "arguments": '{"a": 1}'}],
                finish_reason="tool_calls")))

    client = OpenAIClient()
    result = client.complete(
        "你是助手",
        [{"role": "user", "content": "hi"}],
        [{"name": "get_gpu_status", "description": "查卡",
          "input_schema": {"type": "object", "properties": {}}}])

    called = sdk.chat.completions.create.call_args.kwargs
    assert called["model"] == "m"
    assert called["messages"][0] == {"role": "system", "content": "你是助手"}
    assert called["messages"][1:] == [{"role": "user", "content": "hi"}]
    assert called["tools"] == [{"type": "function", "function": {
        "name": "get_gpu_status", "description": "查卡",
        "parameters": {"type": "object", "properties": {}}}}]
    assert result.text == "好的"
    assert result.tool_calls == [{"id": "c1", "name": "get_gpu_status", "input": {"a": 1}}]
    assert result.stop_reason == "tool_calls"


def test_to_openai_messages_splits_tool_blocks():
    out = OpenAIClient._to_openai_messages([
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "get_gpu_status",
             "input": {"all": True}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "4 卡空闲"}]},
        {"role": "user", "content": "好"},
    ])
    assert out == [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "get_gpu_status",
                                      "arguments": json.dumps({"all": True},
                                                              ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "4 卡空闲"},
        {"role": "user", "content": "好"},
    ]


def test_complete_bad_tool_json_defaults_to_empty_input(monkeypatch):
    _set_env(monkeypatch)
    _mk_openai(monkeypatch, create_retval=_response(
        _choice("", tool_calls=[{"id": "x", "name": "f", "arguments": "not-json{"}])))
    result = OpenAIClient().complete("s", [{"role": "user", "content": "hi"}])
    assert result.tool_calls == [{"id": "x", "name": "f", "input": {}}]


def test_no_tools_arg_when_none(monkeypatch):
    _set_env(monkeypatch)
    sdk = _mk_openai(monkeypatch, create_retval=_response(_choice("无工具")))
    OpenAIClient().complete("s", [{"role": "user", "content": "hi"}], tools=None)
    called = sdk.chat.completions.create.call_args.kwargs
    assert "tools" not in called


def test_create_llm_client_selects_openai(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    _mk_openai(monkeypatch)  # no network at construction
    client = create_llm_client()
    assert isinstance(client, OpenAIClient)
```

> 注：`test_create_llm_client_selects_openai` 会一直失败到本任务 Step 3 建好 `openai_client.py`，正好作为本任务实现完成的标准。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest tests/test_llm_client_openai.py -v`

Expected: FAIL，报 `ModuleNotFoundError: No module named 'app.agent.openai_client'`。

- [ ] **Step 3: 实现 `OpenAIClient`（含伪流式占位）**

创建 `backend/app/agent/openai_client.py`，完整内容：

```python
"""OpenAI Chat Completions client (official OpenAI + OpenAI-compatible endpoints).

Env (read only when LLM_PROVIDER=openai):
  OPENAI_API_KEY   required
  OPENAI_BASE_URL  optional, default https://api.openai.com/v1; put a compat
                   endpoint here (e.g. Ark /api/v3 or a vLLM /v1 server)
  OPENAI_MODEL     required (no built-in default: prevents accidental billing)
  OPENAI_STREAMING auto|true|false (same semantics as LLM_STREAMING)

All protocol conversion lives here (boundary A): the agent loop still speaks
Anthropic-style messages; this class translates to/from OpenAI Chat Completions
and exposes the neutral complete()/stream_complete() shapes. This module never
reads LLM_*/ANTHROPIC_* env vars, so an anthropic gateway key cannot leak in.
"""
import json
import os
from typing import Iterator, List, Dict, Optional

from .llm_client import BaseLLMClient, LLMResult

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def _require(env_var: str) -> str:
    value = os.environ.get(env_var, "").strip()
    if not value:
        raise ValueError("%s must be set when LLM_PROVIDER=openai" % env_var)
    return value


class OpenAIClient(BaseLLMClient):
    def __init__(self):
        super().__init__(
            api_key=_require("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL", "").strip() or DEFAULT_OPENAI_BASE_URL,
            model=_require("OPENAI_MODEL"),
            streaming=os.environ.get("OPENAI_STREAMING", "auto"),
        )
        from openai import OpenAI  # lazy: only when this provider is chosen
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                             timeout=self.timeout, max_retries=self.max_retries)

    # ── outbound: tools & messages ─────────────────────────────────
    @staticmethod
    def _to_openai_tools(tools: Optional[List[Dict]]) -> Optional[List[Dict]]:
        """[{name,description,input_schema}] -> OpenAI function tools; None when empty."""
        if not tools:
            return None
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t.get("input_schema", {"type": "object"})}}
                for t in tools]

    @staticmethod
    def _to_openai_messages(messages: List[Dict]) -> List[Dict]:
        """Translate agent messages to OpenAI chat format.

        Plain {role, content: str} pass through. Anthropic content-block turns
        (built by agent_loop only, as tool feedback) are split: assistant
        tool_use -> assistant message with tool_calls; user tool_result ->
        role:"tool" messages keyed by tool_call_id.
        """
        out: List[Dict] = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text_parts: List[str] = []
                tool_calls: List[Dict] = []
                for block in content:
                    btype = block.get("type") if isinstance(block, dict) else ""
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block["id"], "type": "function",
                            "function": {"name": block["name"],
                                         "arguments": json.dumps(block.get("input", {}),
                                                                 ensure_ascii=False)}})
                assistant = {"role": "assistant", "content": "".join(text_parts) or ""}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                out.append(assistant)
            elif role == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out.append({"role": "tool",
                                    "tool_call_id": block["tool_use_id"],
                                    "content": str(block.get("content", ""))})
            else:
                out.append({"role": role, "content": str(content)})
        return out

    # ── inbound ────────────────────────────────────────────────────
    @staticmethod
    def _parse_choice(choice) -> LLMResult:
        """One chat completion choice -> neutral LLMResult."""
        message = getattr(choice, "message", None)
        text_parts: List[str] = []
        if message is not None:
            body = getattr(message, "content", None)
            if isinstance(body, str):
                text_parts.append(body)
            elif body:  # a few compat endpoints return content segments as a list
                for part in body:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif getattr(part, "type", "") == "text":
                        text_parts.append(getattr(part, "text", "") or "")
        tool_calls: List[Dict] = []
        if message is not None:
            for tc in (getattr(message, "tool_calls", None) or []):
                fn = getattr(tc, "function", None)
                raw = getattr(fn, "arguments", "") or ""
                try:
                    inp = json.loads(raw) if raw else {}
                except Exception:
                    inp = {}
                tool_calls.append({"id": getattr(tc, "id", None),
                                   "name": getattr(fn, "name", "") or "",
                                   "input": inp})
        return LLMResult(text="".join(text_parts), tool_calls=tool_calls,
                         stop_reason=getattr(choice, "finish_reason", None))

    # ── neutral interface ──────────────────────────────────────────
    def complete(self, system, messages, tools=None, max_tokens=1024) -> LLMResult:
        kwargs = dict(model=self.model, max_tokens=max_tokens,
                      messages=[{"role": "system", "content": system}]
                      + self._to_openai_messages(messages))
        oa_tools = self._to_openai_tools(tools)
        if oa_tools:
            kwargs["tools"] = oa_tools
        resp = self.client.chat.completions.create(**kwargs)
        return self._parse_choice(resp.choices[0])

    def stream_complete(self, system, messages, tools=None, max_tokens=1024) -> Iterator[dict]:
        """Native streaming lands in Task 5; for now share the emulated fallback."""
        yield from self._emulated_stream(system, messages, tools, max_tokens=max_tokens)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest tests/test_llm_client_openai.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
cd /amax/gpu_manager_v2 && git add backend/app/agent/openai_client.py backend/tests/test_llm_client_openai.py && git commit -m "feat(llm): OpenAIClient conversions + complete (env-gated, emulated stream)"
```

---

### Task 5: OpenAIClient 原生流式（delta 文本 + tool_calls 增量组装）

把 Task 4 里 `stream_complete()` 的伪流式占位替换为 OpenAI native SSE 解析：`content` delta 实时 yield 文本事件；`tool_calls` 增量按 `index` 累积 `id`/`name`/`arguments`，流结束按序 yield 每个 `tool_use` 事件。`OPENAI_STREAMING=false`（`stream_enabled()` False）时仍走基类 `_emulated_stream()`。

**Files:**
- Modify: `backend/app/agent/openai_client.py`（仅替换 `stream_complete` 方法体）
- Test: `backend/tests/test_llm_client_openai.py`（末尾追加）

**Interfaces:**
- Consumes: Task 4 的 `OpenAIClient`、`_to_openai_messages`、`_to_openai_tools`、`_emulated_stream`（继承）、`stream_enabled()`。
- Produces: 完整 `stream_complete()`，事件形状与 Anthropic 一致（`{'type':'text','delta'}` / `{'type':'tool_use','id','name','input'}`），`agent_loop.run_agent_stream` 无需改动。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_llm_client_openai.py` 末尾追加：

```python
# ── native streaming ──────────────────────────────────────────────

def _chunk(delta_content=None, delta_tools=None):
    delta = mock.Mock()
    delta.content = delta_content
    delta.tool_calls = delta_tools
    choice = mock.Mock()
    choice.delta = delta
    chunk = mock.Mock()
    chunk.choices = [choice]
    return chunk


def _tool_delta(index, id_=None, name=None, arguments=None):
    tc = mock.Mock()
    tc.index = index
    tc.id = id_
    tc.function = mock.Mock()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def test_stream_native_assembles_text_and_tool_calls(monkeypatch):
    _set_env(monkeypatch)
    chunks = iter([
        _chunk(delta_content="正在查"),
        _chunk(delta_content="询……"),
        _chunk(delta_tools=[_tool_delta(0, id_="t1", name="get_", arguments='{"all":')]),
        _chunk(delta_tools=[_tool_delta(0, name="gpu_status", arguments='true}')]),
    ])
    sdk = _mk_openai(monkeypatch, create_retval=chunks)
    client = OpenAIClient()
    events = list(client.stream_complete("s", [{"role": "user", "content": "hi"}]))
    assert {"type": "text", "delta": "正在查"} in events
    assert {"type": "text", "delta": "询……"} in events
    tool = [e for e in events if e["type"] == "tool_use"]
    assert tool == [{"type": "tool_use", "id": "t1",
                     "name": "get_gpu_status", "input": {"all": True}}]
    # native stream requested, system message prepended, no tools sent when none
    called = sdk.chat.completions.create.call_args.kwargs
    assert called["stream"] is True
    assert called["messages"][0] == {"role": "system", "content": "s"}


def test_stream_native_two_parallel_tool_calls(monkeypatch):
    _set_env(monkeypatch)
    chunks = iter([
        _chunk(delta_tools=[_tool_delta(0, id_="a", name="list_containers", arguments='{}'),
                            _tool_delta(1, id_="b", name="get_gpu_status",
                                        arguments='{"x": 2}')]),
    ])
    _mk_openai(monkeypatch, create_retval=chunks)
    events = list(OpenAIClient().stream_complete("s", [{"role": "user", "content": "hi"}]))
    tools = sorted([e for e in events if e["type"] == "tool_use"],
                   key=lambda e: e["id"])
    assert tools == [
        {"type": "tool_use", "id": "a", "name": "list_containers", "input": {}},
        {"type": "tool_use", "id": "b", "name": "get_gpu_status", "input": {"x": 2}},
    ]


def test_stream_disabled_uses_emulated_single_chunk(monkeypatch):
    _set_env(monkeypatch, OPENAI_STREAMING="false")
    _mk_openai(monkeypatch)
    client = OpenAIClient()
    client.complete = mock.Mock(return_value=LLMResult(
        text="全量回复", tool_calls=[{"id": "a", "name": "x", "input": {}}]))
    events = list(client.stream_complete("s", [{"role": "user", "content": "hi"}]))
    assert {"type": "text", "delta": "全量回复"} in events
    assert {"type": "tool_use", "id": "a", "name": "x", "input": {}} in events
    assert client.complete.call_count == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest tests/test_llm_client_openai.py -v`

Expected: 新增的 `test_stream_native_*` FAIL（伪流式占位不会发 `stream=True`，也不会分片）；`test_stream_disabled_uses_emulated_single_chunk` 预期 PASS。

- [ ] **Step 3: 替换 `stream_complete` 为 native**

`backend/app/agent/openai_client.py` 中 `stream_complete` 方法体整体替换为：

```python
    def stream_complete(self, system, messages, tools=None, max_tokens=1024) -> Iterator[dict]:
        if not self.stream_enabled():
            yield from self._emulated_stream(system, messages, tools, max_tokens=max_tokens)
            return
        kwargs = dict(model=self.model, max_tokens=max_tokens, stream=True,
                      messages=[{"role": "system", "content": system}]
                      + self._to_openai_messages(messages))
        oa_tools = self._to_openai_tools(tools)
        if oa_tools:
            kwargs["tools"] = oa_tools
        stream = self.client.chat.completions.create(**kwargs)
        slots = {}  # tool_call index -> {'id','name','arguments'}
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue  # e.g. an empty usage-only chunk
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                yield {"type": "text", "delta": delta.content}
            for tc in (getattr(delta, "tool_calls", None) or []):
                slot = slots.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments
        for slot in slots.values():
            try:
                inp = json.loads(slot["arguments"] or "{}")
            except Exception:
                inp = {}
            yield {"type": "tool_use", "id": slot["id"],
                   "name": slot["name"], "input": inp}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest tests/test_llm_client_openai.py tests/test_llm_client_stream.py tests/test_agent_loop_stream.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
cd /amax/gpu_manager_v2 && git add backend/app/agent/openai_client.py backend/tests/test_llm_client_openai.py && git commit -m "feat(llm): OpenAIClient native SSE streaming (text deltas + tool_calls accumulation)"
```

---

### Task 6: `.env.example` 增加 `LLM_PROVIDER` + `OPENAI_*` 文档段

**Files:**
- Modify: `.env.example`（在 LLM/Agent 段头加 `LLM_PROVIDER`，段尾追加 OpenAI 子段）
- （无 Python 测试 —— 纯文档；验证方式是 diff 复核 + 两项 grep）

**Interfaces:**
- Consumes: Global Constraints 的 env 清单。
- Produces: 运维可照抄的 `.env.example`（设计文档 §4 的表落地为文件）。

- [ ] **Step 1: 在 LLM 段首加 `LLM_PROVIDER`**

`.env.example` 中，`# ── LLM / Agent ──` 标题与 `# LLM_* 优先级高于 ANTHROPIC_*。` 注释之间，插入：

```
# 选择 LLM 提供方：anthropic（默认，读下方 LLM_*/ANTHROPIC_*）| openai（读下方 OPENAI_*）。
# 两套 provider 配置可在 .env 共存，切换只改这一行；未知值后端启动即报错（fail-fast）。
LLM_PROVIDER=anthropic
```

- [ ] **Step 2: 在 LLM 段末尾（`# ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6` 之后）追加 OpenAI 子段**

```
# ── OpenAI（LLM_PROVIDER=openai 时生效，不读上方 LLM_*/ANTHROPIC_*）──
# API Key —— 必填（官方 key 或兼容网关 key，如火山方舟 /api/v3、vLLM）。
OPENAI_API_KEY=
# Base URL —— 可选；默认官方 https://api.openai.com/v1；兼容端点填这里（含协议与版本路径）。
OPENAI_BASE_URL=
# 模型名 —— 必填（无内置默认，防止误扣费）。值需匹配你的 key/网关，如 gpt-4o-mini。
OPENAI_MODEL=
# OPENAI_STREAMING: auto|true 走原生流式；false 关流式（一次性 complete）。语义同 LLM_STREAMING。
OPENAI_STREAMING=auto
```

- [ ] **Step 3: 验证**

Run:

```bash
cd /amax/gpu_manager_v2 && grep -n "^LLM_PROVIDER=" .env.example && grep -n "^OPENAI_" .env.example
```

Expected: 打印 `LLM_PROVIDER=anthropic` 及 4 行 `OPENAI_*=`。

- [ ] **Step 4: Commit**

```bash
cd /amax/gpu_manager_v2 && git add .env.example && git commit -m "docs(env): document LLM_PROVIDER + OPENAI_* in .env.example"
```

---

### Task 7: 全量回归 + 计划收尾

**Files:** 无新增/修改（只读验证）。
**Interfaces:** Consumes 全部已产出的模块与测试。

- [ ] **Step 1: 跑全量后端测试**

Run: `cd /amax/gpu_manager_v2/backend && /opt/anaconda3/bin/python -m pytest -q`

Expected: 全绿。若有 1-2 个与环境无关的历史失败（需先确认失败用例不是本次改动引入），记录下来但不得因失败而提交任何"修测试"补丁之外的改动。

- [ ] **Step 2: 编译检查两个改动入口模块**

Run: `/opt/anaconda3/bin/python -m py_compile /amax/gpu_manager_v2/backend/app/agent/llm_client.py /amax/gpu_manager_v2/backend/app/agent/openai_client.py /amax/gpu_manager_v2/backend/app/routers/agent.py /amax/gpu_manager_v2/backend/app/agent/cleanup.py`

Expected: 无输出（exit 0）。

- [ ] **Step 3: 提交任意回归修复**

仅当 Step 1 失败确为本次改动引入时执行；否则跳过：

```bash
cd /amax/gpu_manager_v2 && git add -A && git commit -m "fix(llm): regression from multi-provider refactor"
```

---

## 手动验收（可选，LLM key/订阅就绪后）

`LLM_PROVIDER=openai` + `OPENAI_*` 配好后重启后端，用 Agent 页面问一句"我有哪些容器"，确认能走 OpenAI 路径完成一次 tool round；再改回 `LLM_PROVIDER=anthropic` 重启，确认 Ark Coding Plan 路径仍工作。此步骤不阻塞上面代码任务的完成。
