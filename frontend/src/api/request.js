export function errorMessage(value, fallback = '请求失败') {
  if (typeof value === 'string' && value.trim()) return value;
  if (value?.message && typeof value.message === 'string') return value.message;
  if (value?.detail) return errorMessage(value.detail, fallback);
  if (value == null) return fallback;
  try { return JSON.stringify(value); } catch { return String(value); }
}

export async function request(url, options = {}) {
  const { skipAuth = false, timeoutMs = 0, ...fetchOptions } = options || {};
  const token = skipAuth ? '' : sessionStorage.getItem('kechuang-agent-token');
  const headers = new Headers(fetchOptions.headers || {});
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`);
  let requestUrl = url;
  let requestBody = fetchOptions.body;
  const controller = timeoutMs > 0 ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort('request timeout'), timeoutMs) : null;
  let response;
  try {
    response = await fetch(requestUrl, { ...fetchOptions, body: requestBody, headers, ...(fetchOptions.signal || controller ? { signal: fetchOptions.signal || controller?.signal } : {}) });
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw new Error(timeoutMs > 0 ? '请求超时，请稍后重试' : '请求已取消');
    }
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(errorMessage(body.detail || body.error, `请求失败 (${response.status})`));
    error.status = response.status;
    error.retryAfter = Number(response.headers.get('Retry-After')) || body.error?.details?.retry_after_seconds || 0;
    error.code = body.error?.code || body.error_code;
    error.payload = body;
    throw error;
  }
  if (body?.access_token) sessionStorage.setItem('kechuang-agent-token', body.access_token);
  return body;
}

export async function download(url, options = {}) {
  const { skipAuth = false, ...fetchOptions } = options || {};
  const token = skipAuth ? '' : sessionStorage.getItem('kechuang-agent-token');
  const headers = new Headers(fetchOptions.headers || {});
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(url, { ...fetchOptions, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(errorMessage(body.detail || body.error, `下载失败 (${response.status})`));
  }
  return response.blob();
}
