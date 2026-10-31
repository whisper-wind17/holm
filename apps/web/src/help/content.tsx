import type { ReactNode } from "react";
import source from "../../../../docs/content/help-center.md?raw";

// Stable IDs belong here; entry points never carry business data or URLs.
export const HELP_TOPICS = {
  deployment: {
    id: "first-deployment",
    title: "首次部署",
    heading: "# Topic 1 · 首次部署",
    adminOnly: true,
  },
  publishing: {
    id: "service-publishing",
    title: "服务发布",
    heading: "# Topic 2 · 服务发布",
    adminOnly: true,
  },
  access: {
    id: "api-access",
    title: "API 接入",
    heading: "# Topic 3 · API 接入",
    adminOnly: false,
  },
  troubleshooting: {
    id: "troubleshooting",
    title: "故障排查",
    heading: "# Topic 4 · 故障排查（用户版）",
    adminOnly: false,
  },
} as const;
export type HelpTopicId = (typeof HELP_TOPICS)[keyof typeof HELP_TOPICS]["id"];
const adminHeading = "# Topic 4 · 故障排查（管理员版）";

export function readHelpSections(text: string): ReadonlyMap<string, string> {
  const sections = new Map<string, string>();
  for (const part of text.split(/(?=^# Topic )/m).slice(1)) {
    const [heading, ...lines] = part.trim().split("\n");
    if (!heading || sections.has(heading))
      throw new Error("Invalid help section");
    const body = lines
      .filter((line) => !line.startsWith("**可见性：**") && line !== "---")
      .join("\n")
      .trim();
    if (!body) throw new Error("Empty help section");
    sections.set(heading, body);
  }
  const headings = [
    ...Object.values(HELP_TOPICS).map((topic) => topic.heading),
    adminHeading,
  ];
  if (
    sections.size !== headings.length ||
    headings.some((heading) => !sections.has(heading))
  )
    throw new Error("Missing or unknown help section");
  return sections;
}
const sections = readHelpSections(source);

export function helpBody(id: string, admin: boolean): string | undefined {
  const topic = Object.values(HELP_TOPICS).find((item) => item.id === id);
  if (!topic || (topic.adminOnly && !admin)) return undefined;
  const body = sections.get(topic.heading);
  const extra = sections.get(adminHeading);
  if (body === undefined || extra === undefined)
    throw new Error("Missing help section");
  return id === HELP_TOPICS.troubleshooting.id && admin
    ? `${body}\n\n${extra}`
    : body;
}

function inline(text: string): ReactNode {
  return text
    .split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
    .map((part, index) =>
      part.startsWith("**") ? (
        <strong key={index}>{part.slice(2, -2)}</strong>
      ) : part.startsWith("`") ? (
        <code key={index}>{part.slice(1, -1)}</code>
      ) : (
        part
      ),
    );
}

// A deliberately small format for this versioned file, not an HTML/Markdown engine.
// Unsupported block syntax fails loudly; text is always escaped by React.
export function HelpBody({ text }: { text: string }) {
  return (
    <>
      {text.split(/\n\s*\n/).map((block, index) => {
        const lines = block.split("\n");
        if (/^###? /.test(block)) {
          if (lines.length !== 1) throw new Error("Invalid help heading");
          return block.startsWith("### ") ? (
            <h4 key={index}>{inline(block.slice(4))}</h4>
          ) : (
            <h3 key={index}>{inline(block.slice(3))}</h3>
          );
        }
        if (block.startsWith("|")) {
          const rows = lines.map((line) =>
            line
              .slice(1, -1)
              .split("|")
              .map((cell) => cell.trim()),
          );
          const headers = rows[0];
          if (
            !headers ||
            !rows[1]?.every((cell) => /^-+$/.test(cell)) ||
            rows.length < 3 ||
            rows.some((row) => row.length !== headers.length) ||
            lines.some((line) => !line.endsWith("|"))
          )
            throw new Error("Invalid help table");
          return (
            <div
              className="pw-help__table"
              key={index}
              role="region"
              aria-label={headers.join("、")}
              tabIndex={0}
            >
              <table>
                <thead>
                  <tr>
                    {headers.map((cell, i) => (
                      <th scope="col" key={i}>
                        {inline(cell)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.slice(2).map((row, i) => (
                    <tr key={i}>
                      {row.map((cell, j) => (
                        <td key={j}>{inline(cell)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        if (/^(?:- |\d+\. )/.test(block)) {
          const ordered = /^\d/.test(block);
          const pattern = ordered ? /^\d+\. / : /^- /;
          if (!lines.every((line) => pattern.test(line)))
            throw new Error("Invalid help list");
          const items = lines.map((line, i) => (
            <li key={i}>{inline(line.replace(pattern, ""))}</li>
          ));
          return ordered ? (
            <ol key={index}>{items}</ol>
          ) : (
            <ul key={index}>{items}</ul>
          );
        }
        if (/^[#>|]|^```/.test(block))
          throw new Error("Unsupported help block");
        return <p key={index}>{inline(block)}</p>;
      })}
    </>
  );
}
