import {
  Link2Icon,
  CompassIcon,
  NewspaperIcon,
  SendIcon,
  SproutIcon,
  NetworkIcon,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

type Scenario = {
  title: string;
  en: string;
  tag: string; // 中小制造 / 工业寻源 / 媒体广电 / 外贸出海 / 智慧农业 / 产业集群
  desc: string;
  icon: LucideIcon;
};

// 6 大行业，源自「国家级双跨平台 · 工业互联网」实践库
const SCENARIOS: Scenario[] = [
  {
    title: "链管家",
    en: "L-Guardian",
    tag: "中小制造",
    desc: "给存量 ERP 加一层自然语言网关。新员工 1 天上手，录单准确率显著提升，跨菜单点击下降 60%。",
    icon: Link2Icon,
  },
  {
    title: "工业寻源",
    en: "SourceIt",
    tag: "采购寻源",
    desc: "三段 Skill 化：需求理解 → 物料认知 → 产品-供应商匹配。30+ 数据源秒级输出可落地报告。",
    icon: CompassIcon,
  },
  {
    title: "广电数字员工",
    en: "MediaCrew",
    tag: "媒体广电",
    desc: "192 家主流媒体自动扫描，每日 25 条选题；智能派单直达记者；80% 重复劳动交给 AI。",
    icon: NewspaperIcon,
  },
  {
    title: "VELA · AI 获客",
    en: "VELA",
    tag: "外贸出海",
    desc: "多源线索采集 + AI 背调 + 千人千面开发信，邮箱 / LinkedIn / WhatsApp 协同触达，从「被搜索」到「被 AI 推荐」。",
    icon: SendIcon,
  },
  {
    title: "夏小肚",
    en: "XiaLab",
    tag: "智慧农业",
    desc: "拍照问诊 ≤30 秒给方案；Day0-N 全流程陪护；多模态入口适合田间地头；双层知识库引流 + 付费。",
    icon: SproutIcon,
  },
  {
    title: "产业大脑",
    en: "ClusterOS",
    tag: "产业集群",
    desc: "撮合调度员 / 排产中枢 / 金融授信员 / 政企联络员——集群级统一入口与调度中枢。",
    icon: NetworkIcon,
  },
];

export function ScenariosSection({ className }: { className?: string }) {
  return (
    <section
      data-reveal
      id="industries"
      className={cn("neumo-surface relative w-full py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <header className="mb-12 flex flex-col items-start gap-3 md:mb-16">
          <span className="neumo-inset-sm inline-flex w-fit items-center rounded-full px-3 py-1 text-[11px] font-medium tracking-wider text-[color:var(--neumo-accent-text)] uppercase">
            行业落地示例 · INDUSTRIES
          </span>
          <h2 className="font-serif text-3xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-4xl">
            已用 OPIE OS 落地
            <span className="text-[color:var(--neumo-accent-text)]">
              多个行业
            </span>
            <span className="ml-2">的形态示例</span>
          </h2>
          <p className="max-w-2xl text-base leading-7 text-[color:var(--neumo-text-muted)]">
            OPIE OS 是一套通用的 Agent
            操作系统——下面是几个已经跑通、可以看到真实业务结果的行业示例。
            你所在的行业大概率不在列表里，但底座方法论是通用的，可以复用。
          </p>
        </header>

        {/* 4 → 6：网格由 2 列改为 3×2，每张卡片高度更紧，沿用 neumo-raised 样式 */}
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
          {SCENARIOS.map((scenario) => (
            <ScenarioCard key={scenario.title} scenario={scenario} />
          ))}
        </div>
      </div>
    </section>
  );
}

function ScenarioCard({ scenario }: { scenario: Scenario }) {
  const Icon = scenario.icon;
  return (
    <article className="neumo-raised group flex flex-col gap-4 rounded-2xl p-6 transition-all duration-300 ease-in-out md:p-7">
      <span className="neumo-inset-sm inline-flex size-12 shrink-0 items-center justify-center rounded-2xl text-[color:var(--neumo-accent-text)]">
        <Icon className="size-5" />
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <h3 className="text-lg font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-xl">
            {scenario.title}
          </h3>
          <span className="text-xs font-medium text-[color:var(--neumo-accent-text)]/80">
            {scenario.en}
          </span>
        </div>
        <span className="mt-1 inline-flex items-center rounded-full bg-[color:var(--neumo-inset-bg)] px-2 py-0.5 text-[10.5px] font-medium tracking-wider text-[color:var(--neumo-text-muted)] uppercase">
          {scenario.tag}
        </span>
        <p className="mt-3 text-sm leading-6 text-[color:var(--neumo-text-muted)] md:text-base md:leading-7">
          {scenario.desc}
        </p>
      </div>
    </article>
  );
}
