import type {
  PermissionLimits,
  PermissionLimitsMap,
} from "../api/user-contracts";

export const DEFAULT_PERMISSION_LIMITS: PermissionLimits = {
  max_concurrent_requests: 8,
  max_requests_per_minute: null,
};

export function changedPermissionLimits(
  draft: PermissionLimitsMap,
  saved: PermissionLimitsMap,
  granted: (id: string) => boolean,
): PermissionLimitsMap {
  return Object.fromEntries(
    Object.entries(draft).filter(([id, value]) => {
      const before = saved[id] ?? DEFAULT_PERMISSION_LIMITS;
      return (
        granted(id) &&
        (value.max_concurrent_requests !== before.max_concurrent_requests ||
          value.max_requests_per_minute !== before.max_requests_per_minute)
      );
    }),
  );
}

export function PermissionLimitFields({
  name,
  value,
  onChange,
  serviceSide = false,
  disabled = false,
}: {
  name: string;
  value: PermissionLimits;
  onChange: (value: PermissionLimits) => void;
  serviceSide?: boolean;
  disabled?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "var(--pw-space-8)",
      }}
    >
      {(
        [
          [
            "max_concurrent_requests",
            "同时请求",
            "同时请求上限",
            [1, 2, 4, 8, 16, 32, 64],
            "个",
          ],
          [
            "max_requests_per_minute",
            serviceSide ? "每分钟" : "每分钟请求",
            "每分钟请求上限",
            [10, 30, 60, 120, 300, 600],
            "次",
          ],
        ] as const
      ).map(([field, label, accessible, presets, unit]) => {
        const current = value[field];
        const options: number[] = [...presets];
        if (current !== null && !options.includes(current))
          options.push(current);
        options.sort((a, b) => a - b);
        return (
          <label
            key={field}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "var(--pw-space-4)",
              color: "var(--pw-text-secondary)",
              fontSize: "var(--pw-size-aux)",
              whiteSpace: "nowrap",
            }}
          >
            {label}
            <select
              style={{
                minHeight: "var(--pw-control-h-s)",
                padding: "var(--pw-space-4) var(--pw-space-8)",
                border:
                  "var(--pw-width-hairline) solid var(--pw-border-hairline)",
                borderRadius: "var(--pw-radius-sm)",
                background: "var(--pw-surface-base)",
                color: "var(--pw-text-primary)",
                font: "inherit",
              }}
              aria-label={`${name} 的${accessible}`}
              disabled={disabled}
              value={current === null ? "" : String(current)}
              onChange={(event) => {
                onChange({
                  ...value,
                  [field]:
                    event.target.value === ""
                      ? null
                      : Number(event.target.value),
                });
              }}
            >
              {options.map((option) => (
                <option key={option} value={option}>
                  {option} {unit}
                </option>
              ))}
              <option value="">不限制</option>
            </select>
          </label>
        );
      })}
    </div>
  );
}
