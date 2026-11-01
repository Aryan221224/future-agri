import * as THREE from 'three'
import type { DroneSpec } from '../api'

/** The 10 L K3-A Pro sprayer built to its spec sheet: 1300 mm wheelbase (motors at
 *  +-0.46 m), four 0.881 m (34.7 in) rotors, 10 L tank, four nozzles under the
 *  rotors, 920 x 920 x 480 mm unfolded. Forward is +X. Units: metres. */
export interface Drone {
  group: THREE.Group
  setSpin: (k: number) => void
  setSpraying: (on: boolean, heightM?: number) => void
  setFill: (frac: number) => void
  tick: (dt: number) => void
}

export function buildDrone(spec: DroneSpec): Drone {
  const g = new THREE.Group()
  g.name = 'K3-A Pro'
  const dark = new THREE.MeshStandardMaterial({ color: 0x2a2e33, roughness: 0.45, metalness: 0.25 })
  const shell = new THREE.MeshStandardMaterial({ color: 0xd6dbd5, roughness: 0.55, metalness: 0.05 })
  const metal = new THREE.MeshStandardMaterial({ color: 0x9aa1a7, roughness: 0.35, metalness: 0.7 })
  const tankMat = new THREE.MeshStandardMaterial({ color: 0xf4f6f3, roughness: 0.3, transparent: true, opacity: 0.5, depthWrite: false })
  const liquidMat = new THREE.MeshStandardMaterial({ color: 0x3aa69c, roughness: 0.2, transparent: true, opacity: 0.85 })
  const add = (geo: THREE.BufferGeometry, mat: THREE.Material, x = 0, y = 0, z = 0) => {
    const m = new THREE.Mesh(geo, mat)
    m.position.set(x, y, z)
    m.castShadow = true
    g.add(m)
    return m
  }
  // body, battery and mast
  add(new THREE.BoxGeometry(0.34, 0.14, 0.3), shell, 0, 0, 0)
  add(new THREE.BoxGeometry(0.24, 0.07, 0.15), dark, -0.01, 0.105, 0)
  add(new THREE.BoxGeometry(0.06, 0.02, 0.16), new THREE.MeshStandardMaterial({ color: 0xf25c05, roughness: 0.5 }), 0.1, 0.105, 0)
  add(new THREE.CylinderGeometry(0.007, 0.007, 0.12, 8), metal, -0.1, 0.2, 0)
  add(new THREE.CylinderGeometry(0.045, 0.045, 0.02, 20), dark, -0.1, 0.27, 0)
  add(new THREE.BoxGeometry(0.04, 0.035, 0.05), dark, 0.18, -0.01, 0)         // FPV camera
  // tank with liquid
  add(new THREE.BoxGeometry(0.25, 0.2, 0.25), tankMat, 0, -0.17, 0)
  const liquid = add(new THREE.BoxGeometry(0.23, 0.18, 0.23).translate(0, 0.09, 0), liquidMat, 0, -0.26, 0)
  // landing gear
  for (const z of [-0.23, 0.23]) {
    const skid = add(new THREE.CylinderGeometry(0.014, 0.014, 0.62, 10), dark, 0, -0.3, z)
    skid.rotation.z = Math.PI / 2
    for (const x of [-0.13, 0.13]) {
      const leg = add(new THREE.CylinderGeometry(0.012, 0.012, 0.27, 8), dark, x, -0.17, z * 0.82)
      leg.rotation.x = z > 0 ? -0.25 : 0.25
    }
  }
  // arms, motors, rotors, nozzles, sprays
  const rotors: THREE.Group[] = []
  const blurs: THREE.Mesh[] = []
  const sprays: THREE.Mesh[] = []
  const diam = spec.rotor_diameter_m
  const sprayMat = new THREE.MeshBasicMaterial({ color: 0xcfe9f5, transparent: true, opacity: 0.28, depthWrite: false, side: THREE.DoubleSide })
  for (const m of spec.motors) {
    const mx = m.x, mz = -m.y
    const len = Math.hypot(mx, mz)
    const arm = add(new THREE.CylinderGeometry(0.02, 0.02, len, 10), dark, mx / 2, 0.02, mz / 2)
    arm.rotation.z = Math.PI / 2
    arm.rotation.y = -Math.atan2(mz, mx)
    add(new THREE.CylinderGeometry(0.045, 0.045, 0.07, 20), dark, mx, 0.065, mz)
    const led = add(new THREE.SphereGeometry(0.018, 10, 8), new THREE.MeshStandardMaterial({
      color: mx > 0 ? 0x2fd36b : 0xe0453a, emissive: mx > 0 ? 0x1f9f4f : 0xb3261e, emissiveIntensity: 1.2 }), mx, 0.0, mz)
    led.castShadow = false
    const rotor = new THREE.Group()
    rotor.position.set(mx, 0.11, mz)
    const blade = new THREE.Mesh(new THREE.BoxGeometry(diam, 0.006, 0.055), dark)
    blade.castShadow = true
    rotor.add(blade)
    rotor.add(new THREE.Mesh(new THREE.CylinderGeometry(0.025, 0.025, 0.03, 12), metal))
    const blur = new THREE.Mesh(new THREE.CircleGeometry(diam / 2, 40).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: 0x1d2024, transparent: true, opacity: 0.1, depthWrite: false }))
    blur.visible = false
    rotor.add(blur)
    blurs.push(blur)
    rotors.push(rotor)
    g.add(rotor)
    add(new THREE.CylinderGeometry(0.012, 0.008, 0.05, 8), metal, mx, -0.07, mz)
    const cone = new THREE.Mesh(new THREE.ConeGeometry(1, 1, 20, 1, true).translate(0, -0.5, 0), sprayMat)
    cone.position.set(mx, -0.095, mz)
    cone.visible = false
    g.add(cone)
    sprays.push(cone)
  }
  let spin = 0.15
  return {
    group: g,
    setSpin: (k) => { spin = k; blurs.forEach((b) => { b.visible = k > 0.6 }) },
    setSpraying: (on, heightM = 2.5) => {
      for (const c of sprays) {
        c.visible = on
        // plume: spreads to about a quarter of the swath per nozzle at ground level
        c.scale.set(0.35 + heightM * 0.12, Math.max(0.3, heightM), 0.35 + heightM * 0.12)
      }
    },
    setFill: (f) => { liquid.scale.y = Math.max(0.02, Math.min(1, f)) },
    tick: (dt) => { for (let i = 0; i < rotors.length; i++) rotors[i].rotation.y += (i % 2 ? -1 : 1) * spin * 60 * dt },
  }
}

/** The aircraft on its own, on a turntable, for the spec view. */
export class DroneStudio {
  private renderer: THREE.WebGLRenderer
  private scene = new THREE.Scene()
  private camera = new THREE.PerspectiveCamera(32, 1, 0.05, 50)
  private drone: Drone
  private raf = 0
  private last = performance.now()
  private disposed = false
  private ro: ResizeObserver
  private controlsP: Promise<{ dispose: () => void; update: () => void; autoRotate: boolean }>

  constructor(private canvas: HTMLCanvasElement, spec: DroneSpec, bg: string) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    this.renderer.outputColorSpace = THREE.SRGBColorSpace
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping
    this.renderer.shadowMap.enabled = true
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap
    this.scene.background = new THREE.Color(bg)
    this.scene.add(new THREE.HemisphereLight(0xf5f7ff, 0x50463a, 1.2))
    const key = new THREE.DirectionalLight(0xffffff, 2.2)
    key.position.set(2, 3, 1.5)
    key.castShadow = true
    key.shadow.mapSize.set(1024, 1024)
    const sc = key.shadow.camera as THREE.OrthographicCamera
    sc.left = -1.5; sc.right = 1.5; sc.top = 1.5; sc.bottom = -1.5
    this.scene.add(key)
    const floor = new THREE.Mesh(new THREE.CircleGeometry(1.6, 64).rotateX(-Math.PI / 2),
      new THREE.MeshStandardMaterial({ color: 0xc9d1c9, roughness: 1 }))
    floor.position.y = -0.315
    floor.receiveShadow = true
    this.scene.add(floor)
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.645, 0.655, 96).rotateX(-Math.PI / 2), new THREE.MeshBasicMaterial({ color: 0xf25c05 }))
    ring.position.y = -0.31
    this.scene.add(ring)                                         // the 1300 mm wheelbase circle
    this.drone = buildDrone(spec)
    this.scene.add(this.drone.group)
    this.drone.setSpraying(true, 0.32)
    this.camera.position.set(2.5, 1.35, 2.5)
    this.camera.lookAt(0, -0.05, 0)
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    this.controlsP = import('three/addons/controls/OrbitControls.js').then(({ OrbitControls }) => {
      const c = new OrbitControls(this.camera, canvas)
      c.target.set(0, -0.05, 0)
      c.enableDamping = true
      c.autoRotate = !reduce
      c.autoRotateSpeed = 0.8
      c.minDistance = 0.9
      c.maxDistance = 6
      c.maxPolarAngle = Math.PI * 0.55
      return c
    })
    this.ro = new ResizeObserver(() => this.resize())
    this.ro.observe(canvas.parentElement ?? canvas)
    this.resize()
    this.loop()
  }

  private resize() {
    const p = this.canvas.parentElement
    const w = p?.clientWidth || 600, h = p?.clientHeight || 400
    this.renderer.setSize(w, h, false)
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
  }

  private loop = () => {
    if (this.disposed) return
    this.raf = requestAnimationFrame(this.loop)
    const now = performance.now()
    const dt = Math.min(0.1, (now - this.last) / 1000)
    this.last = now
    this.drone.tick(dt)
    this.controlsP.then((c) => c.update())
    this.renderer.render(this.scene, this.camera)
  }

  dispose() {
    this.disposed = true
    cancelAnimationFrame(this.raf)
    this.ro.disconnect()
    this.controlsP.then((c) => c.dispose())
    this.scene.traverse((o) => {
      const m = o as THREE.Mesh
      m.geometry?.dispose?.()
      const mat = m.material as THREE.Material | undefined
      mat?.dispose?.()
    })
    this.renderer.dispose()
  }
}
