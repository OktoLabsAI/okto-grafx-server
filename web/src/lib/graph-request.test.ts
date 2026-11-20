import { describe, expect, it } from "vitest";
import { projectionWeightColumns } from "@/lib/graph-request";

describe("projection weight columns", () => {
  it("weights the single relationship table that is selected", () => {
    expect(projectionWeightColumns(["CITES"], "weight")).toEqual({ CITES: "weight" });
  });

  it("sends nothing when no weight was asked for", () => {
    expect(projectionWeightColumns(["CITES"], "")).toBeUndefined();
    expect(projectionWeightColumns(["CITES"], "   ")).toBeUndefined();
  });

  it("sends nothing when more than one table is selected", () => {
    // A partial map is refused by the daemon: every selected relationship table needs its own weight,
    // or two kinds of edge would be drawn on two unstated scales.
    expect(projectionWeightColumns(["CITES", "TAGGED"], "weight")).toBeUndefined();
  });

  it("sends nothing when the one selection is empty", () => {
    expect(projectionWeightColumns([], "weight")).toBeUndefined();
  });
});
