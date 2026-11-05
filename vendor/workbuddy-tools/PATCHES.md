# vendor/workbuddy-tools —— 来源与本项目补丁

## 来源

WorkBuddy（`@genie/agent-harness`）里 `WebSearch` / `ViewImage` 两个 Agent 工具的
行为等价重实现，原样拷自：

```
D:\0\WorkBuddy\2026-11-05-14-09-17\workbuddy-tools
```

拷进来的只有能跑的代码（`bin/` + `src/` + `package.json`）。原件里的
`PLAYBOOK.md` / `REVERSE.md` / `README.md` / `_query.png` 是文档和素材，没拷。

## 谁在用它

只有 `src/plugins/music_collector/websearch.py` 的 `_search_wb` 会调：

```
node vendor/workbuddy-tools/bin/wb.mjs search "<query>" --json
```

`ViewImage` 没接（项目自己的图片走 `llm.vision` + `ToolContext.images`）。

## 本项目补丁（3 处，其余保持原样）

### 1. `src/constants.mjs` —— 条数可由环境变量控制

原版 `WEB_SEARCH_MAX_RESULTS` 写死 5，项目的 `llm.search_max` 就没法生效了。
改成读 `WB_MAX_RESULTS`（缺省仍 5，夹在 1~20），Python 侧按 `search_max` 传进来。

### 2. `src/providers.mjs` —— 实体解码补齐

原版 `decodeEntities` 只认 6 个实体，实测 Bing 摘要里 `&ensp;&#0183;&ensp;` 会原样
进模型（5 条结果里 12 处）。这里对齐项目 `websearch.decode_entities` 的表：补
`&ensp;` / `&emsp;` / `&hellip;` / `&mdash;` / `&ndash;` / `&#x27;` 和数字实体。

### 3. `bin/wb.mjs` —— selftest 不再写死 Windows 盘符

原版自检里写死 `D:/a.jpg` 这类路径，而 POSIX 上 `path.isAbsolute('D:/a.jpg')` 是
**false**，于是 Linux 上跑 `selftest` 会在第 2 个用例直接抛
`image_path: An absolute image path is required` 然后 `exit 1`（线上验收时踩到，
白排查了两轮）。改成用 `path.resolve()` 现算绝对路径，两个平台都是 10/10。

## 升级上游时怎么办

重新拷 `bin/` + `src/`，**再把上面三处补丁重打一遍**，然后跑：

```
node vendor/workbuddy-tools/bin/wb.mjs selftest     # 应该 10/10 passed（Windows / Linux 都行）
python tests/test_websearch.py                      # 第 10 段覆盖 wb 引擎
```

## 依赖

- Node ≥ 18（用了全局 `fetch`）。本机调试可用 WorkBuddy 自带的
  `C:\Users\CaiLaoM\.workbuddy\binaries\node\versions\22.22.2-6\node.exe`，
  配 `llm.search_node` 指过去即可；服务器上用 PATH 里的 `node` 就留空。
- 不需要 `WB_ENDPOINT` / `WB_TOKEN`：没有凭据时 `--backend auto` 落本地
  `bing` provider，照样能搜（`WB_SEARCH_BACKEND` 可切 `baidu`，实测失效）。
- 原版的 `npm install` 不用做：零依赖。
