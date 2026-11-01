import * as THREE from 'three'

// Sequential layers: one hue each, light -> dark. Zones: the validated categorical
// order (blue, aqua, yellow, violet, magenta; ALL CHECKS PASS light + dark), never cycled.
export const RAMPS = {
  green: ['#e6f2db', '#acd78e', '#62af56', '#2c7e3b', '#0f4d24'],
  rust: ['#fbe8d6', '#f3bd8f', '#df864e', '#b45523', '#76300f'],
  purple: ['#f0e8f7', '#cfb6e4', '#9d76c6', '#6c409a', '#3f1d62'],
  blue: ['#e3eefa', '#a9ccf0', '#5c9ce1', '#2a6bb8', '#123f78'],
  teal: ['#e0f3ef', '#a0d9ce', '#50b4a4', '#1f8174', '#0b4d45'],
} as const
/** Dark mode: the same hues stepped so zero recedes into the dark surface and the
 *  high end is the brightest (light ramps would make near-zero cells glare). */
export const RAMPS_DARK = {
  green: ['#22301f', '#2f5a2c', '#3f8a43', '#62b85e', '#a6e38a'],
  rust: ['#35261d', '#6b3a1f', '#a5532a', '#e07a3c', '#ffb072'],
  purple: ['#2c2236', '#4f3470', '#7a50a8', '#a87fd6', '#d9bdf5'],
  blue: ['#1c2939', '#24476e', '#2f6aa8', '#5c9be0', '#a9ccf0'],
  teal: ['#1b2f2b', '#1f5a52', '#2a8576', '#50b4a4', '#a0d9ce'],
} as const
export const ZONES_LIGHT = ['#2a78d6', '#1baf7a', '#eda100', '#6250d6', '#e87ba4']
export const ZONES_DARK = ['#3987e5', '#199e70', '#c98500', '#9085e9', '#d55181']

const cache = new Map<string, THREE.Color[]>()
function stops(r: readonly string[]): THREE.Color[] {
  const k = r.join()
  if (!cache.has(k)) cache.set(k, r.map((c) => new THREE.Color(c)))
  return cache.get(k)!
}

export function rampColor(r: readonly string[], t: number, out: THREE.Color): THREE.Color {
  const s = stops(r)
  const x = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0)) * (s.length - 1)
  const i = Math.min(s.length - 2, Math.floor(x))
  return out.copy(s[i]).lerp(s[i + 1], x - i)
}

export function cssGradient(r: readonly string[]): string {
  return `linear-gradient(90deg, ${r.join(', ')})`
}
