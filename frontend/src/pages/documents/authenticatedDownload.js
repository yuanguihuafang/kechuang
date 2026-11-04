import { download } from '../../api/request.js';

export async function downloadAuthenticatedFile(url, filename, gatewayUrl) {
  const target = new URL(url, gatewayUrl);
  const gateway = new URL(gatewayUrl);
  const blob = await download(target.toString(), { skipAuth: target.origin !== gateway.origin });
  const objectUrl = URL.createObjectURL(blob);
  try {
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename || '原文件';
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}
