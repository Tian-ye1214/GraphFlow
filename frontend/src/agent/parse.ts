const GOAL_MARKER = /<!--\s*REDLOTUS_GOAL\s*:\s*(CONTINUE|DONE)\s*-->/gi
const CONFIRM = /^\[confirm_delete\]\s*(.+)$/gm

export function stripGoalMarkers(text: string): string {
  return text.replace(GOAL_MARKER, '').trim()
}

export function extractConfirmDeletes(text: string): { text: string; commands: string[] } {
  const commands: string[] = []
  const cleaned = text.replace(CONFIRM, (_, cmd: string) => {
    commands.push(cmd.trim())
    return ''
  }).trim()
  return { text: cleaned, commands }
}
