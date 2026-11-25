import { Component, type ErrorInfo, type ReactNode } from 'react'

/** A page that throws while drawing shows what went wrong instead of a blank screen; navigating away
 *  (the parent re-keys this on every route change) or pressing "Try again" recovers it. */
export class ErrorBoundary extends Component<{ children: ReactNode; wide?: boolean }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) { return { error } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('page crashed:', error, info.componentStack) }

  render() {
    const e = this.state.error
    if (!e) return this.props.children
    const chunk = /dynamically imported module|Loading chunk|Failed to fetch/i.test(e.message)
    return (
      <div className="sheet" role="alert" style={{ margin: '24px auto', maxWidth: 720 }}>
        <h2>This page hit a problem</h2>
        <p className="muted">
          {chunk ? 'Part of the app could not be loaded (an old copy may be cached in the browser). Reload with Ctrl+F5.'
            : 'The rest of the console still works. Pick another page from the menu, or try again.'}
        </p>
        <pre className="small" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', background: 'var(--paper-2, #0001)', padding: 10, borderRadius: 6 }}>
          {e.name}: {e.message}{e.stack ? '\n\n' + e.stack.split('\n').slice(1, 6).join('\n') : ''}
        </pre>
        <p style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-primary" onClick={() => this.setState({ error: null })}>Try again</button>
          <button className="btn" onClick={() => { void navigator.serviceWorker?.getRegistrations().then((rs) => rs.forEach((r) => r.unregister())); location.reload() }}>Reload</button>
          <a className="btn" href="#/">Back to farms</a>
        </p>
      </div>
    )
  }
}
