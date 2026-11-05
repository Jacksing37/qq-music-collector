/**
 * viewImage.mjs —— ViewImage 工具的完整重建。
 *
 * 与源码对齐的行为（逐条对应 shared.cjs 中的 mM 类）:
 *   - 参数校验: image_path 必须是绝对路径且非空; query 必须非空
 *   - 校验顺序: 路径 -> 扩展名 -> stat -> isFile -> 20MB -> 模型能力 -> 预算 -> 读取 -> 压缩
 *   - 失败返回 {content, error, rawResponse:{is_error:true, tool_error_code}}
 *   - 成功返回 {title:"View image <basename>", content, renderer:{type:'text'}}
 *   - relay 缓存: key = sha256(bytes)+query+modelId+endpoint+压缩档位, LRU 200
 *   - 三种模型能力: direct(图像直接回灌) / relay(转交识图模型) / omit(拒绝并报错)
 */
import { createHash } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {
  VIEW_IMAGE_MAX_BYTES, VIEW_IMAGE_RELAY_CACHE_MAX, VIEW_IMAGE_SUPPORTED_EXTS,
  VIEW_IMAGE_DESCRIBER_INSTRUCTIONS, VISION_UNAVAILABLE_MESSAGE,
  IMAGE_TOO_LARGE_AFTER_COMPRESSION, getImageCompressionMaxDimension,
} from './constants.mjs';
import {
  detectMimeFromMagicBytes, isViewImageSupportedFormat, compressUserImage, dimensions,
} from './imageCodec.mjs';

export const ViewImageErrorCode = {
  InvalidParams: 'ViewImageInvalidParamsError',
  UnsupportedFormat: 'ViewImageUnsupportedFormatError',
  FileNotFound: 'ViewImageFileNotFoundError',
  TooLarge: 'ViewImageTooLargeError',
  VisionUnavailable: 'ViewImageVisionUnavailableError',
  BudgetExceeded: 'ViewImageBudgetExceededError',
};

export const ViewImageSchema = {
  type: 'object',
  properties: {
    image_path: {
      type: 'string',
      description: 'Absolute path to the image file to look at.',
      minLength: 1,
      mustBeAbsolute: true,
    },
    query: {
      type: 'string',
      description: 'The specific information to extract from the image. Must be self-contained: ' +
        'the vision model sees only the image and this query, not the conversation.',
      minLength: 1,
    },
  },
  required: ['image_path', 'query'],
};

// ---------- 校验（等价 ZodUtils.validate） ----------
export function validateParams(params) {
  if (!params || typeof params !== 'object') return 'Invalid input: expected object';
  const { image_path, query } = params;
  if (typeof image_path !== 'string' || image_path.trim().length === 0) {
    return 'image_path: An absolute image path is required';
  }
  if (!path.isAbsolute(image_path)) return 'image_path: An absolute image path is required';
  if (typeof query !== 'string' || query.trim().length === 0) return 'query: A non-empty query is required';
  return undefined;
}

function expandHome(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

/** 图片读取预算：真实实现由 imageBlobService 按"一次运行"计费 */
class ImageBudget {
  constructor(limitBytes = Number(process.env.WB_IMAGE_BUDGET_MB ?? 32) * 1024 * 1024) {
    this.limit = limitBytes;
    this.used = 0;
  }
  tryReserve(bytes) {
    if (this.used + bytes > this.limit) return false;
    this.used += bytes;
    return true;
  }
}

const defaultBudget = new ImageBudget();

// ---------- 识图后端（relay 路径） ----------
async function describeWithVisionModel({ dataUrl, query, model }) {
  const baseUrl = (process.env.WB_VISION_BASE_URL || 'https://api.openai.com/v1').replace(/\/+$/, '');
  const apiKey = process.env.WB_VISION_API_KEY || process.env.OPENAI_API_KEY;
  const modelId = model || process.env.WB_VISION_MODEL || 'gpt-4o-mini';
  if (!apiKey) {
    const err = new Error('no vision credentials (set WB_VISION_API_KEY or OPENAI_API_KEY)');
    err.code = 'NO_CREDENTIALS';
    throw err;
  }
  const body = {
    model: modelId,
    max_tokens: 1024,
    messages: [
      { role: 'system', content: VIEW_IMAGE_DESCRIBER_INSTRUCTIONS.trim() },
      {
        role: 'user',
        content: [
          { type: 'text', text: query },
          { type: 'image_url', image_url: { url: dataUrl } },
        ],
      },
    ],
  };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), Number(process.env.WB_VISION_TIMEOUT_MS ?? 90_000));
  try {
    const res = await fetch(`${baseUrl}/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`vision API ${res.status} ${res.statusText}`);
    const json = await res.json();
    return (json.choices?.[0]?.message?.content ?? '').trim();
  } finally {
    clearTimeout(timer);
  }
}

// ---------- 工具 ----------
export class ViewImageTool {
  constructor(opts = {}) {
    this.name = 'ViewImage';
    this.parameters = ViewImageSchema;
    this.permissionRestrictionNames = ['Read'];
    this.argumentRenderer = { displayProperties: [{ type: 'path', name: 'image_path' }] };
    this.relayCaches = new WeakMap();
    /** direct | relay | omit | auto（auto: 有视觉凭据则 relay，否则 direct 交付 payload） */
    this.mode = opts.mode ?? (process.env.WB_VIEWIMAGE_MODE || 'auto');
    this.model = opts.model ?? process.env.WB_VISION_MODEL ?? 'default';
    this.endpoint = opts.endpoint ?? '';
    this.budget = opts.budget ?? defaultBudget;
    this.logger = opts.logger ?? { info: () => {}, warn: (...a) => console.warn(...a) };
  }

  resolveToolType() { return 'Read'; }
  resolveNeedPermissionArgs(_ctx, params) { return [{ type: 'FilePath', value: path.resolve(expandHome(params.image_path)) }]; }

  _relayCache(session) {
    let perSession = this.relayCaches.get(session);
    if (!perSession) { perSession = new Map(); this.relayCaches.set(session, perSession); }
    return perSession;
  }

  _textResult(filePath, content) {
    return {
      title: `View image ${path.basename(filePath)}`,
      content,
      renderer: { type: 'text' },
    };
  }

  _errorResult(content, code) {
    return { content, error: content, rawResponse: { is_error: true, tool_error_code: code } };
  }

  async execute(params, ctx = {}) {
    const invalid = validateParams(params);
    if (invalid) throw new Error(`ViewImage error: Invalid parameters. ${invalid}`);

    ctx.abortSignal?.throwIfAborted?.();

    const filePath = path.resolve(expandHome(params.image_path));
    const ext = path.extname(filePath).toLowerCase();
    if (!isViewImageSupportedFormat(ext)) {
      return this._errorResult(`Error: unsupported format: ${ext || '(no extension)'}`, ViewImageErrorCode.UnsupportedFormat);
    }

    let st;
    try { st = await fs.stat(filePath); }
    catch { return this._errorResult(`Error: file not found: ${filePath}`, ViewImageErrorCode.FileNotFound); }
    if (!st.isFile()) return this._errorResult(`Error: not a file: ${filePath}`, ViewImageErrorCode.FileNotFound);
    if (st.size > VIEW_IMAGE_MAX_BYTES) return this._errorResult('Error: image exceeds 20MB', ViewImageErrorCode.TooLarge);

    // 模型能力: direct / relay / omit
    let mode = this.mode;
    if (mode === 'auto') mode = (process.env.WB_VISION_API_KEY || process.env.OPENAI_API_KEY) ? 'relay' : 'direct';
    if (mode === 'omit') {
      return this._errorResult(
        'Error: the selected model cannot receive images. No image was sent to another model.',
        ViewImageErrorCode.VisionUnavailable,
      );
    }

    const reserve = Math.max(st.size, VIEW_IMAGE_MAX_BYTES);
    if (!this.budget.tryReserve(reserve)) {
      const mb = (this.budget.limit / 1024 / 1024).toFixed(0);
      return this._errorResult(
        `Error: this run's image read budget is exhausted (${mb}MB). Continue in a new message to read more images.`,
        ViewImageErrorCode.BudgetExceeded,
      );
    }

    const bytes = await fs.readFile(filePath);
    if (bytes.length > VIEW_IMAGE_MAX_BYTES) {
      return this._errorResult('Error: image exceeds 20MB', ViewImageErrorCode.TooLarge);
    }
    const mime = detectMimeFromMagicBytes(bytes);
    if (!mime) {
      return this._errorResult(`Error: not a recognized image file: ${filePath}`, ViewImageErrorCode.UnsupportedFormat);
    }

    let dataUrl = `data:${mime};base64,${bytes.toString('base64')}`;
    dataUrl = await compressUserImage(dataUrl);
    if (!dataUrl) {
      return this._errorResult(IMAGE_TOO_LARGE_AFTER_COMPRESSION, ViewImageErrorCode.TooLarge);
    }

    if (mode === 'direct') {
      // 宿主是多模态模型：图像块直接回灌，不落文字
      return {
        title: `View image ${path.basename(filePath)}`,
        content: [{ type: 'input_image', image: dataUrl }],
        renderer: { type: 'image' },
        meta: { dim: dimensions(bytes), mime },
      };
    }

    // relay：带缓存地调用识图模型，只回文字
    const cache = this._relayCache(ctx.session ?? {});
    const key = JSON.stringify([
      createHash('sha256').update(bytes).digest('hex'),
      params.query,
      this.model,
      this.endpoint,
      getImageCompressionMaxDimension(),
    ]);
    if (cache.has(key)) {
      this.logger.info('ViewImage: relay cache hit');
      return this._textResult(filePath, cache.get(key));
    }
    this.logger.info('ViewImage: relay cache miss');

    try {
      const text = await describeWithVisionModel({ dataUrl, query: params.query, model: this.model });
      if (!text) return this._errorResult(VISION_UNAVAILABLE_MESSAGE, ViewImageErrorCode.VisionUnavailable);
      cache.set(key, text);
      if (cache.size > VIEW_IMAGE_RELAY_CACHE_MAX) cache.delete(cache.keys().next().value);
      return this._textResult(filePath, text);
    } catch (e) {
      this.logger.warn(`ViewImage vision call failed: ${e?.message ?? e}`);
      return this._errorResult(VISION_UNAVAILABLE_MESSAGE, ViewImageErrorCode.VisionUnavailable);
    }
  }
}

export { VIEW_IMAGE_SUPPORTED_EXTS };
