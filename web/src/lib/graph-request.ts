/**
 * Build the projection request the daemon accepts.
 *
 * `weight_columns` names a numeric property for **every** selected relationship table: a partial map
 * is refused with `configuration_error`, because silently weighting one relationship type and not the
 * next would draw two kinds of edge on two different, unstated scales. So a weight is only sent while
 * exactly one relationship table is selected.
 */
export function projectionWeightColumns(
  relationshipTables: readonly string[],
  weightColumn: string,
): Record<string, string> | undefined {
  const column = weightColumn.trim();
  if (!column || relationshipTables.length !== 1) return undefined;
  return { [relationshipTables[0]]: column };
}
