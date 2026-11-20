import { describe, expect, it } from "vitest";
import { ApiError, admin, data, dataKey } from "@/api/client";
import { mockFetch } from "@/test/setup";

describe("error envelope", () => {
  it("keeps the code, the retryable flag, the details and the request id", async () => {
    mockFetch([
      {
        status: 409,
        body: {
          error: {
            code: "write_conflict",
            message: "Another transaction changed this record first. Recompute and retry.",
            retryable: true,
            details: { table: "Doc", record_id: 1 },
            request_id: "abc123",
          },
        },
      },
    ]);

    const error = await data.query("docs", "MATCH (d) RETURN d").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    const api = error as ApiError;
    expect(api.status).toBe(409);
    expect(api.code).toBe("write_conflict");
    expect(api.retryable).toBe(true);
    expect(api.details).toEqual({ table: "Doc", record_id: 1 });
    expect(api.requestId).toBe("abc123");
    expect(api.summary).toContain("write_conflict (409)");
    expect(api.message).toMatch(/Recompute and retry/);
  });

  it("turns an absent envelope into something still reportable", async () => {
    mockFetch([{ status: 502 }]);
    const error = (await admin.databases().catch((caught: unknown) => caught)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("unknown_error");
    expect(error.retryable).toBe(false);
  });

  it("marks an unauthenticated door so the shell can redirect", async () => {
    mockFetch([{ status: 401, body: { error: { code: "admin_session_required", message: "no session", retryable: false, details: {} } } }]);
    const error = (await admin.session().catch((caught: unknown) => caught)) as ApiError;
    expect(error.status).toBe(401);
    expect(error.code).toBe("admin_session_required");
  });

  it("passes a schema refusal's field through to the form", async () => {
    mockFetch([
      {
        status: 422,
        body: {
          error: {
            code: "invalid_request",
            message: "The request body did not match the documented schema.",
            retryable: false,
            details: { problems: [{ field: "limit", message: "Input should be greater than or equal to 0" }] },
            request_id: "z",
            field: "limit",
          },
        },
      },
    ]);
    const error = (await data.query("docs", "MATCH (n) RETURN n", { limit: -1 }).catch(
      (caught: unknown) => caught,
    )) as ApiError;
    expect(error.field).toBe("limit");
    expect(error.details.problems).toEqual([{ field: "limit", message: "Input should be greater than or equal to 0" }]);
  });
});

describe("the two doors", () => {
  it("sends the admin session with credentials and no bearer header", async () => {
    const { calls } = mockFetch([{ status: 200, body: { databases: [] } }]);
    await admin.databases();
    expect(calls[0].url).toBe("/admin/api/databases");
    expect(calls[0].init?.credentials).toBe("include");
    expect((calls[0].init?.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("sends a bearer key to the data plane and stops sending cookies", async () => {
    dataKey.set("okg_test_key");
    const { calls } = mockFetch([{ status: 200, body: { databases: [] } }]);
    await data.databases();
    expect(calls[0].url).toBe("/api/v1/databases");
    expect((calls[0].init?.headers as Record<string, string>).Authorization).toBe("Bearer okg_test_key");
    expect(calls[0].init?.credentials).toBe("same-origin");
  });

  it("falls back to the admin session when no key is stored, and says so", async () => {
    const { calls } = mockFetch([{ status: 200, body: { databases: [] } }]);
    await data.databases();
    expect(data.usingKey()).toBe(false);
    expect(calls[0].init?.credentials).toBe("include");
  });

  it("omits empty query parameters instead of sending blanks", async () => {
    const { calls } = mockFetch([{ status: 200, body: { entries: [] } }]);
    await admin.audit({ actor: "key:abc", database: "", action: "query" });
    expect(calls[0].url).toContain("actor=key%3Aabc");
    expect(calls[0].url).toContain("action=query");
    expect(calls[0].url).not.toContain("database=");
  });

  it("posts a JSON body for a statement", async () => {
    const { calls } = mockFetch([{ status: 200, body: { columns: [], rows: [] } }]);
    await data.query("docs", "MATCH (d:Doc) RETURN d", { limit: 5 });
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      query: "MATCH (d:Doc) RETURN d",
      parameters: undefined,
      limit: 5,
    });
  });
});
