export const examples = [
  "我有创新需求，帮我匹配专家或成果",
  "我有技术成果，帮我寻找潜在客户",
  "我有智能化升级需求，帮我推荐方案商",
];

export const quickIntents = [
  {
    intent: "enterprise_profile",
    label: "企业画像",
    code: "01",
    description: "了解企业能力、行业背景与潜在需求",
    prompt: "帮我了解这家企业的基本情况、技术能力和潜在需求",
  },
  {
    intent: "demand_discovery",
    label: "需求发现",
    code: "02",
    description: "查询客户当前的需求与合作诉求",
    prompt: "帮我查询当前客户的需求和待解决问题",
  },
  {
    intent: "supply_demand_match",
    label: "供需匹配",
    code: "03",
    description: "查询企业、专家、方案和科研成果",
    prompt: "帮我查询与当前需求相关的企业、专家、方案和科研成果",
  },
  {
    intent: "customer_visit",
    label: "客户拜访",
    code: "04",
    description: "记录交流内容并整理拜访后的跟进方向",
    prompt: "我正在拜访客户，请帮我记录交流内容并整理企业画像、需求和跟进方向",
  },
];

export const externalQuickIntents = [
  {
    intent: "demand",
    label: "我有需求",
    code: "01",
    description: "梳理需求并生成可编辑草稿",
    prompt: "我有一项需求，请帮我梳理并生成需求草稿",
  },
  {
    intent: "supply_draft",
    label: "我有方案 & 成果",
    code: "02",
    description: "整理技术方案或科研成果",
    prompt: "我有方案或成果，请帮我整理成草稿",
  },
  {
    intent: "supply_demand_match",
    label: "供需匹配",
    code: "03",
    description: "在公开知识中寻找适配内容",
    prompt: "请帮我在公开知识中匹配相关需求和技术方案",
  },
];
