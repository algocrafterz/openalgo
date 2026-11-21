import type { ThemeMode } from '@/stores/themeStore'

/**
 * Whether the page actually renders dark. The Analyze theme (`.analyzer`, dark purple) replaces
 * the `.dark` class instead of adding to it, so the stored light/dark mode alone is wrong there.
 * `.sandbox` is the light amber theme and follows the stored mode.
 */
export function isEffectiveDark(themeMode: ThemeMode, appMode: string): boolean {
  return themeMode === 'dark' || appMode === 'analyzer'
}
