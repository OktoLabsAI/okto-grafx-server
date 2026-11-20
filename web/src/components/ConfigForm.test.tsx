import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { DatabaseOption } from "@/api/client";
import { ApiError } from "@/api/client";
import { ConfigForm, toPayload, validateOptions } from "@/components/ConfigForm";
import { ErrorBanner } from "@/components/ui";

const OPTIONS: DatabaseOption[] = [
  {
    name: "page_size",
    type: "integer",
    default: 8192,
    nullable: false,
    group: "storage",
    label: "page size",
    description: "Page size in bytes; a power of two fixed when the store is created.",
    minimum: 4096,
    maximum: 32768,
  },
  {
    name: "buffer_budget_bytes",
    type: "integer",
    default: 67108864,
    nullable: false,
    group: "memory",
    label: "buffer budget",
    description: "Pages this handle may hold.",
    minimum: 1048576,
  },
  {
    name: "checksum",
    type: "enum",
    default: "crc32c",
    nullable: false,
    group: "storage",
    label: "checksum",
    description: "Per-page checksum algorithm.",
    choices: ["none", "crc32c", "xxh3"],
  },
  {
    name: "metrics",
    type: "boolean",
    default: false,
    nullable: false,
    group: "metrics",
    label: "metrics",
    description: "Publish per-handle counters.",
  },
];

describe("generated configuration form", () => {
  it("renders one control per option, grouped, with the default and the bounds shown", () => {
    render(
      <ConfigForm
        onChange={() => undefined}
        options={OPTIONS}
        problems={{}}
        values={{}}
      />,
    );
    expect(screen.getByText("Storage")).toBeInTheDocument();
    expect(screen.getByText("Memory")).toBeInTheDocument();
    expect(screen.getByText("Metrics")).toBeInTheDocument();
    expect(screen.getByLabelText(/page size/)).toHaveAttribute("type", "number");
    expect(screen.getByText(/range 4096…32768/)).toBeInTheDocument();
    expect(screen.getByText(/default: 8192/)).toBeInTheDocument();
    // An untouched boolean is not a decision: the store keeps its own default.
    expect(screen.getByLabelText(/metrics/)).toHaveValue("unset");
  });

  it("refuses out-of-range and unknown values before the request is made", () => {
    expect(validateOptions(OPTIONS, { page_size: 2048 })).toEqual({
      page_size: "must be at least 4096",
    });
    expect(validateOptions(OPTIONS, { page_size: 65536 })).toEqual({ page_size: "must be at most 32768" });
    expect(validateOptions(OPTIONS, { buffer_budget_bytes: 1.5 })).toEqual({
      buffer_budget_bytes: "must be a whole number",
    });
    expect(validateOptions(OPTIONS, { checksum: "md5" })).toEqual({
      checksum: "must be one of: none, crc32c, xxh3",
    });
    expect(validateOptions(OPTIONS, { page_size: 16384, checksum: "xxh3" })).toEqual({});
  });

  it("sends only what the operator actually changed", () => {
    const payload = toPayload({
      page_size: 16384,
      buffer_budget_bytes: null,
      checksum: "",
      metrics: false,
    });
    // An untouched option stays the store's decision.
    expect(payload).toEqual({ page_size: 16384, metrics: false });
  });

  it("lets the operator unset a value again", async () => {
    const onChange = vi.fn();
    render(<ConfigForm onChange={onChange} options={OPTIONS} problems={{}} values={{ page_size: 16384 }} />);
    const input = screen.getByLabelText(/page size/) as HTMLInputElement;
    expect(input.value).toBe("16384");
    await userEvent.clear(input);
    expect(onChange).toHaveBeenCalledWith("page_size", null);
  });

  it("shows a refusal from the daemon with the option it named", () => {
    render(
      <ErrorBanner
        error={
          new ApiError(
            400,
            {
              code: "configuration_error",
              message: "Invalid configuration for 'page_size': a power of two is required. Got 4097.",
              retryable: false,
              details: { field: "page_size", value: 4097 },
              field: "page_size",
              request_id: "r1",
            },
            "",
          )
        }
      />,
    );
    expect(screen.getByText("configuration_error")).toBeInTheDocument();
    expect(screen.getByText("field: page_size")).toBeInTheDocument();
    expect(screen.getByText(/power of two is required/)).toBeInTheDocument();
    expect(screen.getByText("request id r1")).toBeInTheDocument();
  });

  it("offers a retry only when the refusal says retrying can help", () => {
    const onRetry = vi.fn();
    const { rerender } = render(
      <ErrorBanner
        error={new ApiError(429, { code: "reader_lanes_busy", message: "busy", retryable: true, details: {} }, "")}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByText("retryable")).toBeInTheDocument();
    rerender(
      <ErrorBanner
        error={new ApiError(403, { code: "write_not_permitted", message: "read door", retryable: false, details: {} }, "")}
        onRetry={onRetry}
      />,
    );
    expect(screen.queryByText("retryable")).not.toBeInTheDocument();
  });
});
