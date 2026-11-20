import { useMemo } from "react";
import type { DatabaseOption } from "@/api/client";
import { Field } from "@/components/ui";

export const GROUP_TITLES: Record<string, string> = {
  storage: "Storage",
  memory: "Memory",
  wal: "Write-ahead log",
  budgets: "Budgets",
  vector: "Vector search",
  metrics: "Metrics",
  query: "Query",
  other: "Other",
};

export type OptionValues = Record<string, string | number | boolean | null>;

/** Client-side check of the same bounds the server will check, so a typo is caught before a POST. */
export function validateOptions(
  options: DatabaseOption[],
  values: OptionValues,
): Record<string, string> {
  const problems: Record<string, string> = {};
  for (const option of options) {
    const value = values[option.name];
    if (value === undefined || value === null || value === "") continue;
    if (option.type === "integer") {
      const number = Number(value);
      if (!Number.isInteger(number)) problems[option.name] = "must be a whole number";
      else if (option.minimum !== undefined && number < option.minimum)
        problems[option.name] = `must be at least ${option.minimum}`;
      else if (option.maximum !== undefined && number > option.maximum)
        problems[option.name] = `must be at most ${option.maximum}`;
    }
    if (option.type === "enum" && option.choices?.length && !option.choices.includes(String(value)))
      problems[option.name] = `must be one of: ${option.choices.join(", ")}`;
  }
  return problems;
}

export function toPayload(values: OptionValues): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(values)) {
    // An untouched option is the store's decision, not ours.
    if (value === "" || value === undefined || value === null) continue;
    payload[name] = value;
  }
  return payload;
}

/**
 * The form is generated from the daemon's description of `DatabaseConfig`, so a library release that
 * adds an option appears here without a frontend change. Options left untouched are not sent: the
 * store keeps its own default rather than being pinned to whatever this console shipped with.
 */
export function ConfigForm({
  options,
  values,
  problems,
  onChange,
}: {
  options: DatabaseOption[];
  values: OptionValues;
  problems: Record<string, string>;
  onChange: (name: string, value: string | number | boolean | null) => void;
}) {
  const groups = useMemo(() => {
    const byGroup = new Map<string, DatabaseOption[]>();
    for (const option of options) {
      byGroup.set(option.group, [...(byGroup.get(option.group) ?? []), option]);
    }
    return [...byGroup.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [options]);

  return (
    <div className="grid gap-4">
      {groups.map(([group, items]) => (
        <fieldset className="rounded-md border border-line p-3" key={group}>
          <legend className="px-1 text-[11px] font-semibold uppercase tracking-wide text-accent">
            {GROUP_TITLES[group] ?? group}
          </legend>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((option) => {
              const value = values[option.name];
              const problem = problems[option.name];
              const hint = [
                option.description,
                option.minimum !== undefined || option.maximum !== undefined
                  ? `range ${option.minimum ?? "−∞"}…${option.maximum ?? "∞"}`
                  : null,
                option.nullable ? "nullable" : null,
                `default: ${JSON.stringify(option.default)}`,
              ]
                .filter(Boolean)
                .join(" · ");
              return (
                <Field key={option.name} hint={hint} label={option.label ?? option.name}>
                  {option.type === "boolean" ? (
                    <select
                      className="input"
                      onChange={(event) =>
                        onChange(option.name, event.target.value === "unset" ? null : event.target.value === "true")
                      }
                      value={value === undefined || value === null ? "unset" : String(Boolean(value))}
                    >
                      <option value="unset">— store default —</option>
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  ) : option.type === "enum" && option.choices?.length ? (
                    <select
                      className="input"
                      onChange={(event) => onChange(option.name, event.target.value || null)}
                      value={typeof value === "string" ? value : ""}
                    >
                      <option value="">— store default —</option>
                      {option.choices.map((choice) => (
                        <option key={choice} value={choice}>
                          {choice}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      className="input"
                      inputMode={option.type === "integer" ? "numeric" : undefined}
                      onChange={(event) => {
                        const raw = event.target.value;
                        if (raw === "") return onChange(option.name, null);
                        onChange(option.name, option.type === "integer" ? Number(raw) : raw);
                      }}
                      placeholder={`default ${JSON.stringify(option.default)}`}
                      type={option.type === "integer" ? "number" : "text"}
                      value={value === undefined || value === null ? "" : String(value)}
                    />
                  )}
                  {problem ? <span className="mt-1 block text-[11px] text-danger">{problem}</span> : null}
                </Field>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
