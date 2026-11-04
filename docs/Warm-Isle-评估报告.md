# Warm-Isle 能否直接替换本项目的 LLM 模块 · 评估报告

> 评估日期：2026-11-04
> 评估对象：<https://github.com/Anran3366/Warm-Isle>（AGPL-3.0，镜像 <https://gitee.com/menran/warm_isle>）
> 对比对象：`E:\0\qq-music-collector` 的 `llm` 相关模块（`src/plugins/music_collector/`）
> 评估方式：完整 clone 源码逐文件核对（Java 21,714 行 / 本项目 Python 相关 5,711 行），非文档推测

---

## 结论

**不能直接替换，一次都不行。** 三个理由，任意一个都足以阻断：

1. **语言与运行时不同**：Warm-Isle 是 Java 21 / Spring Boot 3 的独立进程，本项目是 Python 3.10+ 的 NoneBot2 插件。跨语言没有"换模块"这回事，只有"重写"或"整项目换掉"。
2. **它是整台机器人，不是库**：自带 Vue3 后台、Docker Compose、每个 QQ 号一个 NapCat 容器、自己的 SQLite 分库。它的 `llm` 包只有 **480 行**，且深度依赖它自己的 `memory` / `chat` / `budget` / `knowledge` 包，抽不出来。
3. **功能面是倒退**：本项目有工具调用、技能系统、联网抓页（含 SSRF 校验）、群聊背景、群风格卡、人设修订提案、戳一戳触发、非音乐链路兜底。**这些 Warm-Isle 一个都没有**（交叉核对见附录 A）。

**真正有价值的用法只有一种：不是替换，是"抄设计"。** 它有 6 个设计点确实比本项目强，且都能用几十行 Python 移植（详见 §四）。

---

## 一、被评估对象

### 1.1 Warm-Isle 是什么

| 项 | 内容 |
|---|---|
| 定位 | 自托管的 **QQ 陪聊机器人**（"暖岛·小满"），面向"有长期记忆的 AI 好友"场景 |
| 技术栈 | Java 21 / Spring Boot 3 · SQLite(WAL) · Vue3 + Element Plus + Vite · NapCat(OneBot v11) · DeepSeek · MiMo TTS · ONNX Runtime · Docker Compose + Caddy |
| 形态 | 单机 Docker 部署，`qqbot` 进程 + 每账号一个 `napcat-*` 容器 + `guard` 图片预筛容器 |
| 核心域 | **单个聊天对象**（"机器人 × QQ 用户"）的陪聊：记忆、心情、熟悉度、主动消息、表情包、语音条、RAG 知识库、空间说说 |
| 许可 | **AGPL-3.0** |
| 规模 | Java 21,714 行 / 16 个测试类 / CHANGELOG 90KB |
| 部署要求 | Linux（Ubuntu 22.04+/Debian 12+）、≥2G 内存（每多一个账号 +250M）、≥20G 磁盘、公网 80/443 |

**关键认知**：它是一个"成品应用"，不是可复用的框架或 SDK。官方 README 的定位就是"想自己部署一个 QQ AI 好友的人"，不是"想给你的机器人加个 LLM 模块的人"。

### 1.2 它的 "LLM 模块" 到底有多大

`src/main/java/com/qqbot/llm/` 全包 **480 行**：

| 文件 | 行数 | 职责 |
|---|---|---|
| `DeepSeekClient.java` | 113 | 非流式 `/chat/completions`，JDK HttpClient，无 SDK |
| `PromptBuilder.java` | 289 | 拼 prompt：人设 → 记忆检索 → 历史 → 当前消息 |
| `TimeGuard.java` | 64 | 白天出现"晚安/今晚/早点睡"时改写的兜底 |
| `LlmResult.java` | 5 | `content + promptTokens + completionTokens` |
| `BudgetExceededException.java` | 9 | 预算用尽信号 |

另有 4 个**松散耦合但功能上属于"LLM 层"**的类：

| 文件 | 行数 | 职责 |
|---|---|---|
| `memory/MemoryRecall.java` | 361 | 记忆检索打分（本地计算，不花 token） |
| `chat/ReplyComposer.java` | 201 | 拆条 + 打字延迟 |
| `budget/BudgetGuard.java` | 48 | 日预算闸门 |
| `image/VisionClient.java` | 94 | 视觉描述 |

`DeepSeekClient` 的能力边界（这点很重要）：**只发 `model / temperature / max_tokens / stream=false / messages`**。没有 `extra_body`、没有思考参数（`thinking`/`reasoning_effort`）、没有 `presence_penalty`/`frequency_penalty`/`top_p`/`seed`/`stop`、没有流式、没有降级重试链。本项目 `LLMConfig` 仅这一段的字段就比它整个客户端多。

### 1.3 本项目现有 LLM 相关代码面

| 文件 | 行数 | 职责 |
|---|---|---|
| `llm.py` | 1,249 | 配置归一/思考参数/降级链/tool 循环/`LLMBot` |
| `webui.py` | 2,435 | 后台后端 |
| `webui_frontend.py` | 2,994 | 后台前端（单文件） |
| `tools.py` | 680 | 工具集 + `dispatch()` |
| `webtool.py` | 658 | 抓网页（四类 kind / 缓存 / SSRF 逐跳校验） |
| `memory.py` | 580 | 群记忆档案（文件式） |
| `persona.py` | 511 | 人设修订提案 + 护栏 |
| `banter.py` | 507 | 主动搭话 + 判官 |
| `style.py` | 443 | 群风格卡 |
| `skills.py` | 367 | 技能系统 |
| `websearch.py` | 312 | 联网搜索 |
| `llmtrace.py` | 217 | 调用追踪（内存环形 200 条） |
| `digest.py` | 187 | 记忆整理 |
| **小计** | **5,711**（不含 webui 两个文件） | |

对比：它的 LLM 层 480 行，本项目的 LLM 生态 **5,711 行**，且问题域不同（群聊 vs 一对一）。

---

## 二、为什么不能"直接替换"

### 2.1 语言与运行时（阻塞项，无法绕过）

- Warm-Isle：`pom.xml` → Maven → `java -jar` → Spring Boot 容器 → 连 NapCat WebSocket
- 本项目：`pyproject.toml` → `nonebot.init()` → 加载 `music_collector` 插件 → 共享 NoneBot 事件循环

本项目 `llm.py` 里的一切都建立在 Python 生态上：`asyncio.Lock`/`deque`、`httpx.AsyncClient`、pydantic 配置校验、loguru、NoneBot 的 `Message`/`Bot` 类型。Java 的 `DeepSeekClient` 是同步阻塞调用，拿进 Python 只能"照着行为重写一遍"——那就不是"用它的模块"，而是"看它的代码自己写"。

### 2.2 它是整台机器人（形态冲突）

用它的 `BotProperties` 举例：配置模型是**一个** `llm` 段（apiKey/model/baseUrl/temperature/maxTokens/historyRounds/priceIn/priceOut），加上 `persona`/`reply`/`admin`/`push` 等段。而本项目是"**按群覆盖 + 全局段**"的双层配置体系（`GLOBAL_ONLY_PREFIXES`、`GroupOverrides`、`KEY_INDEX`、热生效 PATCH）。两套配置模型没有交集。

再举一个更硬的例子：它的数据模型是 `bots/<账号>.db` 里按 `qqId` 存消息/记忆/人设；本项目的记忆是 `data/memory/<群号>/people/<QQ号>.md` 文件 + SQLite `chat_log`。**两边的历史数据结构完全不兼容**，已有的记忆资产无法迁移过去（迁移需要重写抽取管线，且群维度信息会丢失）。

### 2.3 功能面倒退（能力矩阵）

| 能力 | 本项目 | Warm-Isle | 判定 |
|---|---|---|---|
| LLM 网关 | OpenAI 兼容任意中转，`extra_body` 可覆盖任何键 | 单一 `baseUrl + model` | 本项目强 |
| 思考参数 | DeepSeek(`thinking`+`reasoning_effort`)/OpenAI(`reasoning_effort`)/Moonshot 三写法 + `auto` 推断 | 无 | 本项目强 |
| function calling / tools | 有（`tools.py` 680 行，`tool_rounds` 默认 5，`strip_tool_marks` 三道闸） | **无**（全仓 `"tools"`/`tool_calls`/`function_call` 零命中） | 本项目强 |
| 技能系统 | 有（`data/skills/*/SKILL.md` + 受限脚本 + 沙箱） | **无** | 本项目强 |
| 联网抓页 | 有（四类 kind / 缓存 / SSRF 逐跳校验 / B站特判） | **无**（仅 `meme/SearxClient` 表情包检索） | 本项目强 |
| 联网搜索 | 有（Bing HTML + 限流 + 跑偏检测） | 仅 meme 用 Searx | 本项目强 |
| 转发聊天记录 | 有（`describe_forward` + 内联 node） | **无** | 本项目强 |
| 群聊背景 | 有（"上次说话之后" + 2500 字硬上限 + 单独记账） | **无**（一对一模型，无群概念） | 本项目强 |
| 群风格卡 | 有（`style.py`，学"这个群怎么说话"） | 无（只有知识库轮换的风格样本） | 本项目强 |
| 人设修订提案 | 有（`persona.py`，判官 + `vet_edits` 护栏） | 无 | 本项目强 |
| 戳一戳触发 | 有 | 无（未见 `PokeNotifyEvent` 相关处理） | 本项目强 |
| 降级链 | 有（去图/丢历史/去 tools，各一次） | 无 | 本项目强 |
| 调用追踪 | 有（`llmtrace.py` + 后台"🧠 LLM 调用"页） | 有 `/api/logs` 文本日志 | 平 |
| 视觉 | 有（`llm.vision` + `max_images`） | 有（`VisionClient` + 本地 guard 预筛 NSFW） | **它强** |
| 记忆检索 | 按活跃倒序装到预算满（无相关度） | **打分检索** top 8~12 / ≤1200 字 / 三块注入 | **它强** |
| 记忆衰减/过期/冲突合并 | 无 | 有（`importance`/`confidence`/`expires_at`/`superseded_by`） | **它强** |
| 主动回忆（follow_up_at） | 无（只有随机搭话） | 有（六类事件排期 + 额度约束） | **它强** |
| 拆条 + 打字延迟 | 无（整段发） | 有（`ReplyComposer` + `REPLY_TYPING_MS`） | **它强** |
| 日预算闸门 | 无（只在 llmtrace 记 usage token） | 有（`BudgetGuard` + 回退话术） | **它强** |
| 时间感兜底 | 无（仅提示词约束） | 有（`TimeGuard` 改写） | **它强** |
| 语音条 / 语音克隆 | 无 | 全套（MiMo TTS） | 它强（但与本项目无关） |
| 表情包 | 无 | 全套（入库/打标/按情绪发） | 它强（但与本项目无关） |
| 多账号 | 单账号 | 多账号（每账号独立 NapCat） | 它强 |
| 音乐收集业务 | **全套** | **完全没有** | 本项目独有 |

**净账**：LLM 本体本项目更强且更贴合群聊；它领先的 6 项都是**可以单独搬过来的小设计**（§四），没必要为此换掉整个 LLM 层。

### 2.4 许可：AGPL-3.0（法律层面）

Warm-Isle 是 **AGPL-3.0**，比 GPL 更严：§13 要求"通过网络与修改版交互的用户，必须能获得源码"。

- **直接搬代码进本项目** → 本项目成为衍生作品 → 整个 `qq-music-collector` 必须 AGPL 化，且要对群友提供服务端源码下载。本项目目前**没有 LICENSE 文件**，仓库是私有的，这一搬等于单方面改变项目性质。
- **抄设计思路自己实现**（公式、参数、提示词结构、流程）→ 不受限。**这是本报告推荐的路径。**

---

## 三、如果一定要"用它"，三种形态的成本

| | A. 整项目切换 | B. 双 bot 并行 | C. 当 LLM 网关调 |
|---|---|---|---|
| 做法 | 扔掉 collector，全用 Warm-Isle | Warm-Isle 一个号管陪聊，collector 一个号管音乐 | 给 Warm-Isle 加 `/api/llm/chat`，Python 调它 |
| 可行性 | 可行但不合理 | **可行** | **不可行（收益为负）** |
| 丢掉什么 | 音乐收集、网易云歌单同步、期号、长图渲染、猜歌、`/music` 全套命令、现有 WebUI、5,711 行 LLM 生态 | 无（但两套记忆不互通） | 无 |
| 要做什么 | 用 Java 重写整个音乐域（约 8 个模块） | 部署 Docker、配 80/443、改 3 个入口为"交棒"、两套白名单 | 改 Java 加接口 + 部署 + 绕鉴权 + 仍要维护两套 |
| 成本 | **数月** | **1~2 天** | 数天 |
| 建议 | ❌ | ⚠️ 仅当你确实想要它的语音/表情包 | ❌ |

关于 C 的细节：它的 `chat-test` 端点只有两个，分别在 `EmojiController` 和 `VoiceController`，是表情包/语音测试，**不是通用 LLM 接口**。要用它当网关必须自己改 Java 代码加一层，那还不如直接调 DeepSeek——毕竟你的 `llm.py` 已经比它的客户端能力强。

关于 B 的现实摩擦（若你仍想走）：

1. **记忆资产不迁移**：现有 `data/memory/<群>/people/*.md` 是"群 × 人"档案，它的是"账号 × QQ 用户"，语义对不上，只能重来。
2. **三个入口要改成"不回复"**：见附录 C。
3. **两套运维**：Docker + systemd、两套日志、两套白名单、两次重启。
4. **群友体感**：@ 音乐 bot 不搭理人，@ 陪聊 bot 不理歌单——需要群公告解释。

---

## 四、真正值得抄的 6 个设计点（推荐路径）

以下全部是**纯设计移植**，不需要它的任何代码。工作量按"改一个文件 + 补回归测试"估。

### 4.1 记忆检索打分（最值钱的一项）

**它的问题意识**：全量注入会把 prompt 撑大（短消息也带几十条），既贵又让模型背清单。记忆的价值在"能转化为关心"，只需要和**当前这句话**相关的部分。

**它的公式**（`memory/MemoryRecall.java`）：

```
score = 相关度 × 0.5 + 重要度 × 0.3 + 新鲜度 × 0.2
取 top 8~12 条，总长 ≤ 1200 字，单条 ≤ 90 字
置信度 < 0.6 不注入
```

- **相关度**：当前这句话做中文**二元组分词**（`我昨天加班到十点` → `我昨/昨天/天加/加班/…`，英文数字按词），算被这条记忆 `key+value` 覆盖的比例，再 `×2.2` 封顶 1.0；`key` 命中额外 `×1.4`。
- **新鲜度**：指数衰减，半衰期 30 天（今天 ≈1，30 天 ≈0.5）。
- **无命中回退**：只带最近 1~2 条事件；再退化到 3 条 `profile`/`relation`。
- **注入分三块**：`【关于对方】profile` / `【你们之间】relation+world` / `【最近发生】event`，结尾固定一句"以上是印象，自然带入对话，不要逐条复述，也不要说「根据我的记忆」"。

**本项目现状差异**：`memory.render_for_prompt()` 是**按最后活跃倒序装人，装到预算满为止**（`memory.inject_chars`），完全没有相关度——问一句"周杰伦哪年的"，也会把整个群的人档案塞进去。

**移植建议**：`memory.py` 保持文件式存储不变，只在 `render_for_prompt()` 之前插一层打分挑选。需要新增的最小信息是每人的"最后活跃时间"（已有）+"重要度"（可用"被提及次数"或人工标注，缺失时取常量 0.5）。**离线纯本地计算，不花 token，失败可回退旧行为**——风险很低。

**注意**：本项目是**群**维度、多成员；不要照搬"只挑一个人的记忆"，正确做法是"群概况必带 + 成员档案按相关度打分取 top N + 末尾注明'还有 X 人未展开'"（现有代码已经会注明"还剩多少人没进来"，这个设计要保留）。

### 4.2 TimeGuard：时间感兜底

**它的事故**：上午 09:55 回复"今晚先睡好"；18:07 说"别大晚上做决定"（用户回"现在才18点"）。

**它的做法**（`llm/TimeGuard.java`，64 行）：

1. 只在 06:00–20:00 生效（夜间不拦）；
2. 正则在回复里找 `晚安|今晚|大晚上|这么晚|早点睡|该睡了|睡吧|做个梦|好梦`；
3. 命中则**低温暖改写一次**（temperature 0.5, max_tokens 200）：给出当前时刻与时段（上午/中午/下午/傍晚），禁止上述词，保持原意与口语，只输出一句；
4. 改写结果若还含夜间词或为空 → **退回词表硬替换**（`今晚`→`今天`、`晚安`→`回头聊`、`早点睡`→`早点休息`、`该睡了`→`别太累`）。

**为什么值得抄**：本项目 `system_prompt` 里只有"时段规则"这类软约束，模型不听话就没有兜底。这段是"软约束 + 硬兜底"的双层保险，代价是一次额外的低温暖调用（只在命中时发生）。

**注意**：本项目走 `llm.complete()` 就能实现（一次性纯文本补全、`thinking="off"`、不占冷却、不写历史），无需新设施。

### 4.3 日预算闸门

`budget/BudgetGuard.java`（48 行）逻辑极简：`dailyBudget <= 0` 视为不限；`usage.todayCost() >= budget` 则所有模型调用直接抛 `BudgetExceededException`，调用方回退到不花钱路径，并给一句**不暴露原因**的兜底话术（"我这会儿有点卡，等下再回你哈"）。

**本项目现状**：`llmtrace.py` 只记录每次的 `usage` token（环形 200 条），**没有累计、没有单价、没有闸门**。中转挂了或模型抽风狂刷时没有止损。

**移植建议**：`llmtrace` 里已能拿到 `usage`，加"当日累计 token × 单价 → 当日花费"，在 `_llm_gate` 里加一道判断即可。单价做成配置项（对齐项目"时间相关全部可配置"的偏好）。

### 4.4 拆条 + 打字延迟

`chat/ReplyComposer.java`（201 行）的完整规则，值得逐条照搬：

1. **先按换行切**（模型自己分段的情况），再**按句末标点切**（`。！？!?~～…`）；
2. 按 `REPLY_MAX_CHARS`（默认 20 起）累加，超长就断开；
3. **碎片合并**：≤6 字的片段并回前一条（避免"嗯。""啊。"连发）；
4. **逗号二次拆**：长度 ≥16 且含逗号时，在第一个逗号处分成两条（真人停顿）；要求左右各 ≥5 / ≥4 字才拆；
5. **目标条数随机**：<12 字 1 条；<30 字 1~2；<60 字 1~4；≥60 字 1~5；且 1/3 概率再多一条，"让节奏不那么规律"；
6. **`fitCount`**：条数多了就合并最短的相邻两条（用"、"连接），少了就拆最长的一条（在逗号/分号处切）；
7. **延迟**：每条 `min(4000, base + len×35)` ms 模拟打字，条间 `REPLY_SPLIT_DELAY_MS`。

**本项目现状**：无。`ReplyConfig` 是音乐回复模板，与聊天无关，LLM 回复是整段发一条。

**为什么值得抄**：这是"AI 味"最直接的来源之一——一整段 200 字连发，和分三口气说，观感差距极大。本项目已经有 `persona.py` 在做"AI 味粗筛"，拆条是配套的物理层修复。

**移植注意**：NoneBot 里需要处理"多条发送 + 中途被打断/撤回"的边界；建议做成 `reply.split` 开关，默认关，先在单群观察。

### 4.5 时间间隔提示

**事故**：用户隔 23 小时回来，机器人回"刚想发呢，你就来了"——把前一天的情绪和话题当成现在。

**做法**（`PromptBuilder.timeGapHint`）：

- 间隔 < 2 小时：不提；
- ≥ 2 小时：在 system 里插一句"距离你们上一次说话已经过去 N 小时/天。除非对方主动提起，不要接着上次的话题往下演，也不要说「我刚想找你」「我一直在等你」这类假装连续的话；把它当成新的一段对话"；
- ≥ 6 小时：`ChatService` 只带最近 6 条历史（而不是 10 轮）。

**本项目现状**：有"群聊背景 = 它上次说话之后"的划分，但**没有"隔了多久"的显式提示**，也没有按间隔缩短历史的逻辑。

### 4.6 SPEAKING_RULES 的六段结构 + 它的变更日志

它的固定规则被重构过一次：从 7 块 2,303 字符合并为 **6 块 1,206 字符（-48%）**，六块是：

`说话方式` / `接住情绪` / `往前聊` / `表情语音图片` / `记性与分寸` / `时间感`

更值钱的是 `docs/prompt-changelog.md`（9.8KB）——它把**每一次提示词改动都记了"改了什么、为什么、哪次对话触发的"**。摘几条对群聊同样有效的：

| 事故 | 规则修正 |
|---|---|
| 连续 3 次复读（1759/1761） | 新增"不复述、不总结对方刚说的话，也不把上一句换个说法再说一遍" |
| 被说"阴阳/敷衍"时反问"我哪有"（1947/1953） | 被质疑先认一句（"行吧，是我嘴欠"）再往下聊，**禁用反问式辩解** |
| 反复解释自己的能力（"语音我发不出来呀"） | 改成正向表述：说明它**有什么能力、该怎么用**，而不是"别聊能力" |
| 把括号里的系统记录当台词发出去（1945/1955） | 代码侧 `isMediaMarker` 过滤 + 提示词里**不要反复出现那个格式**（提一次就会诱发照抄） |
| 发图不解释梗（1938，在讲"奶龙是什么"） | 对方发表情包先当玩笑接，按梗理解，禁止抠字面/问"是不是打错字" |
| 主动说"今天聊得有点多，先歇会儿" | 禁止主动结束对话；只有对方说要睡/要忙才可收尾 |

**教训（对所有提示词都成立）**：规则越堆越散、同一条在三处出现且口径不一，模型就会挑最省事的那条执行；**提示词里反复出现的格式会被照抄**。

**移植建议**：不照抄文字（AGPL + 你的场景是群聊不是私聊），而是用它的**结构**重排你的 `system_prompt`，并把"改了什么/为什么/哪次对话"记进你的 `overview.md` 或 `.workbuddy/memory`。

---

## 五、本项目比它强、绝对不要丢的 8 项

1. **工具调用 + 三道闸**（`tools.py` + `strip_tool_marks`）：它完全没有 tools。你 2026-10 修的那个"最后一轮不带 tools 时模型吐出 `<｜｜DSML｜｜invoke>`"是真实事故。
2. **技能系统**（`skills.py`，聊天里禁装技能的 `ALLOWED_TOOL_NAMES` 白名单）。
3. **联网抓页**（`webtool.py`：四类 kind / 缓存 / **SSRF 逐跳校验必须在查缓存之前** / B 站走 API）。
4. **群聊背景**（`_llm_context()` + 2500 字硬上限 + `bg_chat_min_rows` 补位）。
5. **群风格卡**（`style.py`）与人设修订提案（`persona.py` 的 `vet_edits()` 护栏）。
6. **主动搭话 + 判官**（`banter.py`：`_LAST_ID` 游标、勿扰顺延、判官"先判否定词"）。
7. **三个被动入口的互斥设计**（防回两遍：`_reply_llm_link` 见 `_at_bot` 即 return）。
8. **音乐业务本身**——这是本项目存在的理由。

---

## 六、附录

### 附录 A：核对方式（可复现）

```bash
git clone --depth 1 https://github.com/Anran3366/Warm-Isle.git
cd Warm-Isle

# 1. 确认无 function calling / tools
grep -rn '"tools"\|tool_calls\|function_call' src/main/java   # → 0 命中

# 2. 确认无技能系统
grep -rn 'Skill\|skill' src/main/java                          # → 0 命中

# 3. 确认无网页抓取
grep -rn 'Jsoup\|HttpURLConnection\|fetchPage' src/main/java    # → 0 命中（仅 SearxClient 供 meme 用）

# 4. LLM 包规模
wc -l src/main/java/com/qqbot/llm/*.java                       # → 480

# 5. 全仓规模
find src/main/java -name '*.java' | xargs wc -l | tail -1       # → 21714
```

### 附录 B：Warm-Isle 关键文件索引（若要精读）

| 路径 | 关注点 |
|---|---|
| `docs/memory-design.md` | ★★★ 记忆 v2 完整设计稿：四类记忆 / 可行动性打分 / 主动回忆时间表 / 风险表。**整仓最有参考价值的文件** |
| `docs/prompt-changelog.md` | ★★★ 每次提示词改动的"为什么"，全是真实事故 |
| `src/main/java/com/qqbot/memory/MemoryRecall.java` | ★★ 检索打分实现（361 行） |
| `src/main/java/com/qqbot/chat/ReplyComposer.java` | ★★ 拆条算法（201 行） |
| `src/main/java/com/qqbot/llm/PromptBuilder.java` | ★★ prompt 拼装顺序 + `SPEAKING_RULES` 六段 |
| `src/main/java/com/qqbot/llm/TimeGuard.java` | ★ 时间感兜底（64 行） |
| `src/main/java/com/qqbot/budget/BudgetGuard.java` | ★ 预算闸门（48 行） |
| `src/main/java/com/qqbot/proactive/ProactiveService.java` | 主动消息策略（24KB，与 `banter.py` 可对照） |
| `docs/STATUS.md` | 已知限制（单机 3.6G / 安全上限 3 个账号等） |
| `docs/multi-account-design.md` | 多账号隔离设计 |

### 附录 C：本项目 LLM 入口（若走"双 bot 交棒"路线，改这些）

| 入口 | 位置 |
|---|---|
| 门禁（三道） | `__init__.py:335` `_llm_gate()` |
| 统一答题出口 | `__init__.py:685` `_llm_answer()` |
| ① @ 机器人 | `__init__.py:765` `_try_llm_reply()` ← `handle_at:877` |
| ② 戳一戳 | `__init__.py:891/900` `_poked_me()` / `handle_poke()` |
| ③ 非音乐链接 | `__init__.py:1294` `_reply_llm_link()` |
| 主动搭话 | `banter.py:303` `llm_bot.complete()` |
| 后台复用 `complete()` 的四处 | `digest.py:130`、`persona.py:198/447`、`style.py:437`、`tools.py:168` |

### 附录 D：一手来源

- 仓库：<https://github.com/Anran3366/Warm-Isle>
- 镜像：<https://gitee.com/menran/warm_isle>
- 许可：AGPL-3.0（`LICENSE`，34KB 全文）
- 本项目对照基线：`E:\0\qq-music-collector`（HEAD 当时的 `src/plugins/music_collector/`）

---

## 七、建议与下一步

| 档 | 内容 | 工作量 | 风险 |
|---|---|---|---|
| **A（推荐）** | 只移植 §4.1 记忆检索打分 | 1 个文件 + 回归测试 | 低（失败可回退旧排序） |
| **B** | A + §4.2 TimeGuard + §4.3 预算闸门 | 2~3 个文件 | 低-中（TimeGuard 多一次暖调用） |
| **C** | B + §4.4 拆条 + §4.5 间隔提示 + §4.6 提示词重排 | 4~5 个文件 + 后台开关 | 中（涉及发送链路，需单群灰度） |
| ❌ | 换成 Warm-Isle（任一种形态） | 数天~数月 | 高（丢音乐域 / 记忆不迁移 / AGPL） |

**我的判断**：Warm-Isle 的可借鉴价值集中在 `docs/memory-design.md` 和 `docs/prompt-changelog.md` 两份文档，代码只有 `MemoryRecall` 和 `ReplyComposer` 值得看。它的技术选型（Java + Spring + Docker 多容器）与本项目（Python 单进程 systemd）是两条路，**不要为了 480 行 LLM 代码搬进 21,714 行 Java 和一套 Docker 编排**。

---

## 八、后续文档

- `docs/聊天体验拼装设计.md` — 把本文结论与第二个候选（qq-companion-bot，Python / MIT）合并成一份可执行的分期设计稿：分层分工表、4 处冲突裁决、P1~P5 阶段卡（含验收标准与回滚开关）、参数表、许可合规、明确不做清单。
