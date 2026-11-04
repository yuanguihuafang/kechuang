import assert from "node:assert/strict";
import test from "node:test";
import {
  examples,
  externalQuickIntents,
  quickIntents,
} from "./quickIntents.js";

test("外部首页保留流程入口并将三个固定问题单独作为普通示例", () => {
  assert.deepEqual(
    externalQuickIntents.map(({ intent }) => intent),
    ["demand", "supply_draft", "supply_demand_match"],
  );
  assert.deepEqual(
    externalQuickIntents.map(({ label }) => label),
    ["我有需求", "我有方案 & 成果", "供需匹配"],
  );
  assert.deepEqual(
    quickIntents.map(({ intent }) => intent),
    [
      "enterprise_profile",
      "demand_discovery",
      "supply_demand_match",
      "customer_visit",
    ],
  );
  assert.deepEqual(examples, [
    "我有创新需求，帮我匹配专家或成果",
    "我有技术成果，帮我寻找潜在客户",
    "我有智能化升级需求，帮我推荐方案商",
  ]);
});
