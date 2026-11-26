# 486mem

486mem 是一个通用对话记忆库。接入方提供角色、用户、互动和反馈，模型通过同一份握手协议回查证据、提出记忆变更，并判断何时搭话。

核心用 Python 3.11 标准库和一个 SQLite 文件。应用内导入即可运行，模型接收普通 JSON；更换模型或接入其他语言时，适配这份 JSON 契约即可。示例只包含合成数据。

## 本地运行

在本目录运行下面的命令，需要 Python 3.11 或更高版本。

~~~powershell
python -B -X utf8 -m examples.demo
python -B -X utf8 -m unittest discover -s tests -v
~~~

示例使用一个排练助手和一个编程辅导助手，展示一次评价如何成为暂存观察、明确表达的偏好如何生效，以及模型怎样提交基于共同经历的主动开场。示例数据库位于临时目录，运行结束后自动清理。

从本目录安装后即可在自己的 Python 环境导入。

~~~powershell
python -m pip install .
~~~

## 接入顺序

1. **确定范围。** 宿主用已经核验的身份构造 `Scope(tenant, assistant, user, conversation)`。同一个 SQLite 文件可以承载多个用户和角色。四项全部匹配才共享这个范围内的原话和记忆。切换模型时沿用同一范围。
2. **记录观察。** 宿主将实际收到的消息、工具结果、发言机会、实际发出的回复或用户反馈交给 `store.record_event(...)`。每条事件使用稳定的 `event_id` 和带时区的时间。用 `related_to` 关联机会、发出的开场和后续反馈。用于生成发言的原文证据通过 `metadata.used_evidence_ids` 追踪。
3. **打开模型会话。** `bridge.open_turn(scope, event_id, persona=..., language=...)` 返回协议版本、触发事件、角色资料、语言、可用习惯、工具和决策结构的 JSON Schema，以及模型使用说明。宿主把这些交给自己选定的模型，将工具参数从模型适配到通用结构。
4. **执行工具调用。** 模型调用的工具由宿主转交给 `bridge.call_tool(scope, event_id, name, arguments)`。范围来自宿主，事件 ID 来自当前调用。模型可以回查证据、提出修改建议。
5. **检查搭话决策。** `bridge.validate_decision(scope, event_id, decision)` 检查 `speak`、`wait` 或 `skip`。宿主处理实际发送、去重、授权和发送回执。反馈记录应指向实际发出的开场，发送失败或无人回应由宿主按观测结果记录。

`bridge.run_turn(scope, event_id, model, persona=..., language=...)` 也可以代为完成第 3 至 5 步。接入方提供同步回调 `model(context, exchanges)`，负责用所选模型生成以下两类结果之一。

- 请求工具时返回 `{"tool_calls": [{"name": "memory_recall", "arguments": {"query": "排练"}}]}`。下一轮回调会在 `exchanges` 里收到对应调用和结果，工具失败会带错误信息。
- 给出决策时返回 `{"decision": {"action": "speak", "reply": "……"}}`。桥接层会先做协议校验再返回决策。

默认最多执行四轮工具调用。离线示例用一个简单回调演示回查、开场与反馈，接真实模型时替换该回调即可。

~~~python
from memory_framework import MemoryBridge, MemoryStore, Scope

store = MemoryStore("conversation-memory.sqlite3")
bridge = MemoryBridge(store)
scope = Scope("workspace-1", "learning-coach", "user-123", "session-456")

store.record_event(
    scope,
    event_id="user-message-1",
    kind="user_message",
    occurred_at="2026-11-26T17:00:00Z",
    text="以后讲代码题时，请先给结论。",
)
turn = bridge.open_turn(scope, "user-message-1", persona="耐心的编程辅导助手", language="zh")
~~~

## 握手协议

`open_turn` 将这些字段交给模型。

| 字段 | 用途 |
| --- | --- |
| `protocol_version` | 当前为 `"2"`。适配器使用本轮返回的决策结构，区分普通回复和主动搭话。 |
| `event` | 本轮触发原因和已观察的信息。事件类别包括 `user_message`、`assistant_message`、`tool_result`、`opportunity` 和 `feedback`。 |
| `recent_events` | 最多六条近期互动短摘录，带时间和证据 ID。原话和反馈元数据可按需回查。 |
| `experience_hints` | 主动机会专用。宿主给出话题线索时，至多带四条较早的共同经历 ID；由模型按需查阅原话。 |
| `persona`、`language`、`habits` | 宿主提供角色说明和场景示例，框架补入这些场景可用的已确认习惯。暂定习惯可用回查工具选择。 |
| `tools`、`decision_schema`、`instructions` | 可调用的工具、最终回答的结构，以及使用和解释结果的说明。 |

### 回查

| 工具 | 模型提供什么 |
| --- | --- |
| `memory_recall` | `query` 描述找什么，`intent` 选 `current`（当前值）、`historical`（历史版本）、`evidence`（证据）或 `habit`（行为习惯）。可以补 `anchor_ids` 指向已知线索，或用 `limit` 控制返回条数。 |
| `memory_read_evidence` | 根据回查给出的证据 ID 取原话或结果，用来核对引用、纠正和历史经过。 |

`recorded_at` 是宿主获知的时间，`effective_at` 是开始生效的时间。`as_of` 可用来问某一时点的有效值；桥接层同时把可用证据限定在本轮开始时。确认暂存说法会追加版本，因此能回放“当时还是猜测，后来才确认”。

`anchor_ids` 可以指向原话、记忆或反馈事件。回查会沿已有的关系双向搜索，能由搭话找反馈，也能从反馈找回开场和原始证据。多个说法冲突时，当前值带争议标记。暂存内容保留自己的证据状态。

### 新增、更改、限制与遗忘

模型通过 `memory_propose` 提交 JSON 对象。下面的试听评价引用了原话的证据 ID。

~~~json
{
  "proposal_id": "p-1",
  "operation": "add",
  "kind": "observation",
  "subject": "本次试听",
  "predicate": "版本评价",
  "value": "这次的 03 更自然",
  "evidence_ids": ["message-1"]
}
~~~

| 字段 | 用法 |
| --- | --- |
| `proposal_id` | 为本次提案提供的稳定 ID。提交重试使用相同 ID。 |
| `operation` | `add` 新增，`revise` 修订内容，`prioritize` 调整权重或保留标记，`restrict` 调整主动提及限制，`forget` 遗忘。 |
| `kind` | 新增条目的性质，可以是 `fact`、`preference`、`habit`、`commitment`、`observation`。 |
| `subject`、`predicate`、`value` | 谁或哪件事、修改哪项信息、这项信息是什么。用明确、能复查的表述。 |
| `evidence_ids` | 支持改动的已记录事件 ID。比如用户的实际原话、工具结果或已观察到的反馈。 |
| `memory_id`、`base_version` | 对现有条目操作前先回查，提交该条目的 ID，并用返回结果中的 `head_version` 填 `base_version`。`version` 表示当前结果所展示的版本。 |
| `relation` | 修订时说明是 `correction`（纠正旧说法）、`supersession`（新状态取代旧状态）、`conflict`（说法有冲突）还是 `unknown`（关系尚未确定）。 |
| `effective_at` | 明确知道何时生效时，以带时区的时间填写。 |
| `weight` | `add` 或 `prioritize` 可填 0–100 的整数。由模型根据上下文和证据判断，回查返回初始分数、当前分数和去重的关联条目数。 |
| `pinned` | 用户明确要求保留时，由模型在 `add` 或 `prioritize` 提议。设置和解除都需要宿主核验授权。 |

一次「这次 03 更好」适合作为暂存观察。若用户后来明确要求「以后日语默认 03」，宿主可根据实际用户事件验证提案，再通过 `store.propose(scope, proposal, event_id=..., approved=True)` 应用。模型工具提交本身会保留为暂存或返回需要确认。模型收到 `applied` 后，才能告诉用户变更已生效。看到 `stale_version` 时应重新读取条目，结合新版本调整提案。

如果已生效的偏好后来又被用户改为 04，模型先回查这条偏好，再带着返回的 ID 和版本提出修订。

~~~json
{
  "proposal_id": "p-2",
  "operation": "revise",
  "memory_id": "m-japanese-voice-default",
  "base_version": 1,
  "relation": "supersession",
  "value": "04",
  "evidence_ids": ["message-4"]
}
~~~

这里保留了「曾经用 03，后来改为 04」的历史。若用户说「我之前说错了，一开始就想用 04」，关系填 `correction`。收到竞争说法时可填 `conflict`，回查后把争议和证据一起交给模型。工具字段非法或证据无效时，宿主应把具体失败转交给模型，由它调整提案。

`restrict` 获批后可限制主动提及，也可用 `value: false` 恢复。用户问起时仍可回查。

模型给出的权重用来区分相关程度相近的候选。`initial_weight` 和 `related_memories` 都可以供模型调分，同一个关联条目只计一次。一个多年前只提过一次的细节，也可以由模型评为高权重。比如用户说“绝对不能忘那天定下的暗号”，模型可建议高分并设置 `pinned: true`。宿主确认后，后来把分数调到 0 也无法遗忘这条。

**遗忘有两条硬边界。** 被用户明确要求保留的条目返回 `pinned`；原话或其登记派生内容还被其他记忆引用时返回 `shared_evidence`。两种情况都拒绝整个遗忘动作，保留所有内容。两项都无阻碍并获宿主批准时，才清除该条的原始证据和已登记派生。外部副本和备份由接入方管理。

### 普通回复

`{"action":"speak","reply":"……"}` 是普通发言格式，可以附 `evidence_ids` 供宿主追踪。用户明确说还有下半句时返回 `{"action":"wait"}`，需要收住时返回 `{"action":"skip"}`。只有 `wait` 可附 `review_at`。

语言及语体先看用户本轮的明确要求，再看宿主的 `language` 默认值。宿主留空时读取已确认的语言习惯，最后才参考用户消息的语言。模型应在整段正文保持选定的语言与语体。寒暄、报喜和倾诉宜接住具体细节后收住。真正的指代歧义、求助缺少关键条件，或此前已经约定回访时，可以问一个具体问题。

### 主动搭话和反馈

宿主收到主动机会时使用专用的 `decision_schema`。如果选择开口，第一条证据必须是同一会话的具体共同经历，并附上从原话摘出的 `experience_quote`。

~~~json
{
  "action": "speak",
  "topic": "昨天排练留下的一小段",
  "reason": "共同经历明确，而且有一个新的练法可分享",
  "evidence_ids": ["rehearsal-message"],
  "experience_quote": "慢一点的节拍",
  "reply": "我想到一个练法。先把最容易抢拍的两小节单独慢练。"
}
~~~

主动开口由宿主创建 `opportunity` 事件，提供 `metadata.allowed` 和带时区的 `metadata.expires_at`。宿主可在用户允许的时段给机会，模型自己从共同经历里找值得说的话。`speak` 的首条证据要对应实际聊过的内容；框架会核对 4–120 字的原文摘录。仅供习惯或偏好引用的记录可作补充依据。若一条原话同时包含共同经历和偏好，宿主可用 `metadata.evidence_role = "experience"` 标记。`"policy"` 标记只作为政策或许可。用户保留话题的发起权、话题已经结束或缺乏共同经历时，可直接跳过；需要延后时选择等待。

程序实际发出后再记录发言，并通过 `related_to` 关联用户反馈。回复轮数、会话长度等属于接入方记录的观察，模型结合原话解释这些反馈并提案更新习惯。

## 参考 spec（可修改草案）

- **目标。** 同一握手、不同角色和用户。换模型后能回查同一段共同经历，继续遵守用户明确确认的偏好，并按适用场景延续接话习惯。
- **可观察验收。** 用两个独立范围分别记录事件时，任一范围都查不到另一个范围的证据。显式修改可以变更当前值，历史查询仍能识别旧说法。未经确认的观察只能作为可能依据。有效主动机会可以返回合法发言，过期或缺证据的机会会被拦截。
- **暂定。** V1 使用精确范围隔离和本地 SQLite。检索从文本和证据关联入手，允许宿主选择模型及决定机会频率。同一角色跨会话授权共享、向量检索、定时调度和实际发送可由宿主按具体需求接入。框架本身面向可信宿主进程，外部网络部署需要宿主建立完整的认证与授权边界。
