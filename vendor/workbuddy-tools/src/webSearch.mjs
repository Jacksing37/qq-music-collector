/**
 * webSearch.mjs —— WebSearch 工具的完整重建。
 *
 * 与源码对齐（shared.cjs 中 pF 类）:
 *   - 端点: `${productConfig.endpoint}/agenttool/v1/search`，POST，20s 超时
 *   - 请求体: { query: `${query}${blockedSuffix}`, type:"text2text", max_results:5,
 *              allowed_domains?, freshness? }
 *     blockedSuffix = ` (${blocked.map(d=>`-site:${d}`).join(' ')})`
 *   - 响应: { provider, results:[{title,url,snippet,site?,favicon?}], total_results, response_time_ms, code?, msg? }
 *   - 安全过滤: 只留 http/https；威胁域名命中则丢弃该条；favicon 单独复检
 *   - 输出格式: "# Search Results for" + "## n. [title](url)" + "**URL:**" + 尾部指纹行
 */
import {
  WEB_SEARCH_API_PATH, WEB_SEARCH_TIMEOUT_MS, WEB_SEARCH_MAX_RESULTS, FRESHNESS_RE,
} from './constants.mjs';

export const WebSearchSchema = {
  type: 'object',
  properties: {
    query: { type: 'string', minLength: 2, description: 'The search query to use' },
    allowed_domains: { type: 'array', items: { type: 'string' }, description: 'Only include search results from these domains.' },
    blocked_domains: { type: 'array', items: { type: 'string' }, description: 'Never include search results from these domains' },
    freshness: { type: 'string', pattern: FRESHNESS_RE.source, description: 'Filter search results by time range. dN / mN / yN.' },
  },
  required: ['query'],
};

export function validateParams(params) {
  if (!params || typeof params !== 'object') return 'Invalid input';
  const q = params.query;
  if (q === undefined || q === null || String(q).trim() === '') return "The 'query' parameter cannot be empty.";
  if (String(q).length < 2) return "The 'query' parameter must be at least 2 characters long.";
  if (params.freshness !== undefined && !FRESHNESS_RE.test(params.freshness)) {
    return 'freshness must be d or d1-d30, m or m1-m12, or y or y1-y5';
  }
  for (const k of ['allowed_domains', 'blocked_domains']) {
    if (params[k] !== undefined && !Array.isArray(params[k])) return `${k} must be an array of strings`;
  }
  return undefined;
}

export function buildRequestBody({ query, allowed_domains, blocked_domains, freshness }) {
  const blockedSuffix = blocked_domains?.length
    ? ` (${blocked_domains.map((d) => `-site:${d}`).join(' ')})`
    : '';
  return {
    query: `${query}${blockedSuffix}`,
    type: 'text2text',
    max_results: WEB_SEARCH_MAX_RESULTS,
    ...(allowed_domains?.length ? { allowed_domains } : {}),
    ...(freshness ? { freshness } : {}),
  };
}

/** 威胁域名检查（真实实现是本地威胁库，这里做成可注入） */
export async function isUnsafeUrl(url, checker) {
  let u;
  try { u = new URL(url); } catch { return true; }
  if (!['http:', 'https:'].includes(u.protocol)) return true;
  const host = u.hostname.toLowerCase().replace(/\.$/, '');
  if (!host) return false;
  // 源码: IP 直连跳过威胁库查询
  if (/^[\d.]+$/.test(host) || host.includes(':')) return false;
  if (!checker) return false;
  return checker.isMalicious(host);
}

export async function filterUnsafeResults(result, ctx = {}, checker) {
  const checked = await Promise.all((result.results ?? []).map(async (r) => {
    if (r.url !== '' && await isUnsafeUrl(r.url, checker)) return undefined;
    if (r.favicon && await isUnsafeUrl(r.favicon, checker)) {
      const copy = { ...r };
      delete copy.favicon;
      return copy;
    }
    return r;
  }));
  const results = checked.filter((r) => r !== undefined);
  return {
    ...result,
    results,
    total_results: results.length === result.results.length ? result.total_results : results.length,
  };
}

/** 逐字还原源码 formatSearchResults */
export function formatSearchResults(results, query, provider) {
  const body = results.map((r, i) =>
    `## ${i + 1}. [${r.title}](${r.url})\n\n${r.snippet}\n\n**URL:** ${r.url}\n\n---`).join('\n\n');
  const n = results.length;
  return `# Search Results for "${query}"\n\n${body}\n\n*Found ${n} result${n !== 1 ? 's' : ''} \u2022 Provider: ${provider} \u2022 Search performed on ${new Date().toISOString().split('T')[0]}*`;
}

export class WebSearchTool {
  /**
   * @param {object} opts
   * @param {string} opts.endpoint   服务端 base url（codebuddy 后端必填）
   * @param {string} opts.token      Bearer token（codebuddy 后端必填）
   * @param {object} opts.provider   兜底 provider: { search(body) -> normalized, id }
   * @param {object} opts.threatChecker { isMalicious(host) -> bool|Promise<bool> }
   */
  constructor(opts = {}) {
    this.name = 'WebSearch';
    this.parameters = WebSearchSchema;
    this.aliases = ['read'];
    this.argumentRenderer = { displayProperties: [{ name: 'query' }] };
    this.endpoint = opts.endpoint ?? process.env.WB_ENDPOINT ?? '';
    this.token = opts.token ?? process.env.WB_TOKEN ?? '';
    this.provider = opts.provider;
    this.threatChecker = opts.threatChecker;
    this.headersExtra = opts.headersExtra ?? safeJson(process.env.WB_EXTRA_HEADERS);
  }

  isConcurrencySafe() { return true; }

  async execute(params, ctx = {}) {
    const invalid = validateParams(params);
    if (invalid) throw new Error(`Error: Invalid parameters provided. Reason: ${invalid}`);
    try {
      const raw = await this.performWebSearch(params, ctx);
      const safe = await filterUnsafeResults(raw, ctx, this.threatChecker);
      if (!safe?.results?.length) {
        const hadAny = (raw?.results?.length ?? 0) > 0;
        return {
          content: `${hadAny ? 'No safe search results' : 'No search results'} found for query: "${params.query}"`,
          title: `${hadAny ? 'No safe results' : 'No results'} for "${params.query}"`,
          renderer: { type: 'text' },
          rawResponse: this.buildStructuredResult(params.query, safe),
        };
      }
      return {
        content: formatSearchResults(safe.results, params.query, safe.provider),
        title: `Found ${safe.results.length} results for "${params.query}" (provider: ${safe.provider})`,
        renderer: { type: 'text' },
        rawResponse: this.buildStructuredResult(params.query, safe),
      };
    } catch (e) {
      throw new Error(`Error: Error during web search for query "${params.query}": ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  buildStructuredResult(query, r) {
    return {
      type: 'web_search_tool_result',
      query,
      searchType: 'text2text',
      provider: r?.provider ?? '',
      results: r?.results ?? [],
      totalResults: r?.total_results ?? 0,
      responseTimeMs: r?.response_time_ms ?? 0,
    };
  }

  /** 源码 performWebSearch：POST ${endpoint}/agenttool/v1/search */
  async performWebSearch(params, ctx = {}) {
    const body = buildRequestBody(params);

    if (!this.endpoint || !this.token) {
      if (!this.provider) {
        throw new Error('Authentication required for web search');
      }
      return this.provider.search(body);
    }

    const requestId = cryptoRandomId();
    const headers = {
      Accept: 'application/json',
      'Content-Type': 'application/json; charset=utf-8',
      'X-Requested-With': 'XMLHttpRequest',
      'User-Agent': `WorkBuddyTools/1.0.0`,
      Authorization: `Bearer ${this.token}`,
      'X-Request-Id': requestId,
      ...(ctx.headers ?? {}),
      ...(this.headersExtra ?? {}),
    };

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), WEB_SEARCH_TIMEOUT_MS);
    let res;
    try {
      res = await fetch(`${this.endpoint}${WEB_SEARCH_API_PATH}`, {
        method: 'POST', headers, body: JSON.stringify(body), signal: ctrl.signal,
      });
    } catch (e) {
      if (e?.name === 'AbortError') {
        throw new Error(`Web Search API request timed out after ${WEB_SEARCH_TIMEOUT_MS / 1000}s (requestId: ${requestId})`);
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      throw new Error(`Web Search API request failed with status ${res.status}: ${res.statusText} (requestId: ${requestId})`);
    }
    const json = await res.json();
    if (json.code) {
      throw new Error(`Web Search API returned error: ${json.msg || 'Unknown error'} (requestId: ${requestId})`);
    }
    return {
      provider: json.provider || '',
      results: (json.results || []).map((r) => ({
        title: r.title || 'Untitled',
        url: r.url || '',
        snippet: r.snippet || '',
        ...(r.site ? { site: r.site } : {}),
        ...(r.favicon ? { favicon: r.favicon } : {}),
      })),
      total_results: json.total_results || 0,
      response_time_ms: json.response_time_ms || 0,
    };
  }
}

function safeJson(s) {
  if (!s) return undefined;
  try { return JSON.parse(s); } catch { return undefined; }
}

function cryptoRandomId() {
  return Math.random().toString(16).slice(2) + Date.now().toString(16);
}

/** 本地威胁库（真实实现是随包下发的域名库） */
export function createBlocklistChecker(domains = []) {
  const set = new Set(domains.map((d) => d.toLowerCase()));
  return {
    isMalicious(host) {
      if (set.has(host)) return true;
      for (const d of set) if (host.endsWith(`.${d}`)) return true;
      return false;
    },
  };
}
