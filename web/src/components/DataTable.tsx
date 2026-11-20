import { useMemo, useState } from "react";
import type { Cell } from "@/api/client";
import { formatCell } from "@/lib/format";
import { Empty } from "@/components/ui";

/**
 * Results are shown with the column names the engine returned and the value tags it sent. Row numbers
 * are the console's own; a Grafx row has no ordinal identity, and pretending otherwise would invite
 * the reader to treat a position as a key.
 */
export function DataTable({
  columns,
  rows,
  onCellClick,
}: {
  columns: string[];
  rows: Cell[][];
  onCellClick?: (value: Cell, column: string, row: number) => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const headers = useMemo(() => (columns.length ? columns : rows[0]?.map((_, index) => `column ${index}`) ?? []), [columns, rows]);
  if (!rows.length) return <Empty>The statement returned no rows.</Empty>;
  return (
    <div className="scroll-thin overflow-auto rounded-md border border-line" style={{ maxHeight: "60vh" }}>
      <table className="w-full border-collapse">
        <thead>
          <tr>
            <th className="th w-10">#</th>
            {headers.map((header, index) => (
              <th className="th" key={`${header}-${index}`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr
              className="hover:bg-raised"
              key={rowIndex}
              onClick={() => setExpanded(expanded === rowIndex ? null : rowIndex)}
            >
              <td className="td text-muted">{rowIndex + 1}</td>
              {headers.map((header, columnIndex) => {
                const value = row[columnIndex];
                const text = formatCell(value);
                const truncatedCell = text.length > 160 && expanded !== rowIndex;
                return (
                  <td className="td" key={`${rowIndex}-${header}-${columnIndex}`}>
                    <span
                      className={typeof value === "object" && value !== null ? "text-azure" : undefined}
                      title={typeof value === "object" && value !== null ? JSON.stringify(value) : text}
                    >
                      {onCellClick ? (
                        <button
                          className="text-left hover:underline"
                          onClick={(event) => {
                            event.stopPropagation();
                            onCellClick(value, header, rowIndex);
                          }}
                          type="button"
                        >
                          {truncatedCell ? `${text.slice(0, 160)}…` : text}
                        </button>
                      ) : truncatedCell ? (
                        `${text.slice(0, 160)}…`
                      ) : (
                        text
                      )}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The plan tree, as an indented operator list rather than a wall of JSON. */
export function PlanTree({ plan, depth = 0 }: { plan: Record<string, unknown> | null; depth?: number }) {
  if (!plan) return <Empty>No plan was returned. Run EXPLAIN to get one.</Empty>;
  const children = (plan.children as Record<string, unknown>[] | undefined) ?? [];
  return (
    <div className="font-mono text-[12px] leading-relaxed">
      <div style={{ paddingLeft: depth * 14 }}>
        <span className="text-azure">{String(plan.operator ?? "?")}</span>
        {plan.detail ? <span className="text-muted"> {String(plan.detail)}</span> : null}
        {typeof plan.estimated_rows === "number" ? (
          <span className="text-muted"> ~{plan.estimated_rows} rows</span>
        ) : null}
      </div>
      {children.map((child, index) => (
        <PlanTree key={index} depth={depth + 1} plan={child} />
      ))}
    </div>
  );
}
