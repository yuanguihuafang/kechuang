import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "@rstest/core";

const frontendRoot = join(import.meta.dirname, "../../../..");

describe("landing section animation scheduling", () => {
  it("drives the hero terminal typing animation via sequenced effects", () => {
    const source = readFileSync(
      join(frontendRoot, "src/components/landing/hero.tsx"),
      "utf8",
    );

    // The hero uses a self-contained terminal typing effect: two
    // effects (one to advance the queue, one to type within the current
    // token) plus a blinking caret.
    expect(source).toContain("useState<TerminalToken[]");
    expect(source).toContain("setShown((prev) => [...prev, next])");
    expect(source).toContain("setTyped((n) => n + 1)");
    expect(source).toContain("animate-pulse");
  });

  it("renders the capability bento without decorative render activity", () => {
    const source = readFileSync(
      join(
        frontendRoot,
        "src/components/landing/sections/capability-bento-section.tsx",
      ),
      "utf8",
    );

    // The section is pure CSS hover — no pointermove listeners or rAF.
    expect(source).not.toContain("useRenderActivity");
    expect(source).not.toContain("pointermove");
    expect(source).not.toContain("MagicBento");

    // The six product capabilities are present.
    expect(source).toContain("任务规划");
    expect(source).toContain("多模型调度");
    expect(source).toContain("工具与技能");
    expect(source).toContain("隔离执行");
    expect(source).toContain("结构化记忆");
    expect(source).toContain("团队协作");
  });

  it("home page references the four reorganized sections", () => {
    const source = readFileSync(join(frontendRoot, "src/app/page.tsx"), "utf8");

    expect(source).toContain("Hero");
    expect(source).toContain("CapabilityBentoSection");
    expect(source).toContain("ScenariosSection");
    expect(source).toContain("ArchitectureSection");
    // CtaSection (announcements / "了解最新动态") was removed when the
    // docs + blog routes were dropped — public landing has no company
    // announcement feed any more.
    expect(source).not.toContain("CtaSection");
    // Demo stream was removed in the rewrite.
    expect(source).not.toContain("DemoStreamSection");
  });
});
