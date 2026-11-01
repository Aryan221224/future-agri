import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { f16, f32, type Scene } from '../api'
import { RAMPS, ZONES_DARK, ZONES_LIGHT, rampColor } from './ramps'

export type Layer = 'canopy' | 'lai' | 'weeds' | 'blight' | 'dose' | 'zone' | 'twi'
export interface Snapshot {
  lai: Float32Array; cc: Float32Array; weeds: Float32Array; blight: Float32Array
  dose_fungicide: Float32Array; dose_herbicide: Float32Array
}
export interface Legend { kind: 'ramp'; ramp: readonly string[]; min: number; max: number; unit: string; label: string }
export interface Cats { kind: 'cats'; items: { color: string; label: string }[]; label: string }
export interface CellInfo { index: number; x: number; y: number; zone: number; twi?: number; lai?: number; cc?: number; weeds?: number; blight?: number; dose_fungicide?: number; dose_herbicide?: number }

/** Canopy height at full cover, metres (used for the 3D crop, scaled for visibility). */
const CROP_H: Record<string, number> = { potato: 0.6, maize: 2.4, rice: 1.0, soybean: 0.9, wheat: 0.9 }

export class FarmScene {
  private renderer: THREE.WebGLRenderer
  private scene = new THREE.Scene()
  private camera: THREE.PerspectiveCamera
  private controls: OrbitControls
  private canvas: HTMLCanvasElement
  private data: Scene
  private cx: number
  private cy: number
  private extent: number
  private heights: Float32Array
  private cellX: Float32Array
  private cellY: Float32Array
  private ground: Float32Array
  private zone: Float32Array
  private twi: Float32Array | null
  private terrain!: THREE.Mesh<THREE.BufferGeometry, THREE.MeshStandardMaterial>
  private canopy!: THREE.InstancedMesh<THREE.BoxGeometry, THREE.MeshStandardMaterial>
  private outline!: THREE.Line
  private bufferLine: THREE.Line | null = null
  private pins!: THREE.InstancedMesh
  private rings!: THREE.InstancedMesh<THREE.TorusGeometry, THREE.MeshBasicMaterial>
  private highlight!: THREE.LineSegments
  private route: THREE.Line | null = null
  private sun!: THREE.DirectionalLight
  private snap: Snapshot | null = null
  private layer: Layer = 'zone'
  private exag = 2
  private canopyScale = 4
  private cropH: number
  private dark: boolean
  private picked = -1
  private raf = 0
  private ro: ResizeObserver
  private disposed = false
  private reduceMotion = false
  private down: { x: number; y: number } | null = null
  private cellIndex = new Map<string, number>()
  onPick: (c: CellInfo | null) => void = () => undefined

  constructor(canvas: HTMLCanvasElement, data: Scene, opts: { crop: string; imageryUrl?: string | null; dark: boolean; bg: string }) {
    this.canvas = canvas
    this.data = data
    this.dark = opts.dark
    this.cropH = CROP_H[opts.crop] ?? 0.8
    const [minx, miny, maxx, maxy] = data.bounds
    this.cx = (minx + maxx) / 2
    this.cy = (miny + maxy) / 2
    this.extent = Math.max(maxx - minx, maxy - miny, 30)
    this.heights = f32(data.terrain.heights_b64)
    this.cellX = f32(data.cells.x_b64)
    this.cellY = f32(data.cells.y_b64)
    this.ground = f32(data.cells.ground_b64)
    this.zone = data.layers.zone_b64 ? f16(data.layers.zone_b64) : new Float32Array(data.cells.n)
    this.twi = data.layers.twi_b64 ? f16(data.layers.twi_b64) : null
    const c = data.cells.cell_m
    const x0 = Math.min(...this.cellX), y0 = Math.min(...this.cellY)
    for (let i = 0; i < data.cells.n; i++) this.cellIndex.set(`${Math.round((this.cellX[i] - x0) / c)},${Math.round((this.cellY[i] - y0) / c)}`, i)
    this.cellOrigin = [x0, y0]
    this.reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    this.renderer.outputColorSpace = THREE.SRGBColorSpace
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping
    this.renderer.toneMappingExposure = 1.0
    this.renderer.shadowMap.enabled = true
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap
    const bg = new THREE.Color(opts.bg)
    this.scene.background = bg
    this.scene.fog = new THREE.Fog(bg, this.extent * 2.5, this.extent * 7)

    this.camera = new THREE.PerspectiveCamera(40, 1, 0.3, this.extent * 30)
    this.controls = new OrbitControls(this.camera, canvas)
    this.controls.enableDamping = true
    this.controls.dampingFactor = 0.08
    this.controls.maxPolarAngle = Math.PI * 0.48
    this.controls.minDistance = 4
    this.controls.maxDistance = this.extent * 8
    this.resetView()

    this.scene.add(new THREE.HemisphereLight(0xf4f7ff, 0x5b4a36, 1.15))
    this.sun = new THREE.DirectionalLight(0xfff1da, 2.4)
    this.sun.position.set(this.extent * 0.7, this.extent * 1.3, this.extent * 0.45)
    this.sun.castShadow = true
    const sc = this.sun.shadow.camera as THREE.OrthographicCamera
    const s = this.extent * 0.85
    sc.left = -s; sc.right = s; sc.top = s; sc.bottom = -s; sc.near = 1; sc.far = this.extent * 4
    this.sun.shadow.mapSize.set(2048, 2048)
    this.sun.shadow.bias = -0.0005
    this.scene.add(this.sun)

    this.buildTerrain(opts.imageryUrl ?? null)
    this.buildField()
    this.buildCanopy()
    this.buildMarkers()
    this.applyLayer()

    this.ro = new ResizeObserver(() => this.resize())
    this.ro.observe(canvas.parentElement ?? canvas)
    this.resize()
    canvas.addEventListener('pointerdown', this.onDown)
    canvas.addEventListener('pointerup', this.onUp)
    this.loop()
  }

  private cellOrigin: [number, number]

  // ---------------------------------------------------------------- geometry
  private X(x: number) { return x - this.cx }
  private Z(y: number) { return -(y - this.cy) }

  private buildTerrain(imageryUrl: string | null) {
    const t = this.data.terrain
    const pos = new Float32Array(t.nx * t.ny * 3)
    const uv = new Float32Array(t.nx * t.ny * 2)
    for (let j = 0; j < t.ny; j++) for (let i = 0; i < t.nx; i++) {
      const k = j * t.nx + i
      pos[k * 3] = this.X(t.x0 + i * t.dx)
      pos[k * 3 + 1] = this.heights[k] * this.exag
      pos[k * 3 + 2] = this.Z(t.y0 + j * t.dy)
      uv[k * 2] = i / (t.nx - 1)
      uv[k * 2 + 1] = j / (t.ny - 1)
    }
    const idx: number[] = []
    for (let j = 0; j < t.ny - 1; j++) for (let i = 0; i < t.nx - 1; i++) {
      const a = j * t.nx + i, b = a + 1, c = a + t.nx, d = c + 1
      idx.push(a, b, c, b, d, c)
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3))
    g.setAttribute('uv', new THREE.BufferAttribute(uv, 2))
    g.setIndex(idx)
    g.computeVertexNormals()
    const mat = new THREE.MeshStandardMaterial({ color: imageryUrl ? 0xffffff : (this.dark ? 0x5a4a37 : 0xb59a72), roughness: 0.96, metalness: 0 })
    if (imageryUrl) {
      new THREE.TextureLoader().load(imageryUrl, (tex) => {
        if (this.disposed) { tex.dispose(); return }
        tex.colorSpace = THREE.SRGBColorSpace
        tex.anisotropy = this.renderer.capabilities.getMaxAnisotropy()
        mat.map = tex
        mat.needsUpdate = true
      })
    }
    this.terrain = new THREE.Mesh(g, mat)
    this.terrain.receiveShadow = true
    this.scene.add(this.terrain)
  }

  private groundAt(x: number, y: number): number {
    const t = this.data.terrain
    const fx = Math.min(t.nx - 1.001, Math.max(0, (x - t.x0) / t.dx))
    const fy = Math.min(t.ny - 1.001, Math.max(0, (y - t.y0) / t.dy))
    const i = Math.floor(fx), j = Math.floor(fy), ax = fx - i, ay = fy - j
    const h = this.heights, n = t.nx
    return (1 - ax) * (1 - ay) * h[j * n + i] + ax * (1 - ay) * h[j * n + i + 1] + (1 - ax) * ay * h[(j + 1) * n + i] + ax * ay * h[(j + 1) * n + i + 1]
  }

  private ringLine(ring: [number, number][], lift: number, mat: THREE.LineBasicMaterial | THREE.LineDashedMaterial): THREE.Line {
    const pts: THREE.Vector3[] = []
    for (let k = 0; k < ring.length - 1; k++) {
      const [ax, ay] = ring[k], [bx, by] = ring[k + 1]
      const steps = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / (this.data.terrain.dx || 5)))
      for (let s = 0; s < steps; s++) {
        const x = ax + ((bx - ax) * s) / steps, y = ay + ((by - ay) * s) / steps
        pts.push(new THREE.Vector3(this.X(x), this.groundAt(x, y) * this.exag + lift, this.Z(y)))
      }
    }
    pts.push(pts[0].clone())
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), mat)
    line.computeLineDistances()
    return line
  }

  private buildField() {
    if (this.outline) { this.scene.remove(this.outline); this.outline.geometry.dispose() }
    if (this.bufferLine) { this.scene.remove(this.bufferLine); this.bufferLine.geometry.dispose() }
    this.outline = this.ringLine(this.data.polygon, 0.25, new THREE.LineBasicMaterial({ color: this.dark ? 0xf3ead9 : 0x2a231c }))
    this.scene.add(this.outline)
    if (this.data.buffer.inner) {
      this.bufferLine = this.ringLine(this.data.buffer.inner, 0.25, new THREE.LineDashedMaterial({ color: 0xf25c05, dashSize: 2, gapSize: 1.5 }))
      this.scene.add(this.bufferLine)
    }
  }

  private buildCanopy() {
    const n = this.data.cells.n
    const geom = new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0)
    const mat = new THREE.MeshStandardMaterial({ roughness: 0.82, metalness: 0 })
    this.canopy = new THREE.InstancedMesh(geom, mat, n)
    this.canopy.castShadow = true
    this.canopy.receiveShadow = true
    const white = new THREE.Color(1, 1, 1)
    for (let i = 0; i < n; i++) this.canopy.setColorAt(i, white)
    this.scene.add(this.canopy)
    const hl = new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0))
    this.highlight = new THREE.LineSegments(hl, new THREE.LineBasicMaterial({ color: 0xf25c05 }))
    this.highlight.visible = false
    this.scene.add(this.highlight)
  }

  private buildMarkers() {
    const pin = new THREE.CylinderGeometry(0.06, 0.06, 1, 6).translate(0, 0.5, 0)
    const pinMat = new THREE.MeshStandardMaterial({ color: 0xb45523, roughness: 0.6 })
    this.pins = new THREE.InstancedMesh(pin, pinMat, 60)
    this.pins.count = 0
    this.scene.add(this.pins)
    const ring = new THREE.TorusGeometry(1, 0.08, 6, 28).rotateX(Math.PI / 2)
    const ringMat = new THREE.MeshBasicMaterial({ color: this.dark ? 0xcfb6e4 : 0x6c409a, transparent: true, opacity: 0.85 })
    this.rings = new THREE.InstancedMesh(ring, ringMat, 60)
    this.rings.count = 0
    this.scene.add(this.rings)
  }

  // ---------------------------------------------------------------- data
  setSnapshot(s: Snapshot | null) {
    this.snap = s
    this.applyLayer()
  }

  setLayer(l: Layer) {
    this.layer = l
    this.applyLayer()
  }

  setExaggeration(k: number) {
    this.exag = k
    const pos = this.terrain.geometry.getAttribute('position') as THREE.BufferAttribute
    for (let i = 0; i < pos.count; i++) pos.setY(i, this.heights[i] * k)
    pos.needsUpdate = true
    this.terrain.geometry.computeVertexNormals()
    this.buildField()
    this.applyLayer()
  }

  setCanopyScale(k: number) {
    this.canopyScale = k
    this.applyLayer()
  }

  setImagery(on: boolean) {
    const m = this.terrain.material
    m.color.set(on && m.map ? 0xffffff : (this.dark ? 0x5a4a37 : 0xb59a72))
    if (m.map) m.map.repeat.set(1, 1)
    m.userData.hideMap = !on
    if (!on && m.map) { m.userData.map = m.map; m.map = null } else if (on && m.userData.map) { m.map = m.userData.map }
    m.needsUpdate = true
  }

  private maxOf(a: Float32Array, q = 0.98): number {
    const v = Array.from(a).filter(Number.isFinite).sort((x, y) => x - y)
    return v.length ? Math.max(v[Math.floor(q * (v.length - 1))], 1e-6) : 1
  }

  private value(i: number): number {
    const s = this.snap
    switch (this.layer) {
      case 'lai': case 'canopy': return s ? s.lai[i] : 0
      case 'weeds': return s ? s.weeds[i] : 0
      case 'blight': return s ? s.blight[i] : 0
      case 'dose': return s ? s.dose_fungicide[i] + s.dose_herbicide[i] : 0
      case 'twi': return this.twi ? this.twi[i] : 0
      default: return this.zone[i]
    }
  }

  legend(): Legend | Cats {
    const s = this.snap
    switch (this.layer) {
      case 'zone': {
        const k = Math.max(...Array.from(this.zone)) + 1
        const pal = this.dark ? ZONES_DARK : ZONES_LIGHT
        return { kind: 'cats', label: 'Zones', items: Array.from({ length: k }, (_, i) => ({ color: pal[i % pal.length], label: `Zone ${i + 1}` })) }
      }
      case 'weeds': return { kind: 'ramp', ramp: RAMPS.rust, min: 0, max: s ? Math.max(5, this.maxOf(s.weeds)) : 5, unit: 'plants/m²', label: 'Weed density' }
      case 'blight': return { kind: 'ramp', ramp: RAMPS.purple, min: 0, max: s ? Math.max(0.05, this.maxOf(s.blight, 0.995)) : 0.05, unit: 'severity', label: 'Late blight' }
      case 'dose': return { kind: 'ramp', ramp: RAMPS.blue, min: 0, max: s ? Math.max(5, this.maxOf(new Float32Array(Array.from(s.dose_fungicide, (v, i) => v + s.dose_herbicide[i])))) : 20, unit: 'L/ha', label: 'Sprayed so far' }
      case 'twi': return { kind: 'ramp', ramp: RAMPS.teal, min: this.twi ? Math.min(...Array.from(this.twi)) : 0, max: this.twi ? this.maxOf(this.twi) : 1, unit: 'index', label: 'Terrain wetness' }
      default: return { kind: 'ramp', ramp: RAMPS.green, min: 0, max: 6, unit: 'm²/m²', label: this.layer === 'canopy' ? 'Crop canopy (leaf area index)' : 'Leaf area index' }
    }
  }

  private applyLayer() {
    const n = this.data.cells.n
    const c = this.data.cells.cell_m
    const m = new THREE.Matrix4()
    const col = new THREE.Color()
    const soil = new THREE.Color(this.dark ? 0x6b5640 : 0xa3855c)
    const brown = new THREE.Color(0x6e4a2c)
    const leg = this.legend()
    const pal = (this.dark ? ZONES_DARK : ZONES_LIGHT).map((x) => new THREE.Color(x))
    for (let i = 0; i < n; i++) {
      const lai = this.snap ? Math.max(0, this.snap.lai[i]) : 0
      const grow = this.snap ? Math.pow(Math.min(1, lai / 4), 0.6) : 0
      const h = Math.max(0.04, this.cropH * grow * this.canopyScale)
      const x = this.cellX[i], y = this.cellY[i]
      m.makeScale(c * 0.94, h, c * 0.94)
      m.setPosition(this.X(x), this.ground[i] * this.exag + 0.02, this.Z(y))
      this.canopy.setMatrixAt(i, m)
      if (this.layer === 'zone') col.copy(pal[Math.round(this.zone[i]) % pal.length])
      else if (this.layer === 'canopy') {
        rampColor(RAMPS.green, 0.35 + 0.65 * Math.min(1, lai / 5), col)
        col.lerp(soil, 1 - Math.min(1, (this.snap?.cc[i] ?? 0) + 0.15))
        col.lerp(brown, Math.min(0.85, (this.snap?.blight[i] ?? 0) * 3))
      } else if (leg.kind === 'ramp') rampColor(leg.ramp, (this.value(i) - leg.min) / (leg.max - leg.min || 1), col)
      this.canopy.setColorAt(i, col)
    }
    this.canopy.instanceMatrix.needsUpdate = true
    if (this.canopy.instanceColor) this.canopy.instanceColor.needsUpdate = true
    this.canopy.computeBoundingSphere()
    this.updateMarkers()
    if (this.picked >= 0) this.pick(this.picked)
  }

  private updateMarkers() {
    const s = this.snap
    const m = new THREE.Matrix4()
    let k = 0
    if (s) {
      const order = Array.from(s.weeds.keys()).filter((i) => s.weeds[i] > 2).sort((a, b) => s.weeds[b] - s.weeds[a]).slice(0, 40)
      for (const i of order) {
        const h = 2 + Math.min(6, s.weeds[i] / 4)
        m.makeScale(1, h, 1)
        m.setPosition(this.X(this.cellX[i]), this.ground[i] * this.exag, this.Z(this.cellY[i]))
        this.pins.setMatrixAt(k++, m)
      }
    }
    this.pins.count = k
    this.pins.instanceMatrix.needsUpdate = true
    this.pins.visible = this.layer === 'weeds' || this.layer === 'canopy'
    k = 0
    if (s) {
      const order = Array.from(s.blight.keys()).filter((i) => s.blight[i] > 0.02).sort((a, b) => s.blight[b] - s.blight[a]).slice(0, 40)
      for (const i of order) {
        const r = this.data.cells.cell_m * (0.8 + 2 * Math.min(1, s.blight[i] * 3))
        m.makeScale(r, 1, r)
        m.setPosition(this.X(this.cellX[i]), this.ground[i] * this.exag + this.cropH * this.canopyScale + 0.4, this.Z(this.cellY[i]))
        this.rings.setMatrixAt(k++, m)
      }
    }
    this.rings.count = k
    this.rings.instanceMatrix.needsUpdate = true
    this.rings.visible = this.layer === 'blight' || this.layer === 'canopy'
  }

  // ---------------------------------------------------------------- route (drone track)
  showRoute(points: { x: number; y: number; z: number }[] | null) {
    if (this.route) { this.scene.remove(this.route); this.route.geometry.dispose(); this.route = null }
    if (!points || points.length < 2) return
    const pts = points.map((p) => new THREE.Vector3(this.X(p.x), this.groundAt(p.x, p.y) * this.exag + p.z, this.Z(p.y)))
    this.route = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: 0xf25c05 }))
    this.scene.add(this.route)
  }

  // ---------------------------------------------------------------- picking
  private onDown = (e: PointerEvent) => { this.down = { x: e.clientX, y: e.clientY } }
  private onUp = (e: PointerEvent) => {
    if (!this.down || Math.hypot(e.clientX - this.down.x, e.clientY - this.down.y) > 5) return
    const r = this.canvas.getBoundingClientRect()
    const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1)
    const ray = new THREE.Raycaster()
    ray.setFromCamera(ndc, this.camera)
    const hit = ray.intersectObject(this.canopy, false)[0]
    let idx = hit?.instanceId ?? -1
    if (idx < 0) {
      const th = ray.intersectObject(this.terrain, false)[0]
      if (th) idx = this.cellAt(th.point.x + this.cx, -th.point.z + this.cy)
    }
    this.pick(idx)
  }

  private cellAt(x: number, y: number): number {
    const c = this.data.cells.cell_m
    return this.cellIndex.get(`${Math.round((x - this.cellOrigin[0]) / c)},${Math.round((y - this.cellOrigin[1]) / c)}`) ?? -1
  }

  pick(i: number) {
    this.picked = i
    if (i < 0) { this.highlight.visible = false; this.onPick(null); return }
    const m = new THREE.Matrix4()
    this.canopy.getMatrixAt(i, m)
    const p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3()
    m.decompose(p, q, s)
    this.highlight.position.copy(p)
    this.highlight.scale.set(s.x * 1.04, Math.max(s.y, 0.3) + 0.05, s.z * 1.04)
    this.highlight.visible = true
    const sn = this.snap
    const [minx, miny] = this.data.bounds
    this.onPick({
      index: i, x: this.cellX[i] - minx, y: this.cellY[i] - miny, zone: Math.round(this.zone[i]) + 1,
      twi: this.twi ? this.twi[i] : undefined,
      lai: sn?.lai[i], cc: sn?.cc[i], weeds: sn?.weeds[i], blight: sn?.blight[i],
      dose_fungicide: sn?.dose_fungicide[i], dose_herbicide: sn?.dose_herbicide[i],
    })
  }

  // ---------------------------------------------------------------- camera
  resetView() {
    const e = this.extent
    this.camera.position.set(e * 0.72, e * 0.9, e * 1.3)
    this.controls.target.set(0, 0, 0)
    this.controls.update()
  }

  topView() {
    this.camera.position.set(0, this.extent * 1.6, 0.01)
    this.controls.target.set(0, 0, 0)
    this.controls.update()
  }

  private resize() {
    const p = this.canvas.parentElement
    const w = p?.clientWidth || 800, h = p?.clientHeight || 600
    this.renderer.setSize(w, h, false)
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
  }

  private loop = () => {
    if (this.disposed) return
    this.raf = requestAnimationFrame(this.loop)
    this.controls.update()
    if (!this.reduceMotion && this.rings.count) this.rings.material.opacity = 0.6 + 0.3 * Math.sin(performance.now() / 450)
    this.renderer.render(this.scene, this.camera)
  }

  dispose() {
    this.disposed = true
    cancelAnimationFrame(this.raf)
    this.ro.disconnect()
    this.canvas.removeEventListener('pointerdown', this.onDown)
    this.canvas.removeEventListener('pointerup', this.onUp)
    this.controls.dispose()
    this.scene.traverse((o) => {
      const mesh = o as THREE.Mesh
      mesh.geometry?.dispose?.()
      const mat = mesh.material as THREE.Material | THREE.Material[] | undefined
      if (Array.isArray(mat)) mat.forEach((x) => x.dispose()); else mat?.dispose?.()
    })
    this.terrain.material.map?.dispose()
    this.renderer.dispose()
  }
}
