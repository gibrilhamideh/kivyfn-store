import { describe, expect, it } from "vitest";
import { matchesActivity, RequestRate } from "./activity";
import type { Activity, Filters } from "./types";

const filters: Filters = {
  client_id: "",
  schema: "",
  category: "",
  status: "",
  identity: "",
};
const request: Activity = {
  id: "one",
  sequence: 1,
  time: 9_000_000,
  category: "request",
  status: "received",
  client_id: "automation",
  capture_id: "capture-one",
  transaction_id: "transaction-one",
  schemas: ["Rows", "Model"],
  detail: {
    request: {
      requests: [
        {
          type: "transaction",
          actions: [
            {
              type: "setrow",
              schema: "Rows",
              row_id: "fan-1",
              values: { speed: 20 },
            },
            { type: "set", schema: "Model", values: { active: true } },
          ],
        },
      ],
    },
  },
};

describe("live Activity request rate", () => {
  it("counts a request batch once, excluding responses and commits", () => {
    const rate = new RequestRate();
    rate.observe(request, 0);
    rate.observe(request, 10);
    for (const category of ["response", "commit", "lifecycle", "gap"]) {
      rate.observe({ ...request, id: category, category }, 20);
    }
    expect(rate.count(filters, 30)).toBe(1);
  });

  it("uses the last second of local arrival time and decays without traffic", () => {
    const rate = new RequestRate();
    rate.observe(request, 100);
    rate.observe({ ...request, id: "two", time: 1 }, 500);
    expect(rate.count(filters, 1_099)).toBe(2);
    expect(rate.count(filters, 1_100)).toBe(1);
    expect(rate.count(filters, 1_500)).toBe(0);
  });

  it("applies every display filter to recent traffic immediately", () => {
    const rate = new RequestRate();
    rate.observe(request, 0);
    const matching: Filters = {
      client_id: "auto",
      schema: "Rows",
      category: "request",
      status: "received",
      identity: "capture-one",
      row: "fan-1",
      field: "speed",
    };
    expect(matchesActivity(request, matching)).toBe(true);
    expect(rate.count(matching, 10)).toBe(1);
    for (const key of Object.keys(matching)) {
      const excluded = { ...matching, [key]: "unmatched" };
      expect(matchesActivity(request, excluded)).toBe(false);
      expect(rate.count(excluded, 10)).toBe(0);
    }
    expect(rate.count({ ...matching, schema: "Model" }, 10)).toBe(0);
    expect(rate.count({ ...filters, category: "response" }, 10)).toBe(0);
    expect(rate.count(filters, 10)).toBe(1);
  });

  it("does not cap rates at the Activity list retention limit", () => {
    const rate = new RequestRate();
    for (let i = 0; i < 2_000; i++) {
      rate.observe({ ...request, id: String(i) }, i / 10);
    }
    expect(rate.count(filters, 500)).toBe(2_000);
    expect(rate.count(filters, 1_100)).toBe(999);
    expect(rate.count(filters, 1_200)).toBe(0);
  });

  it("clears traffic across connection and recording boundaries", () => {
    const rate = new RequestRate();
    rate.observe(request, 0);
    rate.reset();
    expect(rate.count(filters, 1)).toBe(0);
    rate.observe(request, 2);
    expect(rate.count(filters, 3)).toBe(1);
  });

  it("counts truncated requests only where their retained metadata matches", () => {
    const rate = new RequestRate();
    rate.observe({ ...request, truncated: true, detail: {} }, 0);
    expect(rate.count({ ...filters, schema: "Rows" }, 1)).toBe(1);
    expect(rate.count({ ...filters, field: "speed" }, 1)).toBe(0);
  });
});
