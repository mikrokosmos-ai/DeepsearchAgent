import { apiUrl } from "./api";

// 把直连 MinIO 的图片地址改写为同源后端代理地址。
// 后端 /api/image 会校验地址确属本库 MinIO 后再用服务端 ak/sk 取图返回，
// 从而规避浏览器直连 127.0.0.1:9000 被本机代理插件拦截 / CORS / 桶公开读策略缺失等问题。
const IMAGE_BUCKET = "knowledge-base-files";

export function toProxySrc(raw?: string | null): string {
  if (!raw) return "";
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return raw; // 相对地址或非标准 URL，原样返回
  }
  const isMinio =
    parsed.pathname.includes(`/${IMAGE_BUCKET}/`) || /:9000$/.test(parsed.host);
  if (!isMinio) return raw;
  return apiUrl(`/api/image?src=${encodeURIComponent(raw)}`);
}
