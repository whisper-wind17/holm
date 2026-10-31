export interface AccessExamples {
  baseUrl: string;
  anthropicBaseUrl: string;
  curl: string;
  python: string;
  node: string;
  claudeCode: string;
  cursor: string;
  cline: string;
  continue: string;
}

export const API_KEY_PLACEHOLDER = "$HOLM_API_KEY";

export function buildAccessExamples(
  baseUrl: string,
  publicModelName: string,
  serviceName = publicModelName,
): AccessExamples {
  const withoutTrailingSlash = baseUrl.replace(/\/+$/, "");
  const normalizedBaseUrl = withoutTrailingSlash.endsWith("/v1")
    ? withoutTrailingSlash
    : `${withoutTrailingSlash}/v1`;
  const anthropicBaseUrl = normalizedBaseUrl.slice(0, -3);
  const continuation = ` ${String.fromCharCode(92)}`;
  return {
    baseUrl: normalizedBaseUrl,
    anthropicBaseUrl,
    curl: [
      `curl ${normalizedBaseUrl}/chat/completions${continuation}`,
      `  -H "Authorization: Bearer ${API_KEY_PLACEHOLDER}"${continuation}`,
      `  -H "Content-Type: application/json"${continuation}`,
      `  -d '{"model":"${publicModelName}","messages":[{"role":"user","content":"你好"}]}'`,
    ].join("\n"),
    claudeCode: [
      `export ANTHROPIC_BASE_URL=${anthropicBaseUrl}`,
      `export ANTHROPIC_API_KEY=${API_KEY_PLACEHOLDER}`,
      `export ANTHROPIC_MODEL=${publicModelName}`,
      "claude",
    ].join("\n"),
    node: `import OpenAI from 'openai';\n\nconst client = new OpenAI({\n  apiKey: process.env.HOLM_API_KEY,\n  baseURL: '${normalizedBaseUrl}',\n});\n\nconst res = await client.chat.completions.create({\n  model: '${publicModelName}',\n  messages: [{ role: 'user', content: '你好' }],\n});\nconsole.log(res.choices[0].message.content);`,
    cursor: `OpenAI API Key      <你保存的 API 密钥>\nOverride Base URL   ${normalizedBaseUrl}\nModel name          ${publicModelName}`,
    cline: `API Provider   OpenAI Compatible\nBase URL       ${normalizedBaseUrl}\nAPI Key        <你保存的 API 密钥>\nModel ID       ${publicModelName}`,
    continue: `models:\n  - name: ${JSON.stringify(serviceName)}\n    provider: openai\n    model: ${publicModelName}\n    apiBase: ${normalizedBaseUrl}\n    apiKey: <你保存的 API 密钥>`,
    python: `import os\n\nfrom openai import OpenAI\n\nclient = OpenAI(\n    api_key=os.environ["HOLM_API_KEY"],\n    base_url="${normalizedBaseUrl}",\n)\nresponse = client.chat.completions.create(\n    model="${publicModelName}",\n    messages=[{"role": "user", "content": "你好"}],\n)`,
  };
}
