const AVAILABLE = [
  ['资料入库', '上传、解析、人工确认与 Milvus 索引'],
  ['文档生命周期', '查看、更新替换、下架、恢复与删除'],
  ['任务可追溯', '任务状态、失败原因与人工恢复'],
  ['用户资料中心', '资料下架、对话记录和草稿查看'],
];

const PENDING = [
  ['权限管理', '正式登录、角色配置与组织级授权'],
  ['审计检索', '按用户、文档和动作查询审计日志'],
  ['批量导入', '文件模板校验、批量入库与错误导出'],
  ['运营通知', '企业微信与站内通知的正式推送'],
];

function CapabilityColumn({ title, description, items, status }) {
  return <section className="capability-column">
    <div className="capability-column-heading">
      <span className={`capability-status ${status}`}>{status === 'available' ? '已接入' : '待接入'}</span>
      <div><h3>{title}</h3><p>{description}</p></div>
    </div>
    <ul>
      {items.map(([name, detail]) => <li key={name}><strong>{name}</strong><span>{detail}</span></li>)}
    </ul>
  </section>;
}

export function CapabilityBoard() {
  return <section className="capability-board" aria-labelledby="capability-board-title">
    <div className="capability-board-head">
      <div>
        <p className="admin-kicker">PLATFORM CAPABILITIES</p>
        <h2 id="capability-board-title">管理能力接入状态</h2>
        <p>仅展示当前前后端真实可用的能力；待接入项目不会在业务页面伪装为可操作按钮。</p>
      </div>
      <span className="capability-note">当前阶段：知识资产管理</span>
    </div>
    <div className="capability-columns">
      <CapabilityColumn title="可用能力" description="可直接由当前网关执行" items={AVAILABLE} status="available" />
      <CapabilityColumn title="后续接入" description="前端已记录，不调用未实现接口" items={PENDING} status="pending" />
    </div>
  </section>;
}
