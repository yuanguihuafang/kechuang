export const knowledgeTypeLabels = {
  enterprise: '企业',
  achievement: '成果',
  expert: '专家',
  demand: '需求',
  solution: '技术方案',
  customer_visit: '客户拜访',
};

// 字段分组与 A-Plan/草稿卡片字段信息(1).txt、A-Plan/新库建表语句.txt 对齐。
export const fieldPolicies = [
  {
    type: 'enterprise', label: '企业库', collection: 'enterprise', table: 'xp_enterprise_profile', note: '公开展示企业基本画像；联系方式和运营数据仅限内部。',
    public: ['企业名称', '企业介绍', 'Logo', '联系人', '营业执照照片', '用户头像', '营业执照号', '注册资本', '注册地址', '经营范围', '企业类型', '成立日期', '省简称', '市', '区', '国民经济行业分类门类', '大类', '中类', '小类', '企业状态', '企业标签', '街道名称'],
    internal: ['创建人', '参保人数', '联系电话', '联系邮箱', '通讯地址'],
    system: ['数据主键', '原库主键', '源更新时间', '原库源更新时间', '创建时间'],
  },
  {
    type: 'achievement', label: '成果库', collection: 'achievement', table: 'xp_pub_result', note: '成果能力可公开检索，专利和联系人信息仅限内部。',
    public: ['标题', '技术类别', '应用产品', '成果分析', '备注', '拥有知识产权', '应用领域', '应用领域及场景', '成果主要功能', '成果主要优势', '应用场景标签', '产业链标签', '应用产品标签', '技术成熟度', '合作模式'],
    internal: ['发布单位', '发布部门', '专利号', '发明人', '联系人', '联系电话', '成果标题重复标记'],
    system: ['数据主键', '创建时间', '原库主键', '源更新时间'],
  },
  {
    type: 'expert', label: '专家库', collection: 'expert', table: 'xp_expert_library', note: '专家能力和成果可公开展示，联系方式和内部时间字段不对外。',
    public: ['专家姓名', '头像', '企业名称', '职务', '主要成果', '研究领域', '标签', '简介', '介绍', '应用领域', '学院', '职称', '成就与荣誉', '服务领域', '应用产品', '主要技术成果'],
    internal: ['创建时间', '联系电话', '邮箱', '办公电话'],
    system: ['数据主键', '原库主键', '源更新时间'],
  },
  {
    type: 'demand', label: '需求库', collection: 'demand', table: 'xp_pub_content_simple', note: '需求内容可用于匹配，联系人和发布管理字段仅限内部。',
    public: ['标题', '技术类别', '应用产品', '成果概述/需求描述', '成果分析', '备注', '应用领域', '需求类型', '创新成果类型', '应用场景标签', '应用产品标签', '预算区间', '需求周期'],
    internal: ['需求封面', '联系人', '联系电话', '发布部门', '公司名称'],
    system: ['数据主键', '创建时间', '原库主键', '源更新时间'],
  },
  {
    type: 'solution', label: '方案库', collection: 'solution', table: 'xp_pub_tech_solution', note: '确认后的方案字段和 PPT/PDF 原文解析块共同组成一个技术方案记录。',
    public: ['方案名称', '产品类型', '方案描述', '方案分析', '交付周期', '报价区间'],
    internal: ['发布企业名称', '联系人', '联系电话'],
    system: [],
  },
  {
    type: 'customer_visit', label: '客户拜访库', collection: 'inner', table: 'ai_inner_inquiry', note: '拜访记录仅限创建人和管理员访问，同时按 user_id 隔离。',
    public: [],
    internal: ['创建人', '会话', '企业名称', '企业画像', '潜在需求', '预算区间', '预计周期', '适配方案', '供应商匹配维度', '内部备注', '询盘状态', '逻辑删除', '创建时间', '更新时间'],
    system: ['数据主键'],
  },
];
