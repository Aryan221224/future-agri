import * as THREE from 'three'
import type { Frame, MissionResult } from '../api'
import type { Drone } from './DroneModel'
import type { FarmScene } from './FarmScene'

export interface Gauges {
  t: number; duration: number; soc: number; v_cell: number; liquid_l: number; flow_lpm: number
  speed_ms: number; z: number; sortie: number; phase: string; spraying: boolean
}

/** Flies a simulated mission over the 3D farm: the drone follows the track keyframes,
 *  the flown path is drawn (orange where it sprayed), gauges report every frame. */
export class Playback {
  t = 0
  speed = 30
  playing = false
  readonly duration: number
  onFrame: (g: Gauges) => void = () => undefined
  private frames: Frame[]
  private pts: THREE.Vector3[] = []
  private path: THREE.Line
  private planned: THREE.LineSegments
  private pad: THREE.Mesh
  private heading = 0
  private vScale = 1
  private hook = (dt: number) => this.step(dt)
  private last = -1

  constructor(private farm: FarmScene, private drone: Drone, res: MissionResult, private tankL: number, scale = 5) {
    this.frames = res.track
    this.duration = this.frames.length ? this.frames[this.frames.length - 1].t : 0
    this.drone.group.scale.setScalar(scale)
    this.vScale = Math.max(1, scale * 0.6)
    farm.add(drone.group)
    const n = this.frames.length
    const pos = new Float32Array(n * 3)
    const col = new Float32Array(n * 3)
    const orange = new THREE.Color(0xf25c05), grey = new THREE.Color(0x6b6f74)
    for (let i = 0; i < n; i++) {
      const f = this.frames[i]
      const p = farm.world(f.x, f.y, f.z * this.vScale + 0.15)
      this.pts.push(p)
      pos.set([p.x, p.y, p.z], i * 3)
      const c = f.spraying ? orange : grey
      col.set([c.r, c.g, c.b], i * 3)
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3))
    g.setAttribute('color', new THREE.BufferAttribute(col, 3))
    g.setDrawRange(0, 0)
    this.path = new THREE.Line(g, new THREE.LineBasicMaterial({ vertexColors: true }))
    farm.add(this.path)
    const seg: THREE.Vector3[] = []
    for (const r of res.route) {
      seg.push(farm.world(r.start[0], r.start[1], 0.35), farm.world(r.end[0], r.end[1], 0.35))
    }
    this.planned = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(seg),
      new THREE.LineDashedMaterial({ color: 0x2a231c, dashSize: 1.2, gapSize: 1.2, transparent: true, opacity: 0.45 }))
    this.planned.computeLineDistances()
    farm.add(this.planned)
    this.pad = new THREE.Mesh(new THREE.CircleGeometry(Math.max(1.2, scale * 0.35), 32).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: 0xf25c05, transparent: true, opacity: 0.85 }))
    if (n) this.pad.position.copy(farm.world(this.frames[0].x, this.frames[0].y, 0.08))
    farm.add(this.pad)
    farm.frameHooks.add(this.hook)
    this.seek(0)
  }

  setScale(k: number) {
    this.drone.group.scale.setScalar(k)
  }

  play() { if (this.t >= this.duration) this.t = 0; this.playing = true }
  pause() { this.playing = false }

  seek(t: number) {
    this.t = Math.max(0, Math.min(this.duration, t))
    this.render()
  }

  private step(dt: number) {
    this.drone.tick(dt)
    if (!this.playing) return
    this.t += dt * this.speed
    if (this.t >= this.duration) { this.t = this.duration; this.playing = false }
    this.render()
  }

  private index(t: number): number {
    const f = this.frames
    let lo = 0, hi = f.length - 1
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1
      if (f[mid].t <= t) lo = mid; else hi = mid - 1
    }
    return lo
  }

  private render() {
    const f = this.frames
    if (!f.length) return
    const i = this.index(this.t)
    const a = f[i], b = f[Math.min(i + 1, f.length - 1)]
    const span = b.t - a.t
    const k = span > 0 ? Math.min(1, (this.t - a.t) / span) : 1
    const pa = this.pts[i], pb = this.pts[Math.min(i + 1, f.length - 1)]
    const p = pa.clone().lerp(pb, k)
    const lift = (a.z + (b.z - a.z) * k) * this.vScale
    const ground = this.farm.world(a.x + (b.x - a.x) * k, a.y + (b.y - a.y) * k, 0).y
    p.y = ground + lift + 0.35 * this.drone.group.scale.x
    this.drone.group.position.copy(p)
    const dx = b.x - a.x, dy = b.y - a.y
    if (Math.hypot(dx, dy) > 0.05) this.heading = Math.atan2(dy, dx)
    this.drone.group.rotation.y = this.heading
    const spraying = b.spraying && span > 0
    const airborne = a.z > 0.05 || b.z > 0.05
    this.drone.setSpin(airborne ? 1 : this.t >= this.duration ? 0 : 0.12)
    this.drone.setSpraying(spraying, Math.max(0.5, b.z))
    this.drone.setFill(a.liquid_l / this.tankL)
    if (i !== this.last) {
      this.path.geometry.setDrawRange(0, i + 1)
      this.last = i
    }
    const speed = span > 0 ? Math.hypot(dx, dy, b.z - a.z) / span : 0
    this.onFrame({
      t: this.t, duration: this.duration, soc: a.soc + (b.soc - a.soc) * k, v_cell: a.v_cell + (b.v_cell - a.v_cell) * k,
      liquid_l: a.liquid_l + (b.liquid_l - a.liquid_l) * k, flow_lpm: spraying ? b.flow_lpm : 0, speed_ms: speed,
      z: a.z + (b.z - a.z) * k, sortie: b.sortie, phase: b.phase, spraying,
    })
  }

  dispose() {
    this.farm.frameHooks.delete(this.hook)
    for (const o of [this.path, this.planned, this.pad]) {
      this.farm.remove(o)
      o.geometry.dispose()
      ;(o.material as THREE.Material).dispose()
    }
    this.farm.remove(this.drone.group)
  }
}
