import { test, expect } from "./fixtures";

test("structured singleton drafts stay local, resume intact and submit changed fields once", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await expect(page.locator(".connection")).toHaveText("Live");
  await page.getByRole("button", { name: "Edit values", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("x", { exact: true }).fill("42");
  await dialog
    .getByLabel("History label", { exact: true })
    .fill("Set counter");
  await dialog.getByRole("button", { name: "Keep draft and browse" }).click();
  await expect(page.locator('.state-data [data-field="x"]')).toContainText(
    "0",
  );
  await page.getByRole("button", { name: "Resume draft" }).click();
  await expect(dialog.getByLabel("x", { exact: true })).toHaveValue("42");
  await expect(
    dialog.getByLabel("History label", { exact: true }),
  ).toHaveValue("Set counter");
  await dialog.getByRole("button", { name: "Review before applying" }).click();
  await expect(dialog.locator(".changes-table")).toContainText("42");
  const request = page.waitForRequest("**/api/commands");
  await dialog.getByRole("button", { name: "Apply transaction" }).click();
  const transaction = (await request).postDataJSON().requests[0];
  expect(transaction.actions).toEqual([
    { type: "set", schema: "BrowserModel", values: { x: 42 } },
  ]);
  expect(transaction.history_label).toBe("Set counter");
  expect(transaction.expected_revisions).toEqual({ BrowserModel: 0 });
  await expect(dialog).not.toBeVisible();
  await expect(page.locator('.state-data [data-field="x"]')).toContainText(
    "42",
  );
  await page.getByRole("button", { name: "Undo last" }).click();
  await expect(page.locator('.state-data [data-field="x"]')).toContainText(
    "0",
  );
});

test("collection table keeps row identity through sorting, views and removal", async ({
  page,
  backend,
}) => {
  await backend.mutate(
    ["b", "a"].map((id) => ({
      type: "insert",
      schema: "BrowserRows",
      row_id: id,
      values: { title: id },
    })),
  );
  await page.goto(backend.url);
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  const table = page.locator(".collection-table");
  await table.getByRole("button", { name: "b", exact: true }).click();
  await table.getByRole("button", { name: "title", exact: true }).click();
  await expect(page.getByLabel("Row ID", { exact: true })).toHaveValue("b");
  await page.getByRole("button", { name: "Dictionary", exact: true }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText('"a"');
  await page.getByRole("button", { name: "Structured", exact: true }).click();
  await page.getByLabel("Search rows").fill("b");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await backend.mutate([
    { type: "remove", schema: "BrowserRows", row_id: "b" },
  ]);
  await expect(
    page.getByRole("region", { name: "Selected row details" }),
  ).toContainText("no longer present");
  await expect(page.getByLabel("Row ID", { exact: true })).toHaveValue("b");
  await expect(
    page.getByRole("button", { name: "Remove row", exact: true }),
  ).toBeDisabled();
});

test("one collection draft can edit, remove and insert atomically", async ({
  page,
  backend,
}) => {
  await backend.mutate(
    ["a", "b"].map((id) => ({
      type: "insert",
      schema: "BrowserRows",
      row_id: id,
      values: { title: id },
    })),
  );
  await page.goto(backend.url);
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  await page.getByRole("button", { name: "Edit values", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("a / number", { exact: true })
    .fill("9007199254740993");
  await dialog
    .locator("tbody tr")
    .filter({ has: page.getByLabel("b / title", { exact: true }) })
    .getByRole("button", { name: "Remove row", exact: true })
    .click();
  await dialog.getByRole("button", { name: "Add row to draft" }).click();
  await dialog.getByLabel("New row ID").fill("c");
  await dialog.getByLabel("c / title", { exact: true }).fill("new record");
  await dialog.getByRole("button", { name: "Review before applying" }).click();
  const request = page.waitForRequest("**/api/commands");
  await dialog.getByRole("button", { name: "Apply transaction" }).click();
  const batch = (await request).postDataJSON();
  expect(batch.requests).toHaveLength(1);
  expect(batch.requests[0].actions).toEqual([
    {
      type: "setrow",
      schema: "BrowserRows",
      row_id: "a",
      values: { number: { $integer: "9007199254740993" } },
    },
    { type: "remove", schema: "BrowserRows", row_id: "b" },
    {
      type: "insert",
      schema: "BrowserRows",
      row_id: "c",
      values: { title: "new record", number: 0, payload: null },
    },
  ]);
  await expect(dialog).not.toBeVisible();
  await expect(
    page.locator('.collection-table [data-row-id="a"]'),
  ).toContainText("9007199254740993");
  await expect(
    page.locator('.collection-table [data-row-id="c"]'),
  ).toContainText("new record");
  await page.getByRole("button", { name: "Undo last" }).click();
  await expect(
    page.locator('.collection-table [data-row-id="b"]'),
  ).toBeVisible();
  await expect(
    page.locator('.collection-table [data-row-id="c"]'),
  ).toHaveCount(0);
});

test("stale structured drafts compare explicitly and preserve unrelated remote changes", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await page.getByRole("button", { name: "Edit values", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("x", { exact: true }).fill("bad");
  await expect(
    dialog.getByRole("button", { name: "Review before applying" }),
  ).toBeDisabled();
  await dialog.getByLabel("x", { exact: true }).fill("25");
  await backend.mutate([
    {
      type: "set",
      schema: "BrowserModel",
      values: { x: 8, title: "Remote title" },
    },
  ]);
  await dialog.getByRole("button", { name: "Review before applying" }).click();
  await expect(
    dialog.getByRole("button", { name: "Apply transaction" }),
  ).toBeDisabled();
  await dialog
    .getByRole("button", { name: "Compare with current", exact: true })
    .click();
  const comparison = dialog.getByRole("region", {
    name: "Compare draft with current",
  });
  await expect(comparison).toContainText("25");
  await expect(comparison).toContainText("8");
  await dialog
    .getByRole("button", { name: "Keep draft with reviewed revisions" })
    .click();
  const request = page.waitForRequest("**/api/commands");
  await dialog.getByRole("button", { name: "Apply transaction" }).click();
  expect((await request).postDataJSON().requests[0].actions[0].values).toEqual(
    { x: 25 },
  );
  await expect(dialog).not.toBeVisible();
  await expect(page.locator('.state-data [data-field="title"]')).toContainText(
    "Remote title",
  );
});

test("connected activity panels explain the operation and preview retained states", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 7 } },
  ]);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 9 } },
  ]);
  await expect(page.locator('.state-data [data-field="x"]')).toContainText(
    "9",
  );
  await page
    .getByRole("button", { name: "Activity", exact: true })
    .first()
    .click();
  await page.getByLabel("Category", { exact: true }).selectOption("commit");
  await expect(page.locator(".changes-table")).toContainText("9");
  await page.getByRole("button", { name: "Request", exact: true }).click();
  await expect(page.locator(".operation")).toContainText("set");
  await page.getByRole("button", { name: "Response", exact: true }).click();
  await expect(page.locator(".operation")).toContainText("transaction");
  await page
    .getByRole("button", { name: "State afterward", exact: true })
    .click();
  await page
    .locator(".operation")
    .getByRole("button", { name: /Preview/ })
    .click();
  await expect(page.locator(".preview-banner")).toContainText(
    "Historical preview",
  );
  await expect(
    page.getByRole("button", { name: "Edit values", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Previous commit" }).click();
  await expect(page.locator('.state-data [data-field="x"]')).toContainText(
    "7",
  );
  await page.getByRole("button", { name: "Return to live" }).click();
  await expect(page.locator('.state-data [data-field="x"]')).toContainText(
    "9",
  );
});

test("queries show table results and the structured view fits wide and narrow screens", async ({
  page,
  backend,
}, testInfo) => {
  await backend.mutate([
    {
      type: "insert",
      schema: "BrowserRows",
      row_id: "sample",
      values: { title: "hello" },
    },
  ]);
  await page.goto(backend.url);
  await page.getByText("Run a query", { exact: true }).click();
  await page.getByRole("button", { name: "Run query", exact: true }).click();
  await expect(
    page
      .getByRole("region", { name: "Query results", exact: true })
      .locator(".collection-table"),
  ).toContainText("hello");
  await page.getByText("Run a query", { exact: true }).click();
  await page.screenshot({
    path: testInfo.outputPath("state-wide.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Edit values", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("draft-narrow.png"),
    fullPage: true,
  });
});
