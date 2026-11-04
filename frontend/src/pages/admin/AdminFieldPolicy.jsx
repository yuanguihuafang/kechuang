import { useState } from 'react';
import { policyFieldGroups } from './fieldPolicyGroups.js';

const DEFAULT_VISIBLE_FIELDS = 12;

const fieldPairs = [
  ['企业名称', 'enterprise_name'], ['企业介绍', 'description'], ['Logo', 'logo'], ['联系人', 'admin_name'],
  ['营业执照照片', 'authentication_image'], ['用户头像', 'avatar'], ['营业执照号', 'business_no'], ['注册资本', 'reg_capital'],
  ['注册地址', 'reg_location'], ['经营范围', 'business_scope'], ['企业类型', 'company_org_type'], ['成立日期', 'estiblish_time'],
  ['省简称', 'base'], ['市', 'city'], ['区', 'district'], ['行业分类门类', 'category'], ['行业分类大类', 'category_big'],
  ['行业分类中类', 'category_middle'], ['行业分类小类', 'category_small'], ['企业状态', 'reg_status'], ['企业标签', 'tags'], ['街道名称', 'street'],
  ['联系电话', 'phone'], ['联系邮箱', 'email'], ['通讯地址', 'address'], ['创建人', 'created_by'], ['创建时间', 'created_time'], ['参保人数', 'social_staff_num'],
  ['标题', 'title'], ['技术类别', 'tech_type'], ['应用产品', 'product'], ['成果分析', 'analyse_contect'], ['备注', 'remark'], ['拥有知识产权', 'is_knowledge'],
  ['发布部门', 'pub_dept'], ['应用领域', 'application'], ['应用领域及场景', 'aplication_field_scenario'], ['成果主要功能', 'main_function'], ['成果主要优势', 'main_advantage'], ['技术成熟度', 'tech_maturity'], ['合作模式', 'cooperation_mode'],
  ['应用场景标签', 'scene_lable'], ['产业链标签', 'chain_lable'], ['应用产品标签', 'product_names'], ['专利号', 'knowledge_code'], ['发明人', 'invention_name'],
  ['联系人', 'link_name'], ['联系电话', 'link_tel'], ['创建时间', 'create_time'], ['专家姓名', 'user_name'], ['头像', 'image'], ['职务', 'duty'],
  ['主要成果', 'achievements'], ['研究领域', 'research_field'], ['标签', 'label'], ['简介', 'brief_intr'], ['介绍', 'introduction'], ['学院', 'college'],
  ['职称', 'professional_title'], ['成就与荣誉', 'achievements_honors'], ['服务领域', 'technical_category'], ['应用产品', 'main_product'], ['主要技术成果', 'technological_achievements'],
  ['成果概述/需求描述', 'contect'], ['需求类型', 'category'], ['创新成果类型', 'innovation_type'], ['需求封面', 'first_img_url'], ['预算区间', 'budget_range'], ['需求周期', 'demand_cycle'], ['邮箱', 'email'], ['办公电话', 'office_phone'],
  ['方案名称', 'title'], ['产品类型', 'product_type'], ['方案封面图', 'first_img_url'], ['方案描述', 'contect'], ['方案分析', 'analyse_contect'], ['交付周期', 'deliver_cycle'], ['报价区间', 'quote_range'], ['适配应用领域', 'application'],
  ['数据主键', 'id'], ['原库主键', 'src_id'], ['来源草稿主键', 'src_draft_id'], ['更新时间', 'update_time'], ['源更新时间', 'update_time'], ['原库源更新时间', 'updated_time'],
  ['手机号', 'telphone'], ['真实姓名', 'name'], ['联系方式', 'linkphone'], ['公司名称', 'company'], ['用户类型', 'user_type'], ['内部人员标记', 'is_inner_user'],
  ['成果标题重复标记', 'temp_same_title'],
  ['创建人', 'user_card_id'], ['会话', 'chat_session_id'], ['企业画像', 'enterprise_portrait'], ['潜在需求', 'potential_demand_list_text'],
  ['预计周期', 'expect_cycle'], ['适配方案', 'adapt_solution'], ['供应商匹配维度', 'supplier_match_dim'], ['内部备注', 'inner_mark_note'],
  ['询盘状态', 'inquiry_status'], ['逻辑删除', 'is_deleted'],
];
const fieldLabelMap = new Map(fieldPairs.flatMap(([cn, en]) => [[cn, { cn, en }], [en, { cn, en }]]));
const fieldLabelsByType = {
  solution: { pub_dept: '发布企业名称' },
  achievement: { company: '发布单位' },
};

function fieldLabel(field, knowledgeType) {
  const text = String(field);
  if (fieldLabelsByType[knowledgeType]?.[text]) return { cn: fieldLabelsByType[knowledgeType][text], en: text };
  return fieldLabelMap.get(text) || { cn: text, en: text };
}

function FieldTag({ field, className, knowledgeType, redacted = false, hiddenFromResult = false }) {
  const key = String(field?.key ?? field);
  const label = field?.label ? { cn: field.label, en: key } : fieldLabel(key, knowledgeType);
  const marker = hiddenFromResult ? '不展示' : redacted ? '脱敏' : '';
  return <span className={`policy-tag ${className}`} title={`${label.cn} / ${label.en}${marker ? ` · ${marker}` : ''}`}><span>{label.cn}</span><small>{label.en}</small>{marker && <em>{marker}</em>}</span>;
}

function uniqueFields(fields = []) {
  const unique = new Map();
  fields.forEach((field) => {
    const key = String(field?.key ?? field).trim();
    if (key && !unique.has(key)) unique.set(key, typeof field === 'object' ? field : field);
  });
  return [...unique.values()];
}

function policyFieldItems(policy, fields = []) {
  const definitions = new Map((policy.fields || []).map((field) => [field.key, field]));
  return fields.map((field) => {
    const rawKey = String(field?.key ?? field).trim();
    if (definitions.has(rawKey)) return definitions.get(rawKey);
    const label = fieldLabel(rawKey, policy.type);
    return definitions.get(label.en) || { key: label.en, label: label.cn };
  });
}

function PolicyFieldGroup({ title, fields, className, knowledgeType, redactedFields = [], hiddenFields = [] }) {
  const [expanded, setExpanded] = useState(false);
  const unique = uniqueFields(fields);
  const visible = expanded ? unique : unique.slice(0, DEFAULT_VISIBLE_FIELDS);
  const hiddenCount = Math.max(0, unique.length - visible.length);

  return <div className="policy-field-group">
    <div className="policy-group-head"><h3>{title}</h3><span>{unique.length} 个</span></div>
    <div className={`policy-tags${expanded ? ' is-expanded' : ''}`}>
      {visible.map((field) => {
        const key = String(field?.key ?? field);
        return <FieldTag field={field} className={className} knowledgeType={knowledgeType} redacted={redactedFields.includes(key)} hiddenFromResult={hiddenFields.includes(key)} key={key} />;
      })}
    </div>
    {unique.length > DEFAULT_VISIBLE_FIELDS && <button type="button" className="policy-expand-button" onClick={() => setExpanded((current) => !current)} aria-expanded={expanded}>
      {expanded ? '收起字段' : `展开全部（${hiddenCount} 个）`}
    </button>}
  </div>;
}

function PolicySummary({ policy, businessFields, internalFields, systemFields, separateExternalFields }) {
  const total = businessFields.length + internalFields.length + systemFields.length;
  return <div className="policy-summary" aria-label={`${policy.label}字段统计`}>
    <span><b>{businessFields.length}</b>{separateExternalFields ? '公开' : '业务'}</span>
    {separateExternalFields && <span><b>{internalFields.length}</b>脱敏字段</span>}
    <span><b>{systemFields.length}</b>系统</span>
    <span><b>{total}</b>物理字段</span>
  </div>;
}

export function AdminFieldPolicy({ policies = [] }) {
  return <section className="admin-console admin-policy-page" aria-labelledby="field-policy-title">
    <div className="admin-console-hero"><div><h1 id="field-policy-title">字段访问规则</h1><p className="admin-kicker">GOVERNANCE / FIELD ACCESS</p><p>固定分级来自业务字段说明表。这里只展示管理口径，不代表登录鉴权已经接入。</p></div></div>
    <div className="policy-notice"><strong>业务字段</strong><span>除系统字段外全部同步到 Milvus</span><strong>系统字段</strong><span>仅用于主键、同步、身份识别或审核，不进入向量文本</span></div>
    <div className="policy-grid">{policies.map((policy) => {
      const groups = policyFieldGroups(policy).map((group) => ({
        ...group,
        fields: uniqueFields(policyFieldItems(policy, group.fields)),
      }));
      const separateExternalFields = groups.some((group) => group.key === 'internal');
      const businessFields = groups.find((group) => group.key === 'public' || group.key === 'business').fields;
      const internalFields = groups.find((group) => group.key === 'internal')?.fields || [];
      const systemFields = groups.find((group) => group.key === 'system').fields;
      const redactedFields = policy.redacted || [];
      const hiddenFields = policy.hidden_from_result || [];
      return <article className={`admin-card policy-card${policy.identity_only ? ' is-identity' : ''}`} key={policy.type}>
        <div className="admin-card-head"><div><p className="admin-kicker">{policy.type.toUpperCase()}</p><h2>{policy.label}</h2><small className="policy-table">Milvus Collection：{policy.collection || '—'} · MySQL 表：{policy.table || '—'}{policy.identity_only ? ' · 仅身份读取' : ''}</small></div><span className="policy-count">{policy.unconfigured ? '待配置' : `${businessFields.length + internalFields.length + systemFields.length} 个字段`}</span></div>
        <p className="admin-card-note">{policy.note}</p>
        {!policy.unconfigured && <PolicySummary policy={policy} businessFields={businessFields} internalFields={internalFields} systemFields={systemFields} separateExternalFields={separateExternalFields} />}
        {policy.unconfigured ? <div className="policy-unconfigured">暂不展示固定字段清单。后续接入业务字段后，在这里维护版本化规则。</div> : <div className="policy-columns">
          {groups.map((group) => <PolicyFieldGroup
            key={group.key}
            title={group.key === 'public' && redactedFields.length ? `${group.title} · 脱敏字段已标记` : group.title}
            fields={group.fields}
            className={`policy-tag-${group.key === 'internal' ? 'internal' : group.key === 'system' ? 'system' : 'public'}`}
            knowledgeType={policy.type}
            redactedFields={redactedFields}
            hiddenFields={hiddenFields}
          />)}
        </div>}
      </article>;
    })}</div>
  </section>;
}
