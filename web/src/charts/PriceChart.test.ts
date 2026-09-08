import { describe, expect, it } from "vitest";
import { toUtcLineData } from "./PriceChart";

describe("toUtcLineData", () => {
  it("preserves hourly UTC timestamps as sorted, strictly unique epoch seconds", () => {
    const data = toUtcLineData([
      { time: "2026-01-01T02:00:00Z", value: 102 },
      { time: "2026-01-01T00:00:00Z", value: 100 },
      { time: "2026-01-01T01:00:00Z", value: 101 },
      { time: "2026-01-01T01:00:00Z", value: 101.5 },
      { time: "not-a-timestamp", value: 999 },
      { time: "2026-01-01T03:00:00Z", value: Number.NaN },
    ]);

    expect(data).toEqual([
      { time: 1_767_225_600, value: 100 },
      { time: 1_767_229_200, value: 101.5 },
      { time: 1_767_232_800, value: 102 },
    ]);
  });
});
