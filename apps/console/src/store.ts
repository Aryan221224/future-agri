// Per-device preferences (language, Team/Farmer mode). Browser storage can be
// unavailable (private windows, locked-down kiosks): every access is guarded.
export function load<T>(key: string, fallback: T): T {
  try {
    const v = window.localStorage.getItem(`fa.${key}`)
    return v === null ? fallback : (JSON.parse(v) as T)
  } catch {
    return fallback
  }
}
export function save(key: string, value: unknown) {
  try {
    window.localStorage.setItem(`fa.${key}`, JSON.stringify(value))
  } catch {
    /* storage unavailable: preference lasts for this visit only */
  }
}
