import { chromium } from "../frontend/node_modules/.pnpm/playwright@1.59.1/node_modules/playwright/index.mjs";

const baseURL = process.env.OPIE_BASE_URL || "http://localhost:18026";
const password = process.env.OPIE_TEST_PASSWORD || "TestPass-2026!";
const emails = (process.env.OPIE_TEST_EMAILS || "load-3@example.com,load-4@example.com,load-5@example.com,load-6@example.com,load-7@example.com").split(",").map((v) => v.trim());
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const sessions = await Promise.all(emails.map(async (email, i) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(`${baseURL}/login`);
    await page.getByPlaceholder("you@example.com").fill(email);
    await page.getByPlaceholder("•••••••").fill(password);
    await page.getByRole("button", { name: "登录" }).click();
    await page.waitForURL(/\/workspace\/chats\/new/);
    const marker = `E2E_ISOLATION_${i + 1}_${Date.now()}`;
    await page.getByRole("textbox", { name: "今天我能为你做些什么？" }).fill(marker);
    await page.getByRole("button", { name: "Submit" }).click();
    await page.waitForURL(/\/workspace\/chats\/(?!new)/, { timeout: 20000 });
    return { email, context, page, threadURL: page.url(), marker };
  }));

  const checks = [];
  for (let i = 0; i < sessions.length; i++) {
    const attacker = sessions[i];
    const target = sessions[(i + 1) % sessions.length];
    await attacker.page.goto(target.threadURL);
    await attacker.page.waitForTimeout(1200);
    const body = await attacker.page.locator("body").innerText();
    checks.push({ from: attacker.email, target: target.email, url: target.threadURL, isolated: /not found|未找到|不存在/i.test(body) && !body.includes(target.marker) });
  }
  console.log(JSON.stringify({ count: checks.length, passed: checks.filter((x) => x.isolated).length, checks }, null, 2));
  if (checks.some((x) => !x.isolated)) process.exitCode = 1;
  for (const s of sessions) await s.context.close();
} finally { await browser.close(); }
