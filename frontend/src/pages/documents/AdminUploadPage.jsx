import { DocumentIntakeWorkspace } from './DocumentIntakeWorkspace.jsx';
import { AdminDocumentsPage } from './AdminDocumentsPage.jsx';

export function AdminUploadPage({ intakeProps, catalogProps }) {
  const { knowledgeTypes, catalogKnowledgeType, switchCatalog } = catalogProps;
  return <section className="admin-page-content">
    <header className="admin-page-intro">
      <div>
        <h1>上传文档</h1>
        <p className="admin-kicker">DOCUMENT / INTAKE WORKFLOW</p>
        <p>按“上传至 MinIO → Perception 文档解析 → LLM 提取草稿 → 确认结果 → Retrieval 建立索引”处理资料。</p>
      </div>
    </header>
    <nav className="knowledge-catalog-tabs" aria-label="知识库分类">{knowledgeTypes.map(([value, label]) => <button type="button" key={value} className={catalogKnowledgeType === value ? 'active' : ''} onClick={() => switchCatalog(value)}>{label}</button>)}</nav>
    <DocumentIntakeWorkspace {...intakeProps} />
    <AdminDocumentsPage {...catalogProps} catalogOnly showCatalogTabs={false} />
  </section>;
}
