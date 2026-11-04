import {
  Layers3Icon, // 总览
  BoxIcon, // 应用场景层
  BotIcon, // 智能体服务层
  BlocksIcon, // Skill 能力层
  CpuIcon, // 技术平台层
  ServerIcon, // 基础设施层
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

type TechLayer = {
  id: string;
  index: string;
  title: string;
  subtitle: string; // EN
  desc: string;
  items: string[];
  icon: LucideIcon;
  position?: "tall"; // 用于错落视觉
};

// 按 PPT 第 30 页产业大脑分层，适配 OPIE OS 通用叙事
const LAYERS: TechLayer[] = [
  {
    id: "app",
    index: "L1",
    title: "应用场景层",
    subtitle: "Application Layer",
    desc: "在 OPIE OS 之上的场景化界面 —— 工作台、IM 桥接、API 网关、行业 portal 都挂在这一层。",
    items: [
      "可视化工作台",
      "IM 桥接（飞书 / 钉钉 / 企微 / 微信）",
      "Web · H5 · 移动端",
      "API 网关",
      "行业 portal（可选）",
    ],
    icon: BoxIcon,
  },
  {
    id: "agent",
    index: "L2",
    title: "智能体服务层",
    subtitle: "Agent Service Layer",
    desc: "意图理解、任务拆解、路由分发、确认与回写 —— 把自然语言变成可执行的业务动作。",
    items: [
      "意图理解 · NL → 业务目标",
      "任务拆解 · 执行计划与约束",
      "路由分发 · 匹配业务 Agent",
      "智能交互问答",
      "全局调度中枢",
      "全局风控预警",
    ],
    icon: BotIcon,
    position: "tall",
  },
  {
    id: "skill",
    index: "L3",
    title: "Skill 能力层",
    subtitle: "Skill Capability Layer",
    desc: "把流程规则封装为可复用的 Skill —— 一整段业务规则，而非单次工具调用。",
    items: [
      "信息查询 · 跨模块实时取数",
      "录单派单 · 解析·确认·写入",
      "审批处理 · 发起·催办·归档",
      "分析研判 · 归因·预警·报表",
      "生成触达 · 文案·触达·跟进",
      "工单 / 客服 · 流程贯通",
      "数据平台 · 指标与知识库",
      "IoT / 第三方 · 设备与外部服务",
    ],
    icon: BlocksIcon,
  },
  {
    id: "platform",
    index: "L4",
    title: "技术平台层",
    subtitle: "Technology Platform",
    desc: "所有可复用的工程能力都在这一层 —— 一处升级，全栈复用。",
    items: [
      "大模型与多模型路由",
      "数据中心 · 多源治理资产化",
      "微服务 API 网关",
      "统一身份认证 · 可信接入",
      "Skill 编排引擎",
      "隔离执行沙箱",
    ],
    icon: CpuIcon,
    position: "tall",
  },
  {
    id: "infra",
    index: "L5",
    title: "基础设施层",
    subtitle: "Infrastructure",
    desc: "贯穿全生命周期的工程基座 —— 数据安全、可信流转。",
    items: [
      "智算 / 超算集群",
      "高可靠数据存储",
      "云原生底座",
      "私有化部署支持",
    ],
    icon: ServerIcon,
  },
];

export function TechnologyArchitectureSection({
  className,
}: {
  className?: string;
}) {
  return (
    <section
      data-reveal
      id="tech-stack"
      className={cn("neumo-surface relative w-full py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <header className="mb-12 flex flex-col items-start gap-3 md:mb-16">
          <span className="neumo-inset-sm inline-flex w-fit items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-medium tracking-wider text-[color:var(--neumo-accent-text)] uppercase">
            <Layers3Icon className="size-3" />
            技术架构 · Tech Stack
          </span>
          <h2 className="font-serif text-3xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-4xl">
            一套
            <span className="text-[color:var(--neumo-accent-text)]">
              {" "}
              分层架构
            </span>
            ，一处升级全栈复用
          </h2>
          <p className="max-w-2xl text-base leading-7 text-[color:var(--neumo-text-muted)]">
            从用户界面到数据底座，分成五层。每一层都可以替换或扩展——
            你既可以整套接管，也能单点改造，模型变了 / 工具变了 /
            部署换了都不用推倒重来。
          </p>
        </header>

        <div className="space-y-5">
          {LAYERS.map((layer, idx) => (
            <LayerCard
              key={layer.id}
              layer={layer}
              isLast={idx === LAYERS.length - 1}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

function LayerCard({
  layer,
  isLast,
}: {
  layer: TechLayer;
  isLast: boolean;
}) {
  const Icon = layer.icon;
  return (
    <div className="relative">
      {/* 层间连接线（最后一条不画） */}
      {!isLast && (
        <div className="absolute top-full -bottom-5 left-[44px] hidden h-5 w-px bg-[color:var(--neumo-text-subtle)]/40 md:block" />
      )}
      <article
        className={cn(
          "neumo-raised flex flex-col gap-5 rounded-2xl p-6 md:flex-row md:items-stretch md:gap-8 md:p-7",
          layer.position === "tall" && "md:items-start",
        )}
      >
        {/* 左：图标 + 编号 */}
        <div className="flex shrink-0 items-start gap-3 md:w-44 md:flex-col md:items-start md:gap-3">
          <span className="neumo-inset-sm inline-flex size-11 items-center justify-center rounded-2xl text-[color:var(--neumo-accent-text)]">
            <Icon className="size-5" />
          </span>
          <div>
            <div className="font-mono text-[11px] font-semibold tracking-widest text-[color:var(--neumo-accent-text)] uppercase">
              {layer.index}
            </div>
            <div className="mt-1 text-base font-semibold text-[color:var(--neumo-text)] md:text-lg">
              {layer.title}
            </div>
            <div className="mt-0.5 text-[10px] tracking-widest text-[color:var(--neumo-text-subtle)] uppercase">
              {layer.subtitle}
            </div>
          </div>
        </div>

        {/* 中：描述 */}
        <div className="shrink-0 md:w-64">
          <p className="text-sm leading-6 text-[color:var(--neumo-text-muted)]">
            {layer.desc}
          </p>
        </div>

        {/* 右：能力项 chip 流 */}
        <div className="flex-1">
          <ul className="flex flex-wrap gap-2">
            {layer.items.map((item) => (
              <li
                key={item}
                className="neumo-inset-sm rounded-full px-3 py-1.5 text-xs font-medium text-[color:var(--neumo-text)]"
              >
                {item}
              </li>
            ))}
          </ul>
        </div>
      </article>
    </div>
  );
}
