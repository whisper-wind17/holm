/** display-only names; use code points to match PostgreSQL and Python. */
export function defaultInstanceName(
  modelName: string,
  occupied: readonly string[] = [],
) {
  let base = modelName.slice(modelName.lastIndexOf("/") + 1).trim();
  if (Array.from(base).length < 2) base = "模型实例";
  const used = new Set(occupied);
  for (let sequence = 1; sequence <= used.size + 1; sequence++) {
    const suffix = sequence === 1 ? "" : `-${String(sequence)}`;
    const candidate =
      Array.from(base)
        .slice(0, 64 - suffix.length)
        .join("")
        .trimEnd() + suffix;
    if (!used.has(candidate)) return candidate;
  }
  throw new Error("instance name candidates exhausted");
}
