/**
 * imageCodec.mjs —— 还原 ImageUtils 里与 ViewImage 相关的那部分。
 *
 * 源码用的编解码器是 jimp（`readImage`），这里做成可插拔：
 *   1) 若能 resolve 到 sharp  -> 用 sharp（等价能力强，速度更快）
 *   2) Windows 且路径为纯 ASCII -> 用 .NET System.Drawing（随包附带 scripts/resize.ps1）
 *   3) 都没有 -> 原样返回，并打印告警（真实工具会丢弃超限图片）
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  JPEG_MIME, VIEW_IMAGE_SUPPORTED_EXTS,
  getCurrentImageCompressionProfile, IMAGE_TOO_LARGE_AFTER_COMPRESSION,
} from './constants.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const RESIZE_PS1 = path.join(__dirname, '..', 'scripts', 'resize.ps1');

// ---------- MIME ----------
export const IMAGE_MIME_TYPE_MAP = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', jpe: 'image/jpeg', jfif: 'image/jpeg',
  png: 'image/png', gif: 'image/gif', webp: 'image/webp',
  svg: 'image/svg+xml', svgz: 'image/svg+xml',
  bmp: 'image/bmp', dib: 'image/bmp', tiff: 'image/tiff', tif: 'image/tiff',
  ico: 'image/x-icon', cur: 'image/x-icon', avif: 'image/avif',
  heic: 'image/heic', heif: 'image/heif',
};

/** 源码 detectMimeFromMagicBytes：只看魔数，不信扩展名 */
export function detectMimeFromMagicBytes(buf) {
  if (!buf || buf.length < 2) return undefined;
  if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return 'image/png';
  if (buf[0] === 0xff && buf[1] === 0xd8) return 'image/jpeg';
  if (buf.slice(0, 6).toString('ascii') === 'GIF87a' || buf.slice(0, 6).toString('ascii') === 'GIF89a') return 'image/gif';
  if (buf.slice(0, 4).toString('ascii') === 'RIFF' && buf.slice(8, 12).toString('ascii') === 'WEBP') return 'image/webp';
  if (buf[0] === 0x42 && buf[1] === 0x4d) return 'image/bmp';
  return undefined;
}

export function isViewImageSupportedFormat(ext) {
  return VIEW_IMAGE_SUPPORTED_EXTS.includes(String(ext || '').toLowerCase());
}

// ---------- 尺寸（纯 JS，用于统计/估算） ----------
export function dimensions(buf) {
  if (buf.length > 24 && buf[0] === 0x89 && buf[1] === 0x50) {
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  }
  if (buf.length > 10 && buf.slice(0, 3).toString('ascii') === 'GIF') {
    return { width: buf.readUInt16LE(6), height: buf.readUInt16LE(8) };
  }
  if (buf.length > 26 && buf[0] === 0x42 && buf[1] === 0x4d) {
    return { width: Math.abs(buf.readInt32LE(18)), height: Math.abs(buf.readInt32LE(22)) };
  }
  if (buf.length > 30 && buf[0] === 0xff && buf[1] === 0xd8) {
    let i = 2;
    while (i < buf.length - 9) {
      if (buf[i] !== 0xff) { i += 1; continue; }
      const m = buf[i + 1];
      if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) {
        return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
      }
      i += 2 + buf.readUInt16BE(i + 2);
    }
  }
  return undefined;
}

// ---------- 体积/档位判定（源码同名逻辑） ----------
export function base64FromDataUrl(dataUrl) {
  return dataUrl.split(',')[1];
}
export function rawBytesFromBase64(b64) {
  return Math.round(0.75 * b64.length);
}
export function isWithinProfileSizeLimit(b64, profile) {
  return b64.length <= profile.maxBase64Size && rawBytesFromBase64(b64) <= profile.maxRawBytes;
}
/** 源码 resolveTargetDimension：超 raw 上限时按面积比 √(0.85*limit) 再压一档 */
export function resolveTargetDimension(rawLen, currentDim, profile) {
  let target = Math.min(profile.maxDimension, currentDim);
  if (rawLen > profile.maxRawBytes) {
    target = Math.min(target, Math.max(1, Math.round(currentDim * Math.sqrt((0.85 * profile.maxRawBytes) / rawLen))));
  }
  return target;
}

// ---------- 编解码后端 ----------
let sharpMod;
async function getSharp() {
  if (sharpMod !== undefined) return sharpMod;
  try { sharpMod = (await import('sharp')).default; } catch { sharpMod = null; }
  return sharpMod;
}

function isAscii(p) { return /^[\x00-\x7f]*$/.test(p); }

function resizeWithDotnet(buf, mime, maxDim, quality) {
  const dir = mkdtempSync(path.join(tmpdir(), 'wbimg-'));
  const ext = mime === 'image/png' ? '.png' : mime === 'image/bmp' ? '.bmp' : mime === 'image/gif' ? '.gif' : '.jpg';
  const inp = path.join(dir, 'in' + ext);
  const outp = path.join(dir, 'out.jpg');
  try {
    writeFileSync(inp, buf);
    execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-File', RESIZE_PS1,
      '-In', inp, '-Out', outp, '-MaxDim', String(maxDim), '-Quality', String(quality)],
      { stdio: 'pipe', timeout: 30_000 });
    return readFileSync(outp);
  } finally {
    try { rmSync(dir, { recursive: true, force: true }); } catch { /* noop */ }
  }
}

/** 源码 compressImage：只在超尺寸时缩，保持原格式 */
async function compressImage(dataUrl, maxDim) {
  const m = /^data:(image\/[^;]+);base64,(.+)$/.exec(dataUrl);
  if (!m) return dataUrl;
  const mime = m[1];
  const buf = Buffer.from(m[2], 'base64');
  const dim = dimensions(buf);
  const longEdge = dim ? Math.max(dim.width, dim.height) : 0;
  if (!longEdge || longEdge <= maxDim) return dataUrl;

  const sharp = await getSharp();
  if (sharp) {
    const scale = maxDim / longEdge;
    const out = await sharp(buf)
      .resize({ width: Math.round(dim.width * scale), height: Math.round(dim.height * scale), fit: 'fill' })
      .toFormat(mime === 'image/png' ? 'png' : 'jpeg')
      .toBuffer();
    return `data:${mime === 'image/png' ? 'image/png' : JPEG_MIME};base64,${out.toString('base64')}`;
  }
  const windowsOk = process.platform === 'win32';
  if (windowsOk && isAscii(RESIZE_PS1)) {
    // .NET 只能稳定编码 jpg（png 走 Bitmap.Save 保真度较差），故统一转 jpeg
    const out = resizeWithDotnet(buf, mime, maxDim, 90);
    return `data:${JPEG_MIME};base64,${out.toString('base64')}`;
  }
  console.warn('[ImageUtils] no codec available (install sharp); returning original bytes');
  return dataUrl;
}

/** 源码 compressToProfileLimit：jpeg 质量阶梯 + 转 jpeg 兜底 */
async function compressToProfileLimit(dataUrl, profile) {
  const b64 = base64FromDataUrl(dataUrl);
  if (!b64 || isWithinProfileSizeLimit(b64, profile)) return dataUrl;
  const m = /^data:(image\/[^;]+);base64,(.+)$/.exec(dataUrl);
  if (!m) return dataUrl;
  const sharp = await getSharp();
  const src = Buffer.from(m[2], 'base64');
  for (const q of profile.jpegQualities) {
    let out;
    if (sharp) {
      out = await sharp(src).jpeg({ quality: q }).toBuffer();
    } else if (process.platform === 'win32') {
      try { out = resizeWithDotnet(src, m[1], 10_000, q); } catch { continue; }
    } else continue;
    const cand = `data:${JPEG_MIME};base64,${out.toString('base64')}`;
    if (isWithinProfileSizeLimit(base64FromDataUrl(cand), profile)) return cand;
  }
  return '';
}

/**
 * 源码 compressUserImage 的等价实现。
 * 返回 '' 表示压缩后仍超限 -> 调用方应替换为 IMAGE_TOO_LARGE_AFTER_COMPRESSION。
 */
export async function compressUserImage(dataUrl, maxDim) {
  const profile = getCurrentImageCompressionProfile(maxDim);
  if (!profile) return dataUrl;
  let stage1;
  try {
    stage1 = await compressImage(dataUrl, profile.maxDimension);
  } catch (e) {
    console.warn('[ImageUtils] compressUserImage resize failed:', e?.message ?? e);
    const b64 = base64FromDataUrl(dataUrl);
    return b64 && !isWithinProfileSizeLimit(b64, profile) ? '' : dataUrl;
  }
  const b64 = base64FromDataUrl(stage1);
  if (!b64 || isWithinProfileSizeLimit(b64, profile)) return stage1;
  return compressToProfileLimit(stage1, profile);
}

export { IMAGE_TOO_LARGE_AFTER_COMPRESSION };
