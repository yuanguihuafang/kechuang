import { useState } from 'react';
import { api } from '../../api.js';
import { downloadAuthenticatedFile } from './authenticatedDownload.js';

export function AuthenticatedDownload({ url, filename, label = '下载', className = 'text-button' }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (!url) return null;

  async function handleDownload() {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await downloadAuthenticatedFile(url, filename, api.perceptionAssetUrl('/'));
    } catch (failure) {
      setError(failure.message || '文件下载失败');
    } finally {
      setBusy(false);
    }
  }

  return <span className="authenticated-download">
    <button type="button" className={className} onClick={() => void handleDownload()} disabled={busy}>
      {busy ? '下载中…' : label}
    </button>
    {error && <small role="alert">{error}</small>}
  </span>;
}
