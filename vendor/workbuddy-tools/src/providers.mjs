/**
 * providers.mjs —— 当没有 WorkBuddy 后端凭据时的本地搜索后端。
 *
 * 注意：这两个后端只是"能跑"的兜底，**不是**真实 /agenttool/v1/search 的替代品。
 * 真实后端带 query 改写、多源聚合、正文抽取（snippet 常是整段正文），
 * 裸 SERP 对中日文长 query 会按 token 切碎，质量差距明显。
 */
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';

// 本项目补丁（见 ../PATCHES.md）：原版只认 6 个实体，实测 Bing 摘要里的
// `&ensp;&#0183;&ensp;` 会原样进模型。这里对齐项目 websearch.decode_entities 的表。
function decodeEntities(s) {
  const body = String(s ?? '')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&nbsp;/g, ' ').replace(/&ensp;/g, ' ').replace(/&emsp;/g, ' ')
    .replace(/&hellip;/g, '…').replace(/&mdash;/g, '—').replace(/&ndash;/g, '–');
  // 数字实体放最后：标题和摘要里都常见
  return body
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
    .replace(/\s+/g, ' ')
    .trim();
}

function parseDomainQuery(body) {
  // provider 只拿到拼接后的 query（含 -site: 后缀），站点过滤交给后端参数更准
  return { query: body.query, allowed: body.allowed_domains ?? [], freshness: body.freshness };
}

async function fetchText(url, timeoutMs = 15000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      headers: { 'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8' },
      signal: ctrl.signal,
    });
    const buf = Buffer.from(await res.arrayBuffer());
    const ct = res.headers.get('content-type') || '';
    if (/charset=gb/i.test(ct) || /gb2312|gbk/i.test(ct)) {
      // 极简 GBK 处理：交给 TextDecoder
      return new TextDecoder('gb18030').decode(buf);
    }
    return buf.toString('utf8');
  } finally {
    clearTimeout(t);
  }
}

async function resolveRedirect(url, timeoutMs = 8000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      redirect: 'follow',
      headers: { 'User-Agent': UA },
      signal: ctrl.signal,
    });
    return res.url && !res.url.includes('baidu.com/link') ? res.url : undefined;
  } catch {
    return undefined;
  } finally {
    clearTimeout(t);
  }
}

export const bingProvider = {  id: 'bing',
  async search(body) {
    const started = Date.now();
    const { query, allowed, freshness } = parseDomainQuery(body);
    let url = `https://www.bing.com/search?q=${encodeURIComponent(query)}&count=10&setlang=zh-CN`;
    if (freshness) {
      const n = Number.parseInt(freshness.slice(1) || '1', 10);
      const ez = n <= 1 ? 'ez1' : n <= 7 ? 'ez2' : 'ez3';
      url += `&filters=${encodeURIComponent(`ex1:"${ez}"`)}`;
    }
    const html = await fetchText(url);
    const results = [];
    const blocks = html.split('<li class="b_algo"').slice(1);
    for (const raw of blocks) {
      // 关键：split 出来的片段会跨多个结果，必须截到本条的 </li>，
      // 否则 <h2> 会串到侧栏的推广链接上去（实测会抓到抖音/公众号条目）
      const block = raw.split('</li>')[0];
      const m = /<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/.exec(block);
      if (!m) continue;
      let link = m[1].replace(/&amp;/g, '&');
      const title = decodeEntities(m[2]);
      const sm = /<p[^>]*>([\s\S]*?)<\/p>/.exec(block) || /class="b_caption"[\s\S]*?(<div[\s\S]*?<\/div>)/.exec(block);
      const snippet = sm ? decodeEntities(sm[1]).slice(0, 300) : '';
      if (link.includes('bing.com/ck/a')) {
        try {
          const u = new URL(link).searchParams.get('u');
          if (u && u.startsWith('a1')) link = Buffer.from(u.slice(2), 'base64url').toString('utf8');
        } catch { /* keep */ }
      }
      results.push({ title, url: link, snippet });
    }
    const hostOk = (u) => {
      if (!allowed.length) return true;
      try {
        const h = new URL(u).hostname.toLowerCase();
        return allowed.some((d) => h === d || h.endsWith(`.${d}`));
      } catch { return false; }
    };
    const filtered = results.filter((r) => hostOk(r.url)).slice(0, body.max_results ?? 5);
    return {
      provider: 'bing',
      results: filtered,
      total_results: filtered.length,
      response_time_ms: Date.now() - started,
    };
  },
};

export const baiduProvider = {
  id: 'baidu',
  async search(body) {
    const started = Date.now();
    const q = body.query;
    let url = `https://www.baidu.com/s?wd=${encodeURIComponent(q)}&rn=10`;
    if (body.freshness) {
      const n = Number.parseInt(body.freshness.slice(1) || '1', 10);
      const unit = { d: 86400, m: 2592000, y: 31536000 }[body.freshness[0]];
      const now = Math.floor(Date.now() / 1000);
      url += `&gpc=stf%3D${now - n * unit}%2C${now}%7Cstftype%3D1`;
    }
    const html = await fetchText(url);
    const results = [];
    const blocks = html.split(/<div[^>]+class="result[^"]*"/).slice(1);
    for (const block of blocks) {
      const m = /<h3[^>]*>[\s\S]*?<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/.exec(block);
      if (!m) continue;
      const sm = /class="(?:content-right_|c-abstract)[^"]*"[^>]*>([\s\S]*?)<\/span>/.exec(block)
        || /<span[^>]*class="[^"]*(?:abstract|content-right)[^"]*"[^>]*>([\s\S]*?)<\/span>/.exec(block);
      results.push({
        title: decodeEntities(m[2]),
        url: m[1],
        snippet: sm ? decodeEntities(sm[1]).slice(0, 300) : '',
      });
    }
    // 百度给的是 /link?url=... 跳转链，还原成真实地址（失败则保留原链）
    await Promise.all(results.map(async (r) => {
      if (!r.url.includes('baidu.com/link')) return;
      const real = await resolveRedirect(r.url);
      if (real) r.url = real;
    }));
    const allowed = body.allowed_domains ?? [];
    const filtered = results
      .filter((r) => {
        if (!allowed.length) return true;
        try {
          const h = new URL(r.url).hostname.toLowerCase();
          return allowed.some((d) => h === d || h.endsWith(`.${d}`));
        } catch { return false; }
      })
      .slice(0, body.max_results ?? 5);
    return {
      provider: 'baidu',
      results: filtered,
      total_results: filtered.length,
      response_time_ms: Date.now() - started,
    };
  },
};

export function resolveLocalProvider(name = process.env.WB_SEARCH_BACKEND || 'bing') {
  if (name === 'baidu') return baiduProvider;
  if (name === 'none') return { id: 'none', async search() { return { provider: 'none', results: [], total_results: 0, response_time_ms: 0 }; } };
  return bingProvider;
}
