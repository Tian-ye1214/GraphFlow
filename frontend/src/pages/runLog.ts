import type { RunLogEntry } from '../api/types'

export function formatRunLog(entries: RunLogEntry[]): string {
  return entries.map((e) => `[${e.created_at}] ${e.level.toUpperCase()} ${e.message}`).join('\n')
}
