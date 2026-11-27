/* A string with a weight at its end, simulated with Verlet integration, drawn as a thin tube.
   The last point is not drawn: it stands for the tag hanging from the string, and gives the
   direction the tag points in. */
import * as THREE from './three.js';

const GRAVITY = -15;
const DAMPING = 0.993; // kept per step: at 120 steps a second, swings die out in a few seconds
const STEP = 1 / 120;
const MAX_STEPS = 60; // half a second: after a longer pause the string just carries on
const ITERATIONS = 12;
const SIDES = 6;

const tangent = new THREE.Vector3();
const normal = new THREE.Vector3();
const binormal = new THREE.Vector3();
const held = new THREE.Vector3();
const TOWARDS_CAMERA = new THREE.Vector3(0, 0, 1);

export class Rope {
  constructor({ points = 9, length = 1.1, tail = 0.55, radius = 0.012, material }) {
    this.drawn = points;
    this.count = points + 1;
    this.rest = new Float32Array(this.count - 1).fill(length / (points - 1));
    this.rest[this.count - 2] = tail;
    this.fullRest = Float32Array.from(this.rest);
    this.now = new Float32Array(this.count * 3);
    this.before = new Float32Array(this.count * 3);
    this.radius = radius;
    this.leftover = 0;
    this.heldAt = new THREE.Vector3(); // where the top end was at the last step

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(points * SIDES * 3), 3));
    geometry.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(points * SIDES * 3), 3));
    const index = [];
    for (let ring = 0; ring < points - 1; ring += 1) {
      for (let side = 0; side < SIDES; side += 1) {
        const a = ring * SIDES + side;
        const b = ring * SIDES + ((side + 1) % SIDES);
        index.push(a, a + SIDES, b, b, a + SIDES, b + SIDES);
      }
    }
    geometry.setIndex(index);
    this.mesh = new THREE.Mesh(geometry, material);
    this.mesh.frustumCulled = false; // its bounds change every frame
    this.mesh.castShadow = true;
  }

  /** Shorten or lengthen the string along with the object it hangs from. */
  resize(factor) {
    const scale = Math.max(factor, 0.02);
    for (let i = 0; i < this.rest.length; i += 1) this.rest[i] = this.fullRest[i] * scale;
  }

  /** Hang straight down from ``anchor``, at rest. */
  reset(anchor) {
    let y = anchor.y;
    for (let i = 0; i < this.count; i += 1) {
      if (i > 0) y -= this.rest[i - 1];
      this.now.set([anchor.x, y, anchor.z], i * 3);
    }
    this.before.set(this.now);
    this.leftover = 0;
    this.heldAt.copy(anchor);
  }

  /** Advance by ``seconds``, the top end moving to ``anchor``, ``wind`` pushing everything.
      The time is cut into fixed steps, and the hand holding the string travels smoothly across
      them, so a long frame does not yank it. */
  advance(anchor, seconds, wind) {
    this.leftover = Math.min(this.leftover + seconds, STEP * MAX_STEPS);
    const steps = Math.floor(this.leftover / STEP);
    for (let i = 1; i <= steps; i += 1) {
      this.step(held.lerpVectors(this.heldAt, anchor, i / steps), wind);
    }
    if (steps > 0) {
      this.leftover -= steps * STEP;
      this.heldAt.copy(anchor);
    }
    this.draw();
  }

  /** Let it come to rest where it is now: for people who asked for no motion. */
  settle(anchor) {
    for (let i = 0; i < 240; i += 1) this.step(anchor, null);
    this.before.set(this.now);
    this.heldAt.copy(anchor);
    this.draw();
  }

  step(anchor, wind) {
    const { now, before, count, rest } = this;
    const push = STEP * STEP;
    for (let i = 1; i < count; i += 1) {
      const o = i * 3;
      for (let axis = 0; axis < 3; axis += 1) {
        const position = now[o + axis];
        let force = axis === 1 ? GRAVITY : 0;
        if (wind) force += wind.getComponent(axis);
        now[o + axis] = position + (position - before[o + axis]) * DAMPING + force * push;
        before[o + axis] = position;
      }
    }
    for (let pass = 0; pass < ITERATIONS; pass += 1) {
      now[0] = anchor.x; now[1] = anchor.y; now[2] = anchor.z;
      for (let i = 0; i < count - 1; i += 1) {
        const a = i * 3;
        const b = a + 3;
        const dx = now[b] - now[a];
        const dy = now[b + 1] - now[a + 1];
        const dz = now[b + 2] - now[a + 2];
        const distance = Math.hypot(dx, dy, dz) || 1e-6;
        const slack = (distance - rest[i]) / distance;
        // The held end does not give; elsewhere both ends share the correction.
        const give = i === 0 ? 0 : 0.5;
        const take = 1 - give;
        now[a] += dx * slack * give; now[a + 1] += dy * slack * give; now[a + 2] += dz * slack * give;
        now[b] -= dx * slack * take; now[b + 1] -= dy * slack * take; now[b + 2] -= dz * slack * take;
      }
    }
  }

  /** Where the tag hangs (the last drawn point). */
  end(target) {
    return target.fromArray(this.now, (this.drawn - 1) * 3);
  }

  /** Unit vector from the end of the string towards the weight: "down" for the tag. */
  pull(target) {
    const hole = (this.drawn - 1) * 3;
    const weight = this.drawn * 3;
    target.set(this.now[weight] - this.now[hole], this.now[weight + 1] - this.now[hole + 1], this.now[weight + 2] - this.now[hole + 2]);
    return target.lengthSq() > 1e-10 ? target.normalize() : target.set(0, -1, 0);
  }

  draw() {
    const { now, drawn, radius } = this;
    const positions = this.mesh.geometry.attributes.position;
    const normals = this.mesh.geometry.attributes.normal;
    for (let i = 0; i < drawn; i += 1) {
      const next = Math.min(i + 1, drawn - 1) * 3;
      const previous = Math.max(i - 1, 0) * 3;
      tangent.set(now[next] - now[previous], now[next + 1] - now[previous + 1], now[next + 2] - now[previous + 2]).normalize();
      normal.crossVectors(tangent, TOWARDS_CAMERA);
      if (normal.lengthSq() < 1e-8) normal.set(1, 0, 0);
      normal.normalize();
      binormal.crossVectors(tangent, normal);
      for (let side = 0; side < SIDES; side += 1) {
        const angle = (side / SIDES) * Math.PI * 2;
        const cos = Math.cos(angle);
        const sin = Math.sin(angle);
        const nx = normal.x * cos + binormal.x * sin;
        const ny = normal.y * cos + binormal.y * sin;
        const nz = normal.z * cos + binormal.z * sin;
        const vertex = i * SIDES + side;
        positions.setXYZ(vertex, now[i * 3] + nx * radius, now[i * 3 + 1] + ny * radius, now[i * 3 + 2] + nz * radius);
        normals.setXYZ(vertex, nx, ny, nz);
      }
    }
    positions.needsUpdate = true;
    normals.needsUpdate = true;
  }
}
