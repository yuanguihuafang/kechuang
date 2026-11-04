import { describe, expect, it } from "@rstest/core";

import {
  createBuildEnvironment,
  evaluateBudgets,
  extractAssetPaths,
  formatMeasurementSummary,
  ROUTES,
} from "../../../scripts/measure-route-assets.mjs";

describe("route asset measurement", () => {
  it("covers every approved representative route", () => {
    // /[lang]/docs and /blog/* are intentionally absent — both route
    // trees were deleted. The list is the source of truth: any route
    // someone wants to track must live here, not implicitly.
    expect(ROUTES).not.toContain("/en/docs");
    expect(ROUTES).not.toContain("/blog/posts");
    expect(ROUTES).toContain("/login");
    expect(ROUTES).toContain("/workspace/chats");
  });

  it("removes an inherited static-mode flag from the normal build", () => {
    expect(
      createBuildEnvironment(false, {
        KEEP_ME: "yes",
        NEXT_PUBLIC_STATIC_WEBSITE_ONLY: "true",
      }),
    ).toEqual({ KEEP_ME: "yes" });
    expect(createBuildEnvironment(true, {})).toEqual({
      NEXT_PUBLIC_STATIC_WEBSITE_ONLY: "true",
    });
  });

  it("extracts unique Next.js scripts and styles", () => {
    const html = `
      <link rel="stylesheet" href="/_next/static/css/app.css?dpl=1">
      <script src="/_next/static/chunks/app.js"></script>
      <script src="/_next/static/chunks/app.js"></script>
      <script src="https://cdn.example.com/external.js"></script>
    `;

    expect(extractAssetPaths(html)).toEqual({
      css: ["css/app.css"],
      js: ["chunks/app.js"],
    });
  });

  it("reports every route budget overage with exact values", () => {
    const result = evaluateBudgets(
      {
        "/": { css: 101, js: 200 },
        "/login": { css: 12, js: 301 },
      },
      {
        "/": { css: 100, js: 200 },
        "/login": { css: 12, js: 300 },
      },
    );

    expect(result).toEqual([
      "/ css: 101 bytes exceeds 100 bytes by 1 byte",
      "/login js: 301 bytes exceeds 300 bytes by 1 byte",
    ]);
  });

  it("formats a stable human-readable route summary", () => {
    expect(
      formatMeasurementSummary({
        "/": { css: 12, js: 345, html: 67, buildMode: "static-demo" },
      }),
    ).toBe(
      "Route asset summary\n/ [static-demo] JS 345 B | CSS 12 B | HTML 67 B",
    );
  });
});
