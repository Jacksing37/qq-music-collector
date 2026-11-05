/**
 * constants.mjs —— 从 WorkBuddy 安装目录还原出的全部常量。
 *
 * 证据位置（本机）:
 *   node_modules/@genie/agent-harness/cli/dist-server/shared.cjs        (工具实现)
 *   cli/dist/codebuddy-lite-wb.mjs                                      (同实现的 lite 构建, 含 ImageUtils)
 *   cli/product.json                                                    (agents[11] / prompts[])
 */

// ---------- ViewImage ----------
export const VIEW_IMAGE_MAX_BYTES = 0x1400000;            // 20MB, 源码 mI
export const VIEW_IMAGE_PROGRESS_INTERVAL_MS = 1000;      // 源码 mT
export const VIEW_IMAGE_RELAY_CACHE_MAX = 200;            // 源码 mx
export const VIEW_IMAGE_SUPPORTED_EXTS = ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'];

export const VIEW_IMAGE_QUERY_DESCRIPTION =
  'The specific information to extract from the image. Must be self-contained: ' +
  'the vision model sees only the image and this query, not the conversation.';

export const VIEW_IMAGE_TOOL_DESCRIPTION = `Read and inspect an image file. For multimodal models the image itself is returned; otherwise a vision model reads the image and returns a text answer.

Usage:
- image_path must be an absolute path to a local image file (.png, .jpg, .jpeg, .webp, .gif, .bmp).
- query states exactly what you need to know about the image. When a vision model processes the image, it receives ONLY the image and your query — it has no access to this conversation, so the query must be self-contained. Do not write "what's wrong here"; write "in this login page screenshot, identify any misaligned or overlapping elements".
- When the result is a text answer, any detail not present in the returned text is unavailable to you. Do not infer or invent details beyond the returned text.
- If the answer is incomplete or ambiguous, call again with a narrower query rather than guessing.
- This tool does not edit or generate images.
`;

/** relay 路径下识图子代理的系统提示词 (product.json: prompts[viewimage-describer-instructions]) */
export const VIEW_IMAGE_DESCRIBER_INSTRUCTIONS = `You are a vision assistant. You receive an image and a question about it, and you answer based only on what is visible in the image.

Rules:
- Answer the question directly and concisely.
- Describe only what the image shows. Do not speculate or invent details.
- If the requested information is not visible in the image, say so explicitly.
- Transcribe text exactly when asked to (error messages, labels, code, numbers).
`;

/** 识图失败时的占位文案：模型被告知"不要臆测内容" */
export const VISION_UNAVAILABLE_MESSAGE =
  'Error: vision model unavailable. The image was not read. Do not assume anything about its contents; ' +
  'tell the user the image could not be processed.';

// 源码里 image interceptor 用的两个占位符
export const IMAGE_OMITTED_NO_MODEL = '[image omitted: model does not support image input]';
export const IMAGE_OMITTED_NO_LOCAL_COPY =
  '[image omitted: no local copy is available for the current model; nothing was sent]';
export const IMAGE_TOO_LARGE_AFTER_COMPRESSION = '[Image too large — omitted after compression]';
export const IMAGE_PROCESSING_ERROR = '[Image omitted because it could not be processed]';

/** 图片被转存后回灌给主模型的提示（relay 模式下替换 image block） */
export function buildRelayedImageHint(blobPath) {
  return `[The image is stored at: ${blobPath}. Use the ViewImage tool with this path and a specific query to look at it. Do not tell the user you cannot see the image.]`;
}

// ---------- 压缩档位 ----------
export const DEFAULT_MAX_DIMENSION = 2000;               // 源码 M
export const SUPPORTED_MAX_DIMENSIONS = [0, 1080, 2000]; // 源码 N
export const COMPRESSION_DIM_ENV = 'CODEBUDDY_CODE_IMAGE_COMPRESSION_MAX_DIMENSION';
export const JPEG_MIME = 'image/jpeg';

/** 源码 L: 按 maxDimension 分档, 0 表示不压缩 */
export const COMPRESSION_PROFILES = {
  1080: { maxDimension: 1080, maxBase64Size: 512_000, maxRawBytes: 384_000, jpegQualities: [85, 70, 50, 30] },
  2000: { maxDimension: 2000, maxBase64Size: 5_242_880, maxRawBytes: 3_932_160, jpegQualities: [80, 60, 40, 20] },
};

export function getImageCompressionMaxDimension() {
  const raw = process.env[COMPRESSION_DIM_ENV];
  if (raw === undefined || raw === '') return DEFAULT_MAX_DIMENSION;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n >= 0 ? n : DEFAULT_MAX_DIMENSION;
}

export function getCurrentImageCompressionProfile(maxDim = getImageCompressionMaxDimension()) {
  if (maxDim === 0) return undefined;
  return COMPRESSION_PROFILES[maxDim] ?? COMPRESSION_PROFILES[DEFAULT_MAX_DIMENSION];
}

// ---------- WebSearch ----------
export const WEB_SEARCH_API_PATH = '/agenttool/v1/search';
export const WEB_SEARCH_TIMEOUT_MS = 20_000;
// 本项目补丁（见 ../PATCHES.md）：条数改由环境变量控制，Python 侧按 llm.search_max 传进来
export const WEB_SEARCH_MAX_RESULTS = (() => {
  const n = Number.parseInt(process.env.WB_MAX_RESULTS ?? '', 10);
  return Number.isFinite(n) ? Math.min(Math.max(n, 1), 20) : 5;
})();

/** freshness: d1..d30 / m1..m12 / y1..y5, 允许省略数字 */
export const FRESHNESS_RE = /^(?:d(?:[1-9]|[12]\d|30)?|m(?:[1-9]|1[0-2])?|y[1-5]?)$/;

export const FRESHNESS_DESCRIPTION = `Filter search results by time range. Supports d[N], m[N], and y[N] for the last N calendar days, months, and years:
- d[N]: N is an integer from 1 to 30;
- m[N]: N is an integer from 1 to 12;
- y[N]: N is an integer from 1 to 5.

N may be omitted and defaults to 1. The current partial calendar unit counts as 1. For example, d1 means today, m1 means the current month to date, and y1 means the current year to date. Omit this parameter to disable time filtering.`;

export const WEB_SEARCH_TOOL_DESCRIPTION = `For external information, call WebSearch tool when one or a few targeted queries can complete the user's overall task.`;
