/**
 * S7：从用户消息里识别 `/技能名`（与后端 `skills/invocation.py` 同一套规则）。
 *
 * 用于在会话里把"用户显式指定了哪个技能"呈现出来——用户指定即直接注入，
 * 与模型侧目录无关，所以要有可见回执。
 */
const SKILL_NAME = "[a-z][a-z0-9-]{0,63}";
const COMMAND_RE = new RegExp(`(?:^|\\s)/(${SKILL_NAME})(?=\\s|$)`, "g");
const MAX_COMMANDS = 3;

export function skillCommandsOf(content: string): string[] {
  if (!content) return [];
  const names: string[] = [];
  for (const match of content.matchAll(COMMAND_RE)) {
    const name = match[1];
    if (!names.includes(name)) names.push(name);
    if (names.length >= MAX_COMMANDS) break;
  }
  return names;
}
