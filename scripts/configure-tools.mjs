import { isIP } from 'node:net';

// Only the endpoint is compiled into the desktop; provider credentials stay in the tool service.
export function sharedToolsUrl(env) {
  const raw = env.STA_SHARED_TOOLS_URL || '';
  if (/[\\\x00-\x1f\x7f]/.test(raw)) throw new Error('工具服务地址不能包含反斜杠或控制字符。');
  const value = raw.trim() || 'http://127.0.0.1:8080';
  const url = new URL(value);
  const local = url.hostname === 'localhost' || url.hostname === '[::1]' || isIP(url.hostname) === 4 && url.hostname.startsWith('127.');
  if (!(url.protocol === 'https:' || url.protocol === 'http:' && local) || url.username || url.password || /[?#\s]/.test(value) || value.length > 2048 || url.href.length > 2048) {
    throw new Error('工具服务地址必须是无凭据、无查询参数的 HTTPS 地址，或本机回环 HTTP 地址。');
  }
  return url.href.replace(/\/$/, '');
}

export async function verifySharedTools(endpoint, request = fetch) {
  endpoint = sharedToolsUrl({ STA_SHARED_TOOLS_URL: endpoint });
  // A local development service is started by the developer, not on the release runner.
  if (new URL(endpoint).protocol === 'http:') return;
  const response = await request(`${endpoint}/v1/tools`, { redirect: 'error', signal: AbortSignal.timeout(40_000) });
  if (!response.ok) throw new Error('共享工具服务未就绪，停止正式发布。');
  const data = await response.json();
  const providers = Array.isArray(data.providers) ? data.providers : [];
  const tools = Array.isArray(data.tools) ? data.tools : [];
  if (!['amap', 'baidu', 'tavily', 'serper', 'xhs'].every(id => providers.some(item => item.id === id && item.configured === true)) ||
      !tools.some(item => ['baidu_map_search_places', 'amap_maps_text_search'].includes(item.name))) {
    throw new Error('共享工具凭据未全部配置或没有可用地点查询，停止正式发布。');
  }
}
