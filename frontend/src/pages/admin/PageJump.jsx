import { useEffect, useState } from 'react';
import { normalizePageJump } from './pageJump.js';

export function PageJump({ page, totalPages, onJump, disabled = false, label = '列表' }) {
  const [value, setValue] = useState(String(page));
  useEffect(() => setValue(String(page)), [page]);

  function submit(event) {
    event.preventDefault();
    const target = normalizePageJump(value, totalPages);
    if (target === null) return;
    setValue(String(target));
    onJump(target);
  }

  return <form className="admin-page-jump" noValidate onSubmit={submit}>
    <label htmlFor={`page-jump-${label}`}>跳至</label>
    <input
      id={`page-jump-${label}`}
      aria-label={`${label}页码`}
      type="number"
      min="1"
      max={totalPages}
      step="1"
      value={value}
      onChange={(event) => setValue(event.target.value)}
      disabled={disabled}
    />
    <button type="submit" className="secondary" disabled={disabled}>跳转</button>
  </form>;
}
