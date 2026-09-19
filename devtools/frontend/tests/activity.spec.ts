import { test, expect } from "./fixtures";

test.beforeEach(async ({ page, backend }) => {
  await page.goto(backend.url);
  await expect(page.locator(".connection")).toHaveText("Live");
  await page.clock.install();
  await page.clock.pauseAt(new Date());
  await page.clock.runFor(1_000);
  await page.getByRole("button", { name: "Activity", exact: true }).click();
});

test("request rate follows display filters and expires while display is paused", async ({
  page,
  backend,
}, testInfo) => {
  const rate = page.getByLabel("Requests per second", { exact: true });
  await expect(rate).toHaveText("0");
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 10 } },
  ]);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 20 } },
  ]);
  await backend.mutate([
    {
      type: "insert",
      schema: "BrowserRows",
      row_id: "sample",
      values: { title: "hello" },
    },
  ]);
  await expect(rate).toHaveText("3");
  const schema = page
    .locator(".filter-bar")
    .getByLabel("Schema", { exact: false });
  await schema.selectOption("BrowserRows");
  await expect(rate).toHaveText("1");
  await schema.selectOption("BrowserModel");
  await expect(rate).toHaveText("2");
  await page.getByLabel("Field filter", { exact: true }).fill("title");
  await expect(rate).toHaveText("0");
  await page.getByRole("button", { name: "Clear display filters" }).click();
  await expect(rate).toHaveText("3");
  await page.getByLabel("Category", { exact: true }).selectOption("response");
  await expect(rate).toHaveText("0");
  await page.getByRole("button", { name: "Clear display filters" }).click();
  await page.getByRole("button", { name: "Pause display" }).click();
  const frozenCount = await page.locator(".activity-split .list-item").count();
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 30 } },
  ]);
  await page.clock.runFor(250);
  await expect(rate).toHaveText("4");
  await expect(page.locator(".activity-split .list-item")).toHaveCount(
    frozenCount,
  );
  await page.screenshot({
    path: testInfo.outputPath("activity-rate-wide.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(rate).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("activity-rate-narrow.png"),
    fullPage: true,
  });
  await page.clock.runFor(1_000);
  await expect(rate).toHaveText("0");
});

test("rates survive tab changes but do not count snapshots on reconnect", async ({
  page,
  backend,
}) => {
  const rate = page.getByLabel("Requests per second", { exact: true });
  await page.getByRole("button", { name: "State", exact: true }).click();
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 10 } },
  ]);
  await expect(page.locator('.state-data [data-field="x"]')).toContainText(
    "10",
  );
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  await expect(page.locator(".connection")).toHaveText("Live");
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(rate).toHaveText("1");
  await backend.disconnectBrowsers();
  await expect(page.locator(".connection")).toContainText("Disconnected");
  await expect(rate).toHaveText("—");
  await page.clock.runFor(800);
  await expect(page.locator(".connection")).toHaveText("Live");
  await expect(rate).toHaveText("0");
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 20 } },
  ]);
  await expect(rate).toHaveText("1");
});

test("recording exclusions are visible and can be cleared", async ({
  page,
  backend,
}) => {
  const rate = page.getByLabel("Requests per second", { exact: true });
  await page
    .locator(".filter-bar")
    .getByLabel("Schema", { exact: false })
    .selectOption("BrowserRows");
  await page
    .getByRole("button", { name: "Use filters for recording" })
    .click();
  await expect(page.locator(".recording-filters")).toContainText(
    "schema=BrowserRows",
  );
  await page.getByRole("button", { name: "Clear display filters" }).click();
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 10 } },
  ]);
  await expect(rate).toHaveText("0");
  await page.getByRole("button", { name: "Clear recording filters" }).click();
  await expect(page.locator(".recording-filters")).toHaveCount(0);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 20 } },
  ]);
  await expect(rate).toHaveText("1");
  await page.getByRole("button", { name: "Pause recording" }).click();
  await expect(rate).toHaveText("—");
  await expect(page.locator(".activity-rate")).toContainText(
    "Recording paused",
  );
  await page.getByRole("button", { name: "Resume recording" }).click();
  await expect(rate).toHaveText("0");
});
