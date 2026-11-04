import { chromium } from "../frontend/node_modules/.pnpm/playwright@1.59.1/node_modules/playwright/index.mjs";

const baseURL = process.env.OPIE_BASE_URL || "http://localhost:18026";
const password = process.env.OPIE_TEST_PASSWORD || "TestPass-2026!";
const emails = (process.env.OPIE_TEST_EMAILS || "load-3@example.com,load-4@example.com,load-5@example.com,load-6@example.com,load-7@example.com")
  .split(",").map((v) => v.trim()).filter(Boolean);

if (emails.length !== 5) throw new Error("OPIE_TEST_EMAILS must contain exactly 5 emails");

const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const results = await Promise.all(emails.map(async (email, index) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    const marker = `E2E_LOAD_USER_${index + 1}_${Date.now()}`;
    try {
      await page.goto(`${baseURL}/login`, { waitUntil: "domcontentloaded" });
      await page.getByPlaceholder("you@example.com").fill(email);
      await page.getByPlaceholder("•••••••").fill(password);
      await page.getByRole("button", { name: "登录" }).click();
      await page.waitForURL(/\/workspace\/chats\/new/, { timeout: 15000 });
      const box = page.getByRole("textbox", { name: "今天我能为你做些什么？" });
      await box.fill(marker);
      await page.getByRole("button", { name: "Submit" }).click();
      await page.waitForTimeout(2500);
      const url = page.url();
      return { email, marker, url, ok: /\/workspace\/chats\//.test(url) && !url.endsWith("/new") };
    } finally {
      await context.close();
    }
  }));
  console.log(JSON.stringify({ baseURL, count: results.length, passed: results.filter((r) => r.ok).length, results }, null, 2));
  if (results.some((r) => !r.ok)) process.exitCode = 1;
} finally {
  await browser.close();
}
