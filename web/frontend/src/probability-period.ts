export function comparePeriods(left: string, right: string): number {
  const leftNumber = mapNumberForPeriod(left);
  const rightNumber = mapNumberForPeriod(right);
  if (leftNumber != null && rightNumber != null) return leftNumber - rightNumber;
  if (leftNumber != null) return -1;
  if (rightNumber != null) return 1;
  return left.localeCompare(right);
}

export function mapNumberForPeriod(period: string | null | undefined): number | null {
  const value = period?.match(/^(?:map|game)[_-]?(\d+)$/i)?.[1];
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

export function periodLabel(period: string): string {
  const number = mapNumberForPeriod(period);
  return number == null ? period : `\u7b2c ${number} \u5c40`;
}

export function resolvePeriod(
  periods: string[],
  selectedPeriod: string | null,
  preferredPeriod?: string | null,
  preferLatestPeriod = false,
): string | null {
  if (selectedPeriod && periods.includes(selectedPeriod)) return selectedPeriod;
  if (preferredPeriod && periods.includes(preferredPeriod)) return preferredPeriod;
  return (preferLatestPeriod ? periods.at(-1) : periods[0]) || null;
}
