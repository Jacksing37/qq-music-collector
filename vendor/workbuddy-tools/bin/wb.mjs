#!/usr/bin/env node
/**
 * wb.mjs —— 两个工具的统一 CLI。
 *
 *   wb search "query" [--allowed d1 d2] [--blocked d3] [--freshness m6] [--backend bing|baidu|codebuddy] [--json]
 *   wb view <abs-path> --ask "问题" [--mode direct|relay|omit|auto] [--json]
 *   wb schema [view|search]        # 打印工具定义（可直接喂给任意 agent 框架）
 *   wb selftest                    # 行为一致性自检
 */
import { WebSearchTool, WebSearchSchema, createBlocklistChecker, validateParams as validateSearch } from '../src/webSearch.mjs';
import { ViewImageTool, ViewImageSchema, validateParams as validateView } from '../src/viewImage.mjs';
import { resolveLocalProvider } from '../src/providers.mjs';
import { WEB_SEARCH_TOOL_DESCRIPTION, VIEW_IMAGE_TOOL_DESCRIPTION } from '../src/constants.mjs';

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const vals = [];
      while (i + 1 < argv.length && !argv[i + 1].startsWith('--')) { vals.push(argv[++i]); }
      out[key] = vals.length === 0 ? true : vals.length === 1 ? vals[0] : vals;
    } else if (a === '-h') {
      out.help = true;
    } else {
      out._.push(a);
    }
  }
  return out;
}

const asArray = (v) => (v === undefined ? undefined : Array.isArray(v) ? v : [v]);

async function cmdSearch(args) {
  if (!args._[0]) throw new Error('usage: wb search "query"');
  const useBackend = args.backend ?? (process.env.WB_BACKEND ?? 'auto');
  const co = (useBackend === 'codebuddy' || (useBackend === 'auto' && process.env.WB_TOKEN && process.env.WB_ENDPOINT))
    ? { endpoint: process.env.WB_ENDPOINT, token: process.env.WB_TOKEN }
    : { provider: resolveLocalProvider(useBackend === 'auto' ? undefined : useBackend) };

  const tool = new WebSearchTool({
    ...co,
    threatChecker: createBlocklistChecker(
      (process.env.WB_THREAT_DOMAINS ?? '').split(',').filter(Boolean),
    ),
  });

  const params = {
    query: args._[0],
    ...(args.allowed ? { allowed_domains: asArray(args.allowed) } : {}),
    ...(args.blocked ? { blocked_domains: asArray(args.blocked) } : {}),
    ...(args.freshness ? { freshness: args.freshness } : {}),
  };

  const res = await tool.execute(params, {});
  if (args.json) {
    console.log(JSON.stringify({ title: res.title, rawResponse: res.rawResponse }, null, 2));
    return;
  }
  console.log(res.content);
}

async function cmdView(args) {
  const p = args._[0];
  if (!p) throw new Error('usage: wb view <absolute-image-path> --ask "question"');
  const tool = new ViewImageTool({ mode: args.mode ?? process.env.WB_VIEWIMAGE_MODE ?? 'auto' });
  const res = await tool.execute({ image_path: p, query: args.ask ?? 'describe the image' }, {});
  if (args.json) {
    const imageBlocks = Array.isArray(res.content) ? res.content.map((c) => ({ type: c.type, bytes: c.image?.length })) : undefined;
    console.log(JSON.stringify({ title: res.title, error: res.error, content: imageBlocks ?? res.content, rawResponse: res.rawResponse }, null, 2));
    return;
  }
  if (Array.isArray(res.content)) {
    console.log(`[direct] 已组装 image block，base64 长度 ${res.content[0].image.length}（宿主多模态模型可直接看图）`);
    console.log(`[meta] ${JSON.stringify(res.meta ?? {})}`);
    return;
  }
  console.log(res.error ?? res.content);
}

function cmdSchema(which) {
  const mk = (name, description, parameters) => ({ name, description, parameters });
  const all = [
    mk('ViewImage', VIEW_IMAGE_TOOL_DESCRIPTION.trim(), ViewImageSchema),
    mk('WebSearch', WEB_SEARCH_TOOL_DESCRIPTION.trim(), WebSearchSchema),
  ];
  const pick = which ? all.filter((t) => t.name.toLowerCase() === which.toLowerCase()) : all;
  console.log(JSON.stringify(pick.length ? pick : all, null, 2));
}

async function cmdSelftest() {
  const cases = [];
  const record = (name, ok, detail) => cases.push({ name, ok, detail });

  // 本项目补丁（见 ../PATCHES.md）：原版这里写死 `D:/...` 盘符，而 POSIX 上
  // `path.isAbsolute('D:/a.jpg')` 是 false，selftest 会在第 2 个用例就抛错退出（Linux 服务器上踩到）。
  // 改成按平台算绝对路径，Windows / Linux 都能跑。
  const path = await import('node:path');
  const absImg = path.resolve('a.jpg');
  // execute() 内部会 path.resolve() 一次，这里先解析好，断言才能逐字对上（Windows 会补盘符）
  const missingImg = path.resolve(path.join(path.sep, 'definitely', 'not', 'here.png'));
  const badExtImg = path.resolve('a.txt');

  // 1) ViewImage 参数校验
  record('view: 相对路径被拒绝',
    validateView({ image_path: 'rel/a.jpg', query: 'x' })?.includes('absolute') === true,
    validateView({ image_path: 'rel/a.jpg', query: 'x' }));
  record('view: 空 query 被拒绝',
    validateView({ image_path: absImg, query: '  ' })?.includes('non-empty') === true,
    validateView({ image_path: absImg, query: '  ' }));

  // 2) ViewImage 错误矩阵
  const tool = new ViewImageTool({ mode: 'direct' });
  const t1 = await tool.execute({ image_path: missingImg, query: 'x' }, {});
  record('view: 不存在文件 -> file not found',
    t1.error === `Error: file not found: ${missingImg}`, t1.error);

  const t2 = await tool.execute({ image_path: badExtImg, query: 'x' }, {});
  record('view: 非法扩展名 -> unsupported format',
    t2.error?.startsWith('Error: unsupported format'), t2.error);

  const omitTool = new ViewImageTool({ mode: 'omit' });
  // 造一张 1x1 PNG：omit 分支在 stat 之后，必须先存在真实文件
  const { writeFileSync, mkdtempSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const probeDir = mkdtempSync(path.join(tmpdir(), 'wb-selftest-'));
  const probePng = path.join(probeDir, 'probe.png');
  writeFileSync(probePng, Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    'base64'));
  const t3 = await omitTool.execute({ image_path: probePng, query: 'x' }, {}).catch((e) => ({ error: e.message }));
  record('view: omit 模式 -> 明确拒绝', String(t3.error ?? '').includes('cannot receive images'), t3.error);

  // 3) WebSearch 参数校验
  record('search: 空 query 被拒', validateSearch({ query: '' }) === "The 'query' parameter cannot be empty.", validateSearch({ query: '' }));
  record('search: 单字符被拒', validateSearch({ query: 'a' }) === "The 'query' parameter must be at least 2 characters long.", validateSearch({ query: 'a' }));
  record('search: freshness d31 被拒', validateSearch({ query: 'ab', freshness: 'd31' })?.includes('freshness'), validateSearch({ query: 'ab', freshness: 'd31' }));
  record('search: freshness m12 通过', validateSearch({ query: 'ab', freshness: 'm12' }) === undefined, validateSearch({ query: 'ab', freshness: 'm12' }));

  // 4) 无凭据时应报 Authentication required
  const noAuth = new WebSearchTool({});
  const t4 = await noAuth.execute({ query: 'test query' }, {}).catch((e) => ({ message: e.message }));
  record('search: 无凭据 -> Authentication required', String(t4.message ?? '').includes('Authentication required'), t4.message);

  const failed = cases.filter((c) => !c.ok);
  for (const c of cases) console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.ok ? '' : `\n      got: ${c.detail}`}`);
  console.log(`\n${cases.length - failed.length}/${cases.length} passed`);
  if (failed.length) process.exitCode = 1;
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const args = parseArgs(rest);
  if (!cmd || args.help) {
    console.log('wb <search|view|schema|selftest> [args]');
    return;
  }
  if (cmd === 'search') return cmdSearch(args);
  if (cmd === 'view') return cmdView(args);
  if (cmd === 'schema') return cmdSchema(args._[0]);
  if (cmd === 'selftest') return cmdSelftest();
  throw new Error(`unknown command: ${cmd}`);
}

main().catch((e) => {
  console.error(e.message);
  process.exitCode = 1;
});
