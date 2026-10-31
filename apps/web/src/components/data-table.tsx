import { Fragment, useId, useState, type ReactNode } from "react";

import { Button } from "./button";
import { CopyControl } from "./copy-control";
import { Icon } from "./icon";

export interface TableColumn<Row> {
  accessibleValue?: (row: Row) => string;
  header: string;
  headerContent?: ReactNode;
  id: string;
  numeric?: boolean;
  render: (row: Row) => ReactNode;
  truncate?: boolean;
}

export interface DataTableProps<Row> {
  caption: string;
  columns: readonly TableColumn<Row>[];
  /** Optional window groups; absent preserves the original table DOM. */
  headerGroups?: readonly {
    id: string;
    header: string;
    columnIds: readonly string[];
  }[];
  emptyContent?: ReactNode;
  getRowKey: (row: Row) => string;
  loading?: boolean;
  rows: readonly Row[];
  skeletonRows?: number;
  /** Opt-in disclosure; omitting it preserves the original table markup. */
  expansion?: {
    label: (row: Row) => string;
    render: (row: Row) => ReactNode;
  };
}

export function DataTable<Row>({
  caption,
  columns,
  headerGroups,
  emptyContent = "暂无数据",
  getRowKey,
  loading = false,
  rows,
  skeletonRows = 6,
  expansion,
}: DataTableProps<Row>) {
  const [expandedKeys, setExpandedKeys] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const expansionId = useId();
  const allExpanded =
    rows.length > 0 && rows.every((row) => expandedKeys.has(getRowKey(row)));
  const columnCount = columns.length + (expansion ? 1 : 0);
  return (
    <div
      aria-busy={loading}
      aria-label={loading ? `${caption}，正在加载` : caption}
      className="pw-table-wrap"
    >
      {expansion ? (
        <div className="pw-table__expansion-toolbar">
          <Button
            disabled={loading || rows.length === 0}
            onClick={() => {
              setExpandedKeys(
                allExpanded ? new Set() : new Set(rows.map(getRowKey)),
              );
            }}
            size="s"
            variant="ghost"
          >
            {allExpanded ? "全部收起" : "全部展开"}
          </Button>
        </div>
      ) : null}
      <table className="pw-table">
        <caption className="pw-visually-hidden">{caption}</caption>
        {headerGroups ? (
          <colgroup>
            {expansion ? <col data-column="expand" /> : null}
            {columns.map(({ id }) => (
              <col data-column={id} key={id} />
            ))}
          </colgroup>
        ) : null}
        <thead>
          {headerGroups ? (
            <tr className="pw-table__header-groups">
              {expansion ? <th aria-hidden="true" /> : null}
              {columns.map((column) => {
                const group = headerGroups.find(({ columnIds }) =>
                  columnIds.includes(column.id),
                );
                if (!group) return <th aria-hidden="true" key={column.id} />;
                const members = columns.filter(({ id }) =>
                  group.columnIds.includes(id),
                );
                if (members[0]?.id !== column.id) return null;
                return (
                  <th
                    colSpan={members.length}
                    scope="colgroup"
                    data-group={group.id}
                    key={group.id}
                  >
                    <span>{group.header}</span>
                  </th>
                );
              })}
            </tr>
          ) : null}
          <tr>
            {expansion ? (
              <th data-column="expand">
                <span className="pw-visually-hidden">展开指标</span>
              </th>
            ) : null}
            {columns.map((column) => (
              <th
                aria-label={
                  column.headerContent === undefined ? undefined : column.header
                }
                className={column.numeric ? "pw-table__numeric" : undefined}
                data-column={column.id}
                scope={headerGroups ? "col" : undefined}
                key={column.id}
              >
                {column.headerContent ?? column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading
            ? Array.from({ length: skeletonRows }, (_, rowIndex) => (
                <tr aria-hidden="true" key={`skeleton-${String(rowIndex)}`}>
                  {expansion ? (
                    <td>
                      <span className="sk pw-table__skeleton" />
                    </td>
                  ) : null}
                  {columns.map((column) => (
                    <td key={column.id}>
                      <span className="sk pw-table__skeleton" />
                    </td>
                  ))}
                </tr>
              ))
            : rows.map((row) => (
                <Fragment key={getRowKey(row)}>
                  <tr>
                    {expansion ? (
                      <td data-column="expand">
                        <Button
                          aria-controls={
                            expandedKeys.has(getRowKey(row))
                              ? `${expansionId}-${getRowKey(row)}`
                              : undefined
                          }
                          aria-expanded={expandedKeys.has(getRowKey(row))}
                          aria-label={`${expandedKeys.has(getRowKey(row)) ? "收起" : "展开"}${expansion.label(row)}的指标`}
                          className="pw-table__expand-button"
                          icon={
                            expandedKeys.has(getRowKey(row))
                              ? "chevronDown"
                              : "chevronRight"
                          }
                          onClick={() => {
                            setExpandedKeys((current) => {
                              const next = new Set(current);
                              if (next.has(getRowKey(row)))
                                next.delete(getRowKey(row));
                              else next.add(getRowKey(row));
                              return next;
                            });
                          }}
                          size="s"
                          variant="ghost"
                        />
                      </td>
                    ) : null}
                    {columns.map((column) => {
                      const accessibleValue = column.accessibleValue?.(row);
                      return (
                        <td
                          aria-label={accessibleValue}
                          className={[
                            column.numeric ? "pw-table__numeric" : undefined,
                            column.truncate ? "pw-table__truncate" : undefined,
                          ]
                            .filter(Boolean)
                            .join(" ")}
                          data-column={column.id}
                          key={column.id}
                          title={accessibleValue}
                        >
                          {column.render(row)}
                        </td>
                      );
                    })}
                  </tr>
                  {expansion && expandedKeys.has(getRowKey(row)) ? (
                    <tr
                      className="pw-table__expanded-row"
                      id={`${expansionId}-${getRowKey(row)}`}
                    >
                      <td colSpan={columnCount}>{expansion.render(row)}</td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
          {!loading && rows.length === 0 ? (
            <tr>
              <td className="pw-table__empty" colSpan={columnCount}>
                {emptyContent}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

export interface TruncatedValueProps {
  copyLabel?: string;
  value: string;
}

export function TruncatedValue({
  copyLabel = "复制完整值",
  value,
}: TruncatedValueProps) {
  return (
    <span className="pw-truncated-value">
      <span
        aria-label={value}
        className="pw-truncated-value__text"
        title={value}
      >
        {value}
      </span>
      <CopyControl aria-label={copyLabel} label="复制" value={value} />
    </span>
  );
}

export interface MissingValueProps {
  kind: "unavailable" | "not-applicable";
  lastUpdated?: string;
  reason: string;
}

export function MissingValue({ kind, lastUpdated, reason }: MissingValueProps) {
  const reasonId = useId();
  return (
    <span className="pw-missing-value" title={reason}>
      <span
        aria-describedby={reasonId}
        aria-label="暂无数据"
        className="pw-missing-value__mark"
      >
        —
      </span>
      {kind === "unavailable" ? <Icon name="notice" size="xs" /> : null}
      <span className="pw-visually-hidden" id={reasonId}>
        {reason}
        {lastUpdated ? `，最后更新：${lastUpdated}` : ""}
      </span>
      {lastUpdated ? (
        <span aria-hidden="true">最后更新：{lastUpdated}</span>
      ) : null}
    </span>
  );
}
