export function ChatHistory({ conversationList, conversationMenuOpen, setConversationMenuOpen, loadConversationList, openConversation, createConversation, sessionActionId, renamingSessionId, renameValue, setRenameValue, beginRenameConversation, cancelRenameConversation, saveConversationRename, removeConversation, onError, onCurrent }) {
  return <div className="conversation-control">
    <button type="button" className="conversation-trigger" aria-expanded={conversationMenuOpen} onClick={() => { setConversationMenuOpen((open) => !open); loadConversationList(); }}><span aria-hidden="true">◷</span> 历史会话</button>
    {conversationMenuOpen && <div className="conversation-menu" role="menu">
      <div className="conversation-menu-heading"><strong>历史会话</strong><small>{conversationList.length} 个会话</small></div>
      <div className="conversation-menu-list">
        {!conversationList.length && <p>暂无历史会话</p>}
        {conversationList.map((item) => {
          const title = String(item.state?.title || item.state?.summary || '未命名会话');
          const isRenaming = renamingSessionId === item.session_id;
          const isActing = sessionActionId === item.session_id;
          return <div className="conversation-item" key={item.session_id}>
            {isRenaming ? <form className="conversation-rename-form" onSubmit={(event) => saveConversationRename(event, item, onError)}><input className="conversation-rename-input" value={renameValue} maxLength={80} autoFocus onChange={(event) => setRenameValue(event.target.value)} onClick={(event) => event.stopPropagation()} onKeyDown={(event) => { if (event.key === 'Escape') cancelRenameConversation(event); }} /><div className="conversation-item-actions"><button type="submit" className="conversation-action primary" disabled={isActing}>保存</button><button type="button" className="conversation-action" onClick={cancelRenameConversation}>取消</button></div></form> : <><button type="button" className="conversation-item-main" role="menuitem" onClick={() => openConversation(item)}><strong>{title}</strong><small>{item.updated_at ? new Date(item.updated_at).toLocaleString('zh-CN', { hour12: false }) : '—'}</small></button><div className="conversation-item-actions"><button type="button" className="conversation-action" onClick={(event) => beginRenameConversation(event, item)} disabled={isActing}>重命名</button><button type="button" className="conversation-action danger" onClick={(event) => removeConversation(event, item, onError, onCurrent)} disabled={isActing}>删除</button></div></>}
          </div>;
        })}
      </div>
      <button type="button" className="conversation-new" onClick={createConversation}>＋ 新建会话</button>
    </div>}
  </div>;
}
