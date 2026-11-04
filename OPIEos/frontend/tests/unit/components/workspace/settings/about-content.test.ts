import { afterEach, expect, test, rs } from "@rstest/core";

const original = process.env.NEXT_PUBLIC_APP_VERSION;

afterEach(() => {
  rs.resetModules();
  if (original === undefined) {
    delete process.env.NEXT_PUBLIC_APP_VERSION;
  } else {
    process.env.NEXT_PUBLIC_APP_VERSION = original;
  }
});

test("aboutMarkdown heading interpolates the app version", async () => {
  process.env.NEXT_PUBLIC_APP_VERSION = "9.9.9-test";
  const { aboutMarkdown } =
    await import("@/components/workspace/settings/about-content");
  // The top-level title still brands as "About OPIE OS", and the version
  // stamp is inlined into the lead paragraph so it reflects the env override.
  expect(aboutMarkdown).toContain("# About OPIE OS");
  expect(aboutMarkdown).toContain("**OPIE OS** (v9.9.9-test)");
});

test("aboutMarkdown heading reflects the package version when env is unset", async () => {
  delete process.env.NEXT_PUBLIC_APP_VERSION;
  const { APP_VERSION } = await import("@/version");
  const { aboutMarkdown } =
    await import("@/components/workspace/settings/about-content");
  // Positive: the lead paragraph carries the real resolved version. This
  // catches an empty or undefined APP_VERSION interpolation (`(v)` /
  // `(vundefined)`), not just removal of the old literal.
  expect(aboutMarkdown).toContain(`**OPIE OS** (v${APP_VERSION})`);
});
