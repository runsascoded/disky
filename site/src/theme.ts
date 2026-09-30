import { useEffect, useState } from 'react'

// Site theme (system / dark / light), persisted; `data-theme` on <html> drives
// the SCSS tokens. Shared by every page's SpeedDial + the `shift+d` action.
export type Theme = 'system' | 'dark' | 'light'
const THEME_KEY = 'gcs-usage:theme'

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem(THEME_KEY) as Theme) || 'system')
  useEffect(() => {
    if (theme === 'system') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = theme
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])
  return [theme, () => setTheme(t => (t === 'system' ? 'dark' : t === 'dark' ? 'light' : 'system'))]
}
