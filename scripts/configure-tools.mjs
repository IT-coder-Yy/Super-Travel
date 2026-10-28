// Only this public endpoint is compiled into the desktop; provider credentials stay on the server.
export function sharedToolsUrl(env) {
  const value = (env.STA_SHARED_TOOLS_URL || '').trim();
  const release = (env.GITHUB_REF || '').startsWith('refs/tags/');
  if (!value) {
    if (release) throw new Error('正式发布必须配置 STA_SHARED_TOOLS_URL。');
    return '';
  }
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || /\s/.test(value)) {
    throw new Error('共享工具地址必须是无凭据、无查询参数的 HTTPS 地址。');
  }
  return url.href.replace(/\/$/, '');
}

export async function verifySharedTools(endpoint, request = fetch) {
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
