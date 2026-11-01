// Dark / light switch for the header (team console and farmer view).
import { useApp } from '../state'
import { Icon } from './icons'

export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useApp()
  return (
    <div className="seg theme-toggle" role="group" aria-label="Colour theme">
      <button type="button" aria-pressed={resolvedTheme === 'dark'} onClick={() => setTheme('dark')} title="Dark mode">
        <Icon name="moon" size={16} /><span>Dark</span>
      </button>
      <button type="button" aria-pressed={resolvedTheme === 'light'} onClick={() => setTheme('light')} title="Light mode">
        <Icon name="sun" size={16} /><span>Light</span>
      </button>
    </div>
  )
}
