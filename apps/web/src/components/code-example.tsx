import { CopyControl } from "./copy-control";

export function CodeExample({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <section className="pw-code-example">
      <header>
        <h3>{label}</h3>
        <CopyControl label={`复制 ${label} 示例`} value={value} />
      </header>
      <pre>
        <code>{value}</code>
      </pre>
    </section>
  );
}
