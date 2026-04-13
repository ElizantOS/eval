# eval

`eval` 是一个可复用的评测框架基础包，用来承载“跨项目通用、与具体业务无关”的评测运行能力。

它**不是**一个完整的业务评测工作区。  
它提供的是：

- driver 抽象基类
- `responses` 协议基类
- `chat_completions` 协议基类
- 动态 driver 加载
- 通用请求 / SSE / 会话续跑底座

而这些内容**不在这个仓库里**：

- 具体业务 target 实现
- target 配置
- cases
- runs
- dashboard 的本地工作区数据
- 业务自己的 backend/.env 和 skill 目录

这些都应该继续放在消费方仓库里。

## What This Repo Contains

当前包结构：

```text
src/eval/
  __init__.py
  common.py
  adapters/
    __init__.py
    runner_common.py
    responses_driver_base.py
    chat_completions_driver_base.py
```

职责说明：

- `eval.common`
  - workspace 目录推断
  - 通用错误类型
  - 基础路径/序列化工具
- `eval.adapters.runner_common`
  - `TargetDriver`
  - `DriverEvent`
  - `RequestSpec`
  - `SimulatedUserReply`
  - 动态 `driver_class` 加载
  - 通用 HTTP / SSE / retry / run loop
- `eval.adapters.responses_driver_base`
  - `/v1/responses` 协议共性
  - `previous_response_id`
  - session header
  - SSE 事件摘要
  - responses 风格 follow-up 构造底座
- `eval.adapters.chat_completions_driver_base`
  - `messages`
  - `tool_calls`
  - `tool` role follow-up
  - chat-completions 风格 history replay

## Intended Architecture

推荐的消费方目录结构：

```text
your-repo/
  pyproject.toml

  evals/
    config/
      targets/
        your-target.yaml
    cases/
      your-target/
    runs/
    targets/
      your_target.py
```

其中：

- 外部依赖 `eval`
  - 只提供框架层基类和运行底座
- 本地 `evals/`
  - 仍然保留 workspace
  - 仍然定义 target/cases/config/runs

## How Consumers Should Use It

### 1. 在消费者仓库里声明 git 依赖

`pyproject.toml`:

```toml
[project]
dependencies = [
  "eval",
]

[tool.uv.sources]
eval = { git = "ssh://git@github.com/ElizantOS/eval.git", rev = "main" }
```

### 2. 在消费者仓库里写自己的 target driver

新的 `responses` 平台：

```python
from eval.adapters.responses_driver_base import ResponsesDriverBase, DriverEvent
from eval.adapters.runner_common import call_json_model

class MyResponsesDriver(ResponsesDriverBase):
    def tool_specs(self) -> list[dict]:
        ...

    def parse_interaction_event(self, candidate_fragments, raw_response) -> DriverEvent:
        ...
```

新的 `chat_completions` 平台：

```python
from eval.adapters.chat_completions_driver_base import ChatCompletionsDriverBase

class MyChatDriver(ChatCompletionsDriverBase):
    ...
```

### 3. target 配置里绑定本地具体类

```yaml
driver_class: evals.targets.my_target.MyTargetDriver
```

注意：

- `driver_class` 是**运行时动态导入绑定**
- 它应该始终指向消费者仓库里的**具体 target 类**
- 不应该直接指向这个外部包里的基类

## Workspace Contract

这个包默认通过环境变量或当前目录去推断 workspace：

- `SMARTBOT_EVAL_WORKSPACE_DIR`
  - 指向消费者仓库里的 `evals/` 工作目录
- `SMARTBOT_EVAL_SMARTBOT_DIR`
  - 可选，指向业务仓库根目录

所以消费方保留本地 `evals/` 是设计的一部分，不是过渡方案。

## Current Scope

当前这个外部包先抽出了最关键、最稳定的一层：

- driver framework
- protocol base classes

还**没有**把 dashboard / reporting / case sync / 本地 CLI 全部做成独立外部包能力。  
这部分仍然由消费方仓库自己的 `evals/` workspace 负责。

## Verification

建议消费方接入后至少验证：

1. `uv sync`
2. 本地 target driver 能从 `driver_class` 正确解析
3. 一个最小单 case run 能跑通
4. 本地 workspace 的 `.promptfoo/` 和 `runs/` 正常写入
