import { externalQuickIntents } from '../config/quickIntents.js';

function identityOptionLabel(user) {
  const id = String(user?.user_id || '');
  const name = String(user?.name || '').trim();
  const person = name && name !== id ? `${name} · ${id}` : (name || id);
  return `${person} · ${user?.is_inner_user ? '内部人员' : '企业人员'}`;
}

export function SupervisorSidebar({ identity, selectedIdentity, identityOptions, identityLoading, identityError, onIdentityChange, allowedScopes, isExternalUser, startExternalFlow }) {
  return <aside className="chat-sidebar" aria-label="当前身份与可用知识范围">
    <div className="chat-sidebar-panel">
      <section className="chat-sidebar-card identity-sidebar-card">
        <div className="chat-sidebar-heading"><span className="sidebar-icon">◎</span><h2>当前身份</h2></div>
        <div className="identity-sidebar-profile"><span className="identity-avatar">{(selectedIdentity?.name || identity.name || selectedIdentity?.user_id || identity.user_id || 'U').slice(0, 1).toUpperCase()}</span><div><strong>{selectedIdentity?.name || identity.name || (identity.user_role === 'internal_market' ? '内部市场人员' : '企业人员')}</strong><small>{selectedIdentity?.company || identity.company || `用户 ID：${selectedIdentity?.user_id || identity.user_id}`}</small></div></div>
        <label className="identity-switcher">切换测试身份<select value={selectedIdentity?.user_id || ''} onChange={(event) => onIdentityChange?.(event.target.value)} disabled={identityLoading || identityOptions.length === 0}>{identityOptions.length === 0 && <option value="">{identityLoading ? '正在读取用户信息…' : '未读取到用户'}</option>}{identityOptions.map((user) => <option value={user.user_id} key={user.user_id}>{identityOptionLabel(user)}</option>)}</select></label>
        <p className="identity-scope">可访问：{allowedScopes}</p>{identityError && <p className="identity-error" role="alert">用户信息读取失败：{identityError}</p>}
      </section>
      <section className="chat-sidebar-card knowledge-sidebar-card">
        <div className="chat-sidebar-heading"><span className="sidebar-icon">◇</span><h2>{isExternalUser ? '快捷入口' : '可用知识范围'}</h2></div>
        {isExternalUser ? <div className="external-shortcuts"><p>快速进入</p>{externalQuickIntents.slice(0, 4).map((item) => <button type="button" key={item.intent} onClick={() => startExternalFlow(item)}><span>{item.code}</span><strong>{item.label}</strong><small>{item.description}</small><i>→</i></button>)}</div> : <ul className="knowledge-scope-list"><li><span className="scope-icon">▤</span><div><strong>公共知识</strong><small>政策、行业与公开资料</small></div><em>可检索</em></li><li><span className="scope-icon">▱</span><div><strong>企业信息</strong><small>企业能力与基础画像</small></div><em>可检索</em></li><li><span className="scope-icon">▣</span><div><strong>需求与方案</strong><small>已确认需求和技术方案</small></div><em>可检索</em></li><li><span className="scope-icon">◈</span><div><strong>科研成果</strong><small>可公开的成果资料</small></div><em>可检索</em></li></ul>}
        <p className="knowledge-scope-note">范围会随登录身份、数据状态和权限策略变化。</p>
      </section>
    </div>
  </aside>;
}
