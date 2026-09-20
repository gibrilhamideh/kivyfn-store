import { test, expect } from "./fixtures";

// Existing protocol checks use the dictionary view; views.spec exercises the
// structured defaults against the same real backends.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    for (const name of ["State", "History", "Query", "Activity"])
      localStorage.setItem("kivyfn.view." + name, "dictionary");
  });
  page.on("dialog", (dialog) => void dialog.accept());
});

test("guarded edits preserve dirty input, then undo and redo the shared head", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await expect(
    page.getByRole("status").filter({ hasText: /^Live$/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Edit as new mutation" }).click();
  const input = page.getByLabel("Actions", { exact: true });
  const draft = '[{"type":"set","schema":"BrowserModel","values":{"x":42}}]';
  await input.fill(draft);
  await backend.mutate([
    {
      type: "set",
      schema: "BrowserModel",
      values: { title: "remote update" },
    },
  ]);
  await expect(input).toHaveValue(draft);
  await expect(
    page.getByText("Source changed.", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(page.getByRole("alert")).toContainText(/stale|revision/i);
  await page.getByRole("button", { name: "Use current revisions" }).click();
  await page.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 42',
  );
  await page.getByRole("button", { name: "Close draft" }).click();
  await page.getByRole("button", { name: "Undo last" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 0',
  );
  await page.getByRole("button", { name: "Redo next" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 42',
  );
});

test("captured preview stays isolated during application writes", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 7 } },
  ]);
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 7',
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByRole("button", { name: "Preview captured state" }).click();
  await expect(page.locator(".preview-banner")).toContainText(
    "Historical preview",
  );
  await expect(page.getByRole("button", { name: "Undo last" })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Edit as new mutation" }),
  ).toBeDisabled();
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 9 } },
  ]);
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 7',
  );
  await page.getByRole("button", { name: "Return to live" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 9',
  );
});

test("integer-like row IDs remain selectable after structural undo", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await backend.mutate(
    ["10", "2", "1"].map((id) => ({
      type: "insert",
      schema: "BrowserRows",
      row_id: id,
      values: { title: id },
    })),
  );
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  const rows = page.getByLabel("Row ID");
  await expect(rows.locator("option")).toHaveText(["1", "2", "10"]);
  await backend.mutate([
    { type: "remove", schema: "BrowserRows", row_id: "2" },
  ]);
  await expect(rows.locator("option")).toHaveText(["1", "10"]);
  await page.getByRole("button", { name: "Undo last" }).click();
  await expect(rows.locator("option")).toHaveText(["1", "2", "10"]);
});

test("display and recording pauses keep canonical State live, reconnect replaces stale data", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByRole("button", { name: "Pause display" }).click();
  await page.getByRole("button", { name: "Pause recording" }).click();
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 12 } },
  ]);
  await page.getByRole("button", { name: "State", exact: true }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 12',
  );
  await backend.disconnectBrowsers();
  await expect(page.locator(".connection")).toContainText("Disconnected");
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 14 } },
  ]);
  await expect(page.locator(".connection")).toHaveText("Live");
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 14',
  );
  await page.getByRole("button", { name: "Undo last" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 12',
  );
});

test("signals, live query rerun, and copy use original requests", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await page.getByText("Declared signals", { exact: true }).click();
  await page.getByRole("button", { name: "Draft signal" }).first().click();
  await page
    .getByLabel("Actions", { exact: true })
    .fill(
      '[{"type":"signal","schema":"BrowserModel","signal":"open_window","data":{"window_id":"main"}}]',
    );
  await page.getByRole("button", { name: "Apply new mutation" }).click();
  await page.getByRole("button", { name: "Close draft" }).click();
  await expect(page.getByRole("button", { name: "Undo last" })).toBeEnabled();
  await page.getByRole("button", { name: "History", exact: true }).click();
  await expect(page.locator(".details")).toContainText("close_window");
  await page.getByRole("button", { name: "Copy as new mutation" }).click();
  await expect(page.getByLabel("Actions", { exact: true })).toContainText(
    "open_window",
  );
  await page.getByRole("button", { name: "Close draft" }).click();
  await page.getByRole("button", { name: "State", exact: true }).click();
  await page.getByText("Run a query", { exact: true }).click();
  await page.getByRole("button", { name: "Run query", exact: true }).click();
  await expect(
    page.locator("details").filter({ hasText: "Run a query" }),
  ).toContainText('"type": "query"');
});

test("large integers and captured markup are inert in a narrow layout", async ({
  page,
  backend,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(backend.url);
  await backend.mutate([
    {
      type: "set",
      schema: "BrowserModel",
      values: { title: '<img src=x onerror="window.hacked=true">' },
    },
  ]);
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    "onerror",
  );
  expect(await page.evaluate(() => "hacked" in window)).toBe(false);
  await page.getByRole("button", { name: "Edit as new mutation" }).click();
  await page
    .getByLabel("Actions", { exact: true })
    .fill(
      '[{"type":"set","schema":"BrowserModel","values":{"x":9007199254740993}}]',
    );
  await page.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"$integer": "9007199254740993"',
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("lost HTTP response retries the same ID after the real commit arrives", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await page.getByRole("button", { name: "Edit as new mutation" }).click();
  await page
    .getByLabel("Actions", { exact: true })
    .fill('[{"type":"set","schema":"BrowserModel","values":{"x":23}}]');
  let identity = "";
  await page.route(
    "**/api/commands",
    async (route) => {
      identity = route.request().postDataJSON().requests[0]
        .transaction_id as string;
      await route.fetch();
      await route.abort("failed");
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 23',
  );
  await expect(
    page.getByRole("button", { name: "Retry same command ID" }),
  ).toBeVisible();
  const retryRequest = page.waitForRequest((request) =>
    request.url().endsWith("/api/commands"),
  );
  await page.getByRole("button", { name: "Retry same command ID" }).click();
  expect((await retryRequest).postDataJSON().requests[0].transaction_id).toBe(
    identity,
  );
  await expect(
    page.getByRole("button", { name: "Retry same command ID" }),
  ).not.toBeVisible();
  await page.getByRole("button", { name: "Close draft" }).click();
  await page.getByRole("button", { name: "Undo last" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 0',
  );
  await expect(page.getByRole("button", { name: "Undo last" })).toBeDisabled();
});

test("prototype-like row IDs remain ordinary rows through remove and undo", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  await expect(page.getByLabel("Row ID")).toContainText("Empty collection");
  await backend.mutate(
    ["__proto__", "toString"].map((id) => ({
      type: "insert",
      schema: "BrowserRows",
      row_id: id,
      values: { title: id },
    })),
  );
  const rows = page.getByLabel("Row ID");
  await expect(rows.locator("option")).toHaveText(["__proto__", "toString"]);
  await backend.mutate([
    { type: "remove", schema: "BrowserRows", row_id: "__proto__" },
  ]);
  await expect(rows.locator("option")).toHaveText(["toString"]);
  await page.getByRole("button", { name: "Undo last" }).click();
  await expect(rows.locator("option")).toHaveText(["toString", "__proto__"]);
  await rows.selectOption("__proto__");
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"title": "__proto__"',
  );
});

test("keyboard applies and live synchronization tolerates both HTTP and WebSocket arrival orders", async ({
  page,
  backend,
}) => {
  const held: (string | Buffer)[] = [];
  let holdCommits = true;
  let releaseCommits = () => undefined;
  await page.routeWebSocket("**/stream*", (socket) => {
    const server = socket.connectToServer();
    server.onMessage((message) => {
      const envelope = JSON.parse(message.toString()) as { kind: string };
      if (holdCommits && (held.length > 0 || envelope.kind === "commit")) {
        held.push(message);
      } else socket.send(message);
    });
    releaseCommits = () => {
      holdCommits = false;
      held.splice(0).forEach((message) => socket.send(message));
    };
  });
  await page.goto(backend.url);
  await page.getByRole("button", { name: "Edit as new mutation" }).click();
  const input = page.getByLabel("Actions", { exact: true });
  await input.fill(
    '[{"type":"set","schema":"BrowserModel","values":{"x":31}}]',
  );
  const responseFirst = page.waitForResponse((response) =>
    response.url().endsWith("/api/commands"),
  );
  await input.press("Control+Enter");
  await responseFirst;
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 0',
  );
  releaseCommits();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 31',
  );
  await input.fill(
    '[{"type":"set","schema":"BrowserModel","values":{"x":32}}]',
  );
  await page.getByRole("button", { name: "Use current revisions" }).click();
  let releaseResponse: () => void = () => undefined;
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  await page.route(
    "**/api/commands",
    async (route) => {
      const response = await route.fetch();
      await responseGate;
      await route.fulfill({ response });
    },
    { times: 1 },
  );
  await input.press("Control+Enter");
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 32',
  );
  await expect(
    page.getByRole("button", { name: "Apply new mutation" }),
  ).toBeDisabled();
  releaseResponse();
  await expect(
    page.getByText("Transaction confirmed.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Apply new mutation" }),
  ).toBeDisabled();
});

test("empty and loading controls stay disabled; selecting old history never changes the undo target", async ({
  page,
  backend,
}) => {
  let releaseInfo: () => void = () => undefined;
  const infoGate = new Promise<void>((resolve) => {
    releaseInfo = resolve;
  });
  await page.route(
    "**/api/info",
    async (route) => {
      const response = await route.fetch();
      await infoGate;
      await route.fulfill({ response });
    },
    { times: 1 },
  );
  await page.goto(backend.url, { waitUntil: "domcontentloaded" });
  await expect(page.locator(".connection")).toHaveText("Connecting");
  await expect(page.getByRole("button", { name: "Undo last" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Redo next" })).toBeDisabled();
  releaseInfo();
  await expect(page.locator(".connection")).toHaveText("Live");
  await expect(page.getByRole("button", { name: "Undo last" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Redo next" })).toBeDisabled();
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 1 } },
  ]);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 2 } },
  ]);
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 2',
  );
  await page.getByRole("button", { name: "History", exact: true }).click();
  const entries = page
    .getByRole("complementary", { name: "History entries" })
    .locator(".list-item");
  await expect(entries).toHaveCount(2);
  await entries.last().click();
  await expect(
    page.locator('[data-view="History"] > .dictionary'),
  ).toContainText('"after": 1');
  await page.getByRole("button", { name: "Undo last" }).click();
  await page.getByRole("button", { name: "State", exact: true }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 1',
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByRole("button", { name: "Preview captured state" }).click();
  await expect(page.locator(".preview-banner")).toBeVisible();
  await page.getByText("Run a query", { exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Run query", exact: true }),
  ).toBeDisabled();
});

test("draft modal traps focus, preserves failed edits and applies once", async ({
  page,
  backend,
}) => {
  await page.setViewportSize({ width: 390, height: 640 });
  await page.goto(backend.url);
  const opener = page.getByRole("button", { name: "Edit as new mutation" });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Mutation draft" });
  await expect(dialog).toBeVisible();
  const input = dialog.getByLabel("Actions", { exact: true });
  await input.fill(
    '[{"type":"set","schema":"BrowserModel","values":{"unknown":1}}]',
  );
  await dialog.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(input).toContainText('"unknown"');
  await page.mouse.click(1, 1);
  await expect(dialog).toBeVisible();
  for (let index = 0; index < 8; index++) {
    await page.keyboard.press("Tab");
    expect(
      await dialog.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
  }
  await input.fill(
    '[{"type":"set","schema":"BrowserModel","values":{"x":51}}]',
  );
  let commands = 0;
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/commands", async (route) => {
    commands += 1;
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await input.press("Control+Enter");
  await expect(
    dialog.getByRole("button", { name: "Apply new mutation" }),
  ).toBeDisabled();
  await input.press("Control+Enter");
  expect(commands).toBe(1);
  release();
  await expect(
    dialog.getByText("Response received.", { exact: false }),
  ).toBeVisible();
  const bounds = await dialog.boundingBox();
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(640);
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(opener).toBeFocused();
});

test("schema, history and Activity lists scroll without moving their details or page", async ({
  page,
  backend,
}) => {
  await page.setViewportSize({ width: 1100, height: 700 });
  await page.goto(backend.url);
  const schemas = page.getByRole("complementary", {
    name: "Schemas",
    exact: true,
  });
  await schemas
    .getByRole("button", { name: "BrowserExtra34", exact: false })
    .click();
  await expect(
    page.getByRole("heading", { name: "BrowserExtra34" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Edit as new mutation" }),
  ).toBeVisible();
  expect(
    await schemas.evaluate((element) => element.scrollTop),
  ).toBeGreaterThan(0);
  expect(await page.evaluate(() => scrollY)).toBe(0);
  for (let index = 1; index <= 25; index++) {
    await backend.mutate([
      { type: "set", schema: "BrowserModel", values: { x: index } },
    ]);
  }
  for (const [tab, listName] of [
    ["History", "History entries"],
    ["Activity", "Activity records"],
  ]) {
    await page.getByRole("button", { name: tab, exact: true }).click();
    const list = page.getByRole("complementary", { name: listName });
    await list.locator(".list-item").last().click();
    expect(
      await list.evaluate((element) => element.scrollTop),
    ).toBeGreaterThan(0);
    expect(
      await page.locator(".details").evaluate((element) => element.scrollTop),
    ).toBe(0);
    expect(await page.evaluate(() => scrollY)).toBe(0);
    await expect(
      page.getByRole("button", { name: "Undo last" }),
    ).toBeVisible();
  }
});

test("closing a delayed preview prevents it from replacing live State", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 4 } },
  ]);
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 4',
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/api/preview?*",
    async (route) => {
      const response = await route.fetch();
      await gate;
      await route.fulfill({ response });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "Preview captured state" }).click();
  await expect(page.locator(".preview-banner")).toContainText("Loading");
  await page.getByRole("button", { name: "Return to live" }).click();
  const response = page.waitForResponse((item) =>
    item.url().includes("/api/preview?"),
  );
  release();
  await response;
  await page.getByRole("button", { name: "State", exact: true }).click();
  await expect(page.locator(".preview-banner")).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "Edit as new mutation" }),
  ).toBeEnabled();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 4',
  );
});

test("a newer preview wins over an earlier delayed response", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 5 } },
  ]);
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 5',
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/api/preview?*",
    async (route) => {
      const response = await route.fetch();
      await gate;
      await route.fulfill({ response });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "Preview captured state" }).click();
  await expect(page.locator(".preview-banner")).toContainText("Loading");
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 6 } },
  ]);
  const records = page.getByRole("complementary", {
    name: "Activity records",
  });
  await expect(records.locator(".list-item").first()).toContainText(
    "response",
  );
  await records.locator(".list-item").first().click();
  await page.getByRole("button", { name: "Preview captured state" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 6',
  );
  const response = page.waitForResponse((item) =>
    item.url().includes("/api/preview?"),
  );
  release();
  await response;
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 6',
  );
});

test("shared recording controls synchronize tabs and partial identity filters", async ({
  page,
  backend,
  context,
}) => {
  const peer = await context.newPage();
  await page.goto(backend.url);
  await peer.goto(backend.url);
  for (const tab of [page, peer]) {
    await tab.getByRole("button", { name: "Activity", exact: true }).click();
  }
  await page.getByRole("button", { name: "Pause display" }).click();
  await page.getByRole("button", { name: "Pause recording" }).click();
  await expect(
    peer.getByRole("button", { name: "Resume recording" }),
  ).toBeVisible();
  await expect(
    peer.getByRole("button", { name: "Pause display" }),
  ).toBeVisible();
  const resumed = peer.waitForRequest((request) =>
    request.url().endsWith("/api/capture"),
  );
  await peer.getByRole("button", { name: "Resume recording" }).click();
  const browserId = (await resumed).headers()["x-devtools-session"];
  await expect(
    page.getByRole("button", { name: "Pause recording" }),
  ).toBeVisible();
  await peer.getByLabel("Client", { exact: true }).fill(browserId.slice(0, 8));
  await peer
    .getByRole("button", { name: "Use filters for recording" })
    .click();
  await expect(page.locator("footer")).toContainText("filtered");
  await peer.getByRole("button", { name: "State", exact: true }).click();
  await peer.getByRole("button", { name: "Edit as new mutation" }).click();
  await peer
    .getByLabel("Actions", { exact: true })
    .fill('[{"type":"set","schema":"BrowserModel","values":{"x":71}}]');
  await peer.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(peer.getByRole("dialog")).toContainText("Response received");
  await peer.getByRole("button", { name: "Close draft" }).click();
  await peer.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(
    peer
      .getByRole("complementary", { name: "Activity records" })
      .locator(".list-item")
      .first(),
  ).toContainText("response");
  await backend.disconnectBrowsers();
  await expect(page.locator(".connection")).toContainText("Disconnected");
  await expect(page.locator(".connection")).toHaveText("Live");
  await expect(page.locator("footer")).toContainText("filtered");
});

test("reserved tag row IDs and tagged field values survive snapshot, live edit and preview", async ({
  page,
  backend,
}) => {
  await backend.mutate([
    {
      type: "insert",
      schema: "BrowserRows",
      row_id: "$integer",
      values: {
        number: { $integer: "9007199254740993" },
        payload: { $object: [["$integer", "literal"]] },
      },
    },
  ]);
  await page.goto(backend.url);
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  await expect(page.getByLabel("Row ID")).toHaveValue("$integer");
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"$integer": "9007199254740993"',
  );
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"literal"',
  );
  await backend.mutate([
    {
      type: "setrow",
      schema: "BrowserRows",
      row_id: "$integer",
      values: { title: "updated" },
    },
  ]);
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"updated"',
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByLabel("Category").selectOption("commit");
  await expect(
    page.locator('[data-view="Activity"] > .dictionary'),
  ).toContainText('"updated"');
  await page.getByRole("button", { name: "Preview captured state" }).click();
  await expect(page.getByLabel("Row ID")).toHaveValue("$integer");
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"$integer": "9007199254740993"',
  );
});

test.describe("disabled history", () => {
  test.use({ historyEnabled: false });
  test("explains configuration without suggesting capture enables retention", async ({
    page,
    backend,
  }) => {
    await page.goto(backend.url);
    await page.getByRole("button", { name: "History", exact: true }).click();
    await expect(page.locator(".history-empty")).toContainText(
      "History is disabled for this Store",
    );
    await expect(page.locator(".history-empty")).toContainText(
      "HistoryConfig()",
    );
    await expect(page.locator(".history-empty")).toContainText(
      "track_history = True",
    );
    await expect(
      page.getByRole("button", { name: "Undo last" }),
    ).toBeDisabled();
    await backend.mutate([
      { type: "set", schema: "BrowserModel", values: { x: 2 } },
    ]);
    await expect(
      page
        .getByRole("complementary", { name: "History entries" })
        .locator(".list-item"),
    ).toHaveCount(0);
  });
});

test("oversized mixed-command response stays unresolved after commit and retries the same transaction", async ({
  page,
  backend,
}) => {
  for (let index = 0; index < 20; index++) {
    await backend.mutate([
      {
        type: "insert",
        schema: "BrowserRows",
        row_id: `large-${index}`,
        values: { title: "x".repeat(60000) },
      },
    ]);
  }
  await page.goto(backend.url);
  await page.getByRole("button", { name: "Edit as new mutation" }).click();
  await page
    .getByLabel("Actions", { exact: true })
    .fill('[{"type":"set","schema":"BrowserModel","values":{"x":83}}]');
  let identity = "";
  await page.route(
    "**/api/commands",
    async (route) => {
      const batch = route.request().postDataJSON() as {
        requests: Record<string, unknown>[];
      };
      identity = String(batch.requests[0].transaction_id);
      batch.requests.push({
        type: "query",
        query: {
          version: 1,
          source: { schema: "BrowserRows" },
          joins: [],
          predicate: null,
        },
      });
      const response = await route.fetch({ postData: JSON.stringify(batch) });
      await route.fulfill({ response });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 83',
  );
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Unresolved command");
  await expect(
    dialog.getByRole("button", { name: "Apply new mutation" }),
  ).toBeDisabled();
  const retry = page.waitForRequest((request) =>
    request.url().endsWith("/api/commands"),
  );
  await dialog.getByRole("button", { name: "Retry same command ID" }).click();
  expect((await retry).postDataJSON().requests[0].transaction_id).toBe(
    identity,
  );
  await expect(dialog).not.toContainText("Unresolved command");
  await page.getByRole("button", { name: "Close draft" }).click();
  await page.getByRole("button", { name: "Undo last" }).click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 0',
  );
});

test("a Store restart invalidates an outstanding preview response", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 9 } },
  ]);
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 9',
  );
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/api/preview?*",
    async (route) => {
      const response = await route.fetch();
      await gate;
      await route.fulfill({ response });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "Preview captured state" }).click();
  await expect(page.locator(".preview-banner")).toContainText("Loading");
  await backend.restart();
  await expect(
    page.getByText("Store restarted.", { exact: false }).first(),
  ).toBeVisible();
  await expect(page.locator(".connection")).toHaveText("Live");
  const response = page.waitForResponse((item) =>
    item.url().includes("/api/preview?"),
  );
  release();
  await response;
  await page.getByRole("button", { name: "State", exact: true }).click();
  await expect(page.locator(".preview-banner")).not.toBeVisible();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 0',
  );
  await expect(
    page.getByRole("button", { name: "Edit as new mutation" }),
  ).toBeEnabled();
});

test("schema selection loads live rows and signals without recording controls", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByRole("button", { name: "Pause recording" }).click();
  await page.getByRole("button", { name: "State", exact: true }).click();
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  await expect(page.getByLabel("Row ID")).toHaveValue("");
  await expect(page.getByRole("button", { name: "Insert row" })).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Edit as new mutation" }),
  ).toBeDisabled();
  await page.getByText("Declared signals", { exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Draft signal" }),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: /Capture/ })).toHaveCount(0);
  await backend.mutate(
    ["first", "second"].map((row_id) => ({
      type: "insert",
      schema: "BrowserRows",
      row_id,
      values: { title: row_id },
    })),
  );
  await page.getByLabel("Row ID").selectOption("second");
  await page.getByRole("button", { name: "Draft signal" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Actions", { exact: true })).toHaveValue(
    /"row_id": "second"/,
  );
  await dialog.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(dialog).toContainText("Response received.");
  await dialog.getByRole("button", { name: "Close draft" }).click();
  await page
    .getByRole("button", { name: "BrowserExtra02", exact: false })
    .click();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"value": 2',
  );
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  await expect(page.getByLabel("Row ID").locator("option")).toHaveText([
    "first",
    "second",
  ]);
  await expect(page.locator("footer")).toContainText(
    "Activity recording paused",
  );
});

test("rapid schema selections wait for the latest snapshot while the session stays live", async ({
  page,
  backend,
}) => {
  let releaseRows: () => void = () => undefined;
  let heldResolve: () => void = () => undefined;
  const held = new Promise<void>((resolve) => {
    heldResolve = resolve;
  });
  const sessions = new Set<string>();
  const scopes: string[][] = [];
  await page.routeWebSocket("**/stream?*", (socket) => {
    const server = socket.connectToServer();
    socket.onMessage((message) => {
      const control = JSON.parse(String(message));
      scopes.push(control.schemas);
      server.send(message);
    });
    server.onMessage((message) => {
      const envelope = JSON.parse(String(message));
      if (envelope.kind === "snapshot") {
        sessions.add(envelope.payload.session_id);
        if (
          Object.hasOwn(envelope.payload.checkpoint.schemas, "BrowserRows")
        ) {
          releaseRows = () => socket.send(message);
          heldResolve();
          return;
        }
      }
      socket.send(message);
    });
  });
  await page.goto(backend.url);
  await expect(page.locator(".connection")).toHaveText("Live");
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  await held;
  await expect(page.locator(".schema-status")).toHaveText(
    "Loading BrowserRows…",
  );
  await expect(page.locator(".connection")).toHaveText("Live");
  await page
    .getByRole("button", { name: "BrowserExtra01", exact: false })
    .click();
  await page
    .getByRole("button", { name: "BrowserModel", exact: false })
    .click();
  await expect(page.locator(".schema-status")).toHaveText(
    "Loading BrowserModel…",
  );
  await expect(
    page.getByRole("button", { name: "Edit as new mutation" }),
  ).toBeDisabled();
  expect(scopes).toEqual([["BrowserRows"]]);
  releaseRows();
  await expect(
    page.getByRole("button", { name: "Edit as new mutation" }),
  ).toBeEnabled();
  await expect(page.locator(".state-data > .dictionary")).toContainText(
    '"x": 0',
  );
  expect(scopes).toEqual([["BrowserRows"], ["BrowserModel"]]);
  expect(sessions.size).toBe(1);
});

test("received requests display retained success and failure responses despite list filters", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 96 } },
  ]);
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByLabel("Category").selectOption("request");
  const list = page.getByRole("complementary", { name: "Activity records" });
  await expect(list.locator(".list-item").first()).toContainText("Received");
  const related = page.getByRole("region", { name: "Related response" });
  await expect(related).toContainText("Response produced · ok");
  await related.getByText(/Response #\d+ details/).click();
  await expect(related.locator(".inspector")).toContainText(
    '"type": "transaction"',
  );
  await expect(
    backend.mutate([
      { type: "set", schema: "BrowserModel", values: { missing: 1 } },
    ]),
  ).rejects.toThrow();
  await expect(related).toContainText("Response produced · failed");
  await related.getByText(/Response #\d+ details/).click();
  await expect(related.locator(".inspector")).toContainText(
    "transaction_failure",
  );
  await expect(
    list.locator(".badge").filter({ hasText: "pending" }),
  ).toHaveCount(0);
});

test("absent responses stay explicitly unavailable through paused display and recording filters", async ({
  page,
  backend,
}) => {
  let releaseResponse: () => void = () => undefined;
  let holdResponse = true;
  const heldMessages: (string | Buffer)[] = [];
  await page.routeWebSocket("**/stream?*", (socket) => {
    const server = socket.connectToServer();
    server.onMessage((message) => {
      const envelope = JSON.parse(String(message));
      if (
        holdResponse &&
        (heldMessages.length > 0 ||
          (envelope.kind === "activity" &&
            envelope.payload.category === "response"))
      ) {
        heldMessages.push(message);
        releaseResponse = () => {
          heldMessages.splice(0).forEach((item) => socket.send(item));
        };
      } else socket.send(message);
    });
  });
  await page.goto(backend.url);
  await expect(page.locator(".connection")).toHaveText("Live");
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 97 } },
  ]);
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByLabel("Category").selectOption("request");
  const related = page.getByRole("region", { name: "Related response" });
  await expect(related).toContainText("Response unavailable");
  await page.getByRole("button", { name: "Pause display" }).click();
  holdResponse = false;
  releaseResponse();
  await expect(related).toContainText("Resume display");
  await page.getByRole("button", { name: "Resume display" }).click();
  await expect(related).toContainText("Response produced · ok");
  await page
    .getByRole("button", { name: "Use filters for recording" })
    .click();
  await backend.mutate([
    { type: "set", schema: "BrowserModel", values: { x: 98 } },
  ]);
  await expect(related).toContainText("Response unavailable");
  await expect(related).toContainText("recording may have excluded it");
  await expect(page.locator("footer")).toContainText("filtered");
});

test("multi-schema drafts load dependencies but refresh guards only on explicit choice", async ({
  page,
  backend,
}) => {
  let releaseScope: () => void = () => undefined;
  let heldResolve: () => void = () => undefined;
  const held = new Promise<void>((resolve) => {
    heldResolve = resolve;
  });
  let holdScope = true;
  await page.routeWebSocket("**/stream?*", (socket) => {
    const server = socket.connectToServer();
    server.onMessage((message) => {
      const envelope = JSON.parse(String(message));
      if (
        holdScope &&
        envelope.kind === "snapshot" &&
        Object.hasOwn(envelope.payload.checkpoint.schemas, "BrowserExtra03")
      ) {
        releaseScope = () => socket.send(message);
        heldResolve();
      } else socket.send(message);
    });
  });
  await page.goto(backend.url);
  await page.getByRole("button", { name: "Edit as new mutation" }).click();
  const dialog = page.getByRole("dialog");
  const input = dialog.getByLabel("Actions", { exact: true });
  const actions = JSON.stringify([
    { type: "set", schema: "BrowserModel", values: { x: 99 } },
    { type: "set", schema: "BrowserExtra03", values: { value: 42 } },
  ]);
  await input.fill(actions);
  await held;
  await expect(
    dialog.getByRole("button", { name: "Apply new mutation" }),
  ).toBeDisabled();
  await expect(page.locator(".connection")).toHaveText("Live");
  holdScope = false;
  releaseScope();
  await expect(dialog).toContainText("Additional schema values loaded");
  await dialog.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Use current revisions after loading: BrowserExtra03",
  );
  await dialog.getByRole("button", { name: "Use current revisions" }).click();
  await dialog.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(dialog).toContainText("Response received.");
  await dialog.getByRole("button", { name: "Close draft" }).click();
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByLabel("Category").selectOption("request");
  await page.getByRole("button", { name: "Copy as new mutation" }).click();
  await expect(input).toHaveValue(
    JSON.stringify(JSON.parse(actions), null, 2),
  );
  await expect(dialog).toContainText("Additional schema values loaded");
  await input.fill('[{"type":"set","schema":"UnknownSchema","values":{}}]');
  await dialog.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(dialog.getByRole("alert")).toContainText("UnknownSchema");
  await expect(page.locator(".connection")).toHaveText("Live");
  await input.fill("[");
  await dialog.getByRole("button", { name: "Apply new mutation" }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(page.locator(".connection")).toHaveText("Live");
});

test("selecting another live schema cancels delayed preview and keeps retained Activity", async ({
  page,
  backend,
}) => {
  await page.goto(backend.url);
  await expect(page.locator(".connection")).toHaveText("Live");
  for (let x = 1; x <= 10; x++) {
    await backend.mutate([
      { type: "set", schema: "BrowserModel", values: { x } },
    ]);
  }
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByLabel("Category").selectOption("commit");
  const records = page
    .getByRole("complementary", { name: "Activity records" })
    .locator(".list-item");
  await expect(records).toHaveCount(10);
  let releasePreview: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    releasePreview = resolve;
  });
  await page.route(
    "**/api/preview?*",
    async (route) => {
      const response = await route.fetch();
      await gate;
      await route.fulfill({ response });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: "Preview captured state" }).click();
  await expect(page.locator(".preview-banner")).toContainText("Loading");
  await page.getByRole("button", { name: "State", exact: true }).click();
  await page
    .getByRole("button", { name: "BrowserRows", exact: false })
    .click();
  await expect(page.getByRole("button", { name: "Insert row" })).toBeEnabled();
  const response = page.waitForResponse((item) =>
    item.url().includes("/api/preview?"),
  );
  releasePreview();
  await response;
  await expect(page.locator(".preview-banner")).not.toBeVisible();
  await expect(
    page.getByRole("heading", { name: "BrowserRows" }),
  ).toBeVisible();
  await expect(page.getByLabel("Row ID")).toHaveValue("");
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(records).toHaveCount(10);
});
