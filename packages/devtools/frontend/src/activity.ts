import { recordMatchesTarget } from "./display";
import { matches } from "./model";
import type { Activity, Filters } from "./types";

/** Keep the visible list and live request rate on the same filter rules. */
export function matchesActivity(record: Activity, filters: Filters): boolean {
  return (
    matches(record, filters) &&
    recordMatchesTarget(
      record,
      filters.row ?? "",
      filters.field ?? "",
      filters.schema,
    )
  );
}

/** Count live request records independently of retained display history. */
export class RequestRate {
  private samples = new Map<string, { time: number; record: Activity }>();

  observe(record: Activity, now = performance.now()): void {
    this.expire(now);
    if (record.category === "request" && !this.samples.has(record.id)) {
      this.samples.set(record.id, { time: now, record });
    }
  }

  count(filters: Filters, now = performance.now()): number {
    this.expire(now);
    let count = 0;
    for (const { record } of this.samples.values()) {
      if (matchesActivity(record, filters)) count++;
    }
    return count;
  }

  reset(): void {
    this.samples.clear();
  }

  private expire(now: number): void {
    for (const [id, sample] of this.samples) {
      if (sample.time > now - 1_000) break;
      this.samples.delete(id);
    }
  }
}
