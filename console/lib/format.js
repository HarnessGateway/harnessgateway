// Values as a person reads them: sizes, times, numbers, and a typed list.

export const lines = text => String(text || "").split(/[\n,]+/).map(part => part.trim()).filter(Boolean);
export function bytes(count) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(count) || 0, unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit++; }
  return `${unit && value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
export function ago(time) {
  const seconds = Math.max(0, (Date.now() - Date.parse(time)) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}
/** A number worth reading: three figures of it, however small it is. */
export const figure = value => Number(value) ? Number(Number(value).toPrecision(3)).toLocaleString(undefined, { maximumFractionDigits: 6 }) : "0";
