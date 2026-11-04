// 草稿编辑器的业务字段显示名。
// 后端仍使用 MySQL 原字段名提交，前端只在展示层转换，避免把数据库键暴露给用户。
const DRAFT_FIELD_LABELS = {
  id: "数据主键",
  src_id: "原库主键",
  update_time: "源更新时间",
  updated_time: "原库源更新时间",
  title: "标题",
  enterprise_name: "企业名称",
  company_name: "企业名称",
  company: "公司名称",
  tech_type: "技术类别",
  category: "需求类型",
  innovation_type: "创新成果类型",
  contect: "描述",
  analyse_contect: "分析",
  link_name: "联系人",
  link_tel: "联系电话",
  contact_name: "联系人",
  phone: "联系电话",
  telphone: "手机号",
  linkphone: "联系方式",
  email: "联系邮箱",
  office_phone: "办公电话",
  remark: "备注",
  pub_dept: "发布企业名称",
  application: "应用领域",
  aplication_field_scenario: "应用领域及场景",
  application_field: "应用领域",
  scene_lable: "应用场景标签",
  "scene lable": "应用场景标签",
  chain_lable: "产业链标签",
  product: "应用产品",
  product_names: "应用产品标签",
  main_product: "应用产品",
  main_function: "成果主要功能",
  main_advantage: "成果主要优势",
  knowledge_code: "专利号",
  invention_name: "发明人",
  is_knowledge: "拥有知识产权",
  user_name: "专家姓名",
  name: "真实姓名",
  image: "头像",
  avatar: "用户头像",
  duty: "职务",
  achievements: "主要成果",
  research_field: "研究领域",
  label: "标签",
  brief_intr: "简介",
  introduction: "介绍",
  college: "学院",
  professional_title: "职称",
  achievements_honors: "成就与荣誉",
  technical_category: "服务领域",
  technological_achievements: "主要技术成果",
  description: "企业介绍",
  logo: "Logo",
  business_no: "营业执照号",
  reg_capital: "注册资本",
  reg_location: "注册地址",
  business_scope: "经营范围",
  company_org_type: "企业类型",
  estiblish_time: "成立日期",
  base: "省简称",
  city: "市",
  district: "区",
  category_big: "行业分类大类",
  category_middle: "行业分类中类",
  category_small: "行业分类小类",
  reg_status: "企业状态",
  tags: "企业标签",
  street: "街道名称",
  address: "通讯地址",
  created_by: "创建人",
  created_time: "创建时间",
  create_time: "创建时间",
  social_staff_num: "参保人数",
  first_img_url: "需求封面",
  temp_same_title: "成果标题重复标记",
  user_type: "用户类型",
  is_inner_user: "内部人员标记",
  budget: "预算",
  budget_range: "预算区间",
  demand_cycle: "需求周期",
  delivery_time: "期望交付时间",
  provider_name: "企业名称",
  product_category: "产品类型",
  product_type: "产品类型",
  technical_summary: "方案描述",
  delivery_cycle: "交付周期",
  deliver_cycle: "交付周期",
  price_range: "报价区间",
  quote_range: "报价区间",
  organization_name: "组织名称",
  tech_maturity: "技术成熟度",
  technical_maturity: "技术成熟度",
  cooperation_mode: "合作模式",
  enterprise_portrait: "企业画像",
  potential_demand_list_text: "潜在需求",
  expect_cycle: "预计周期",
  adapt_solution: "适配方案",
  supplier_match_dim: "供应商匹配维度",
  inner_mark_note: "补充记录",
};

const FLOW_FIELD_LABELS = {
  demand: {
    title: "需求名称",
    category: "需求类型",
    company: "企业名称",
    contect: "需求描述",
    analyse_contect: "需求分析",
  },
  solution: {
    title: "方案名称",
    contect: "方案描述",
    analyse_contect: "方案分析",
  },
  achievement: {
    title: "成果名称",
    category: "成果类型",
    company: "发布单位",
    tech_type: "成果类型",
    analyse_contect: "成果分析",
  },
};

export function displayDraftFieldLabel(field, draftType) {
  const key = String(field || "").trim();
  if (FLOW_FIELD_LABELS[draftType]?.[key])
    return FLOW_FIELD_LABELS[draftType][key];
  return (
    DRAFT_FIELD_LABELS[key] || (/[\u4e00-\u9fff]/.test(key) ? key : "其他字段")
  );
}

export function displayDraftField(field, draftType) {
  const key = typeof field === "object" ? field?.key : field;
  const mapped = displayDraftFieldLabel(key, draftType);
  if (mapped !== "其他字段") return mapped;
  const label =
    typeof field === "object" ? String(field?.label || "").trim() : "";
  return /[\u4e00-\u9fff]/.test(label) ? label : mapped;
}

export { DRAFT_FIELD_LABELS };
