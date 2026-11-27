/* The things people watch, built from simple solids: a graphics card, a TV, a gamepad,
   headphones, a phone. Plus the paper: hanging price tags and shelf labels.
   Every builder returns { group, anchor } where anchor is the point its tag hangs from. */
import * as THREE from './three.js';

const TURN = Math.PI * 2;

export function makeMaterials(colours) {
  const solid = (color, options) => new THREE.MeshStandardMaterial({ color, ...options });
  return {
    bone: new THREE.MeshPhysicalMaterial({ color: colours.bone, roughness: 0.48, clearcoat: 0.4, clearcoatRoughness: 0.35 }),
    graphite: solid(colours.graphite, { roughness: 0.42, metalness: 0.3 }),
    pit: solid(colours.pit, { roughness: 0.95 }),
    blade: solid(colours.bone, { roughness: 0.35, metalness: 0.05 }),
    metal: solid(colours.metal, { roughness: 0.26, metalness: 0.95 }),
    gold: solid(colours.gold, { roughness: 0.3, metalness: 1 }),
    board: solid(colours.board, { roughness: 0.7 }),
    rubber: solid(colours.pit, { roughness: 0.9 }),
    manila: solid(colours.manila, { roughness: 0.88 }),
    washer: solid(colours.manilaDeep, { roughness: 0.8 }),
    card: solid(colours.paper, { roughness: 0.82 }),
    cobalt: solid(colours.cobalt, { roughness: 0.4 }),
    deal: solid(colours.deal, { roughness: 0.45 }),
    glow: new THREE.MeshBasicMaterial({ color: colours.manila, toneMapped: false }),
    string: solid(colours.ink, { roughness: 0.9, side: THREE.DoubleSide }),
  };
}

function adder(group) {
  return (geometry, material, x = 0, y = 0, z = 0) => {
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(x, y, z);
    mesh.castShadow = true;
    group.add(mesh);
    return mesh;
  };
}

/** A disc facing the camera (cylinders are born standing up). */
function puck(radiusTop, radiusBottom, depth, segments = 40) {
  return new THREE.CylinderGeometry(radiusTop, radiusBottom, depth, segments).rotateX(Math.PI / 2);
}

function printed(texture) {
  return new THREE.MeshStandardMaterial({ map: texture, transparent: true, roughness: 0.9, depthWrite: false });
}

function lit(texture) {
  return new THREE.MeshBasicMaterial({ map: texture, toneMapped: false });
}

// ------------------------------------------------------------------ graphics card

function fanBlade() {
  const outline = new THREE.Shape();
  outline.moveTo(0.11, -0.045);
  outline.quadraticCurveTo(0.33, -0.2, 0.465, -0.13);
  outline.quadraticCurveTo(0.485, -0.02, 0.44, 0.05);
  outline.quadraticCurveTo(0.3, 0.03, 0.11, 0.06);
  outline.closePath();
  const blade = new THREE.ExtrudeGeometry(outline, { depth: 0.012, bevelEnabled: false, curveSegments: 10 });
  blade.translate(0, 0, -0.006);
  blade.rotateX(-0.4); // pitch, around the blade's own length
  return blade;
}

export function buildGpu(m) {
  const group = new THREE.Group();
  const add = adder(group);

  add(new THREE.RoundedBoxGeometry(3.3, 1.42, 0.06, 3, 0.03), m.graphite, 0, 0, -0.27); // backplate
  add(new THREE.BoxGeometry(3.22, 1.34, 0.03), m.board, 0, 0, -0.225);
  add(new THREE.BoxGeometry(1.25, 0.11, 0.02), m.gold, -0.75, -0.76, -0.225); // the edge that plugs in

  const finCount = 52;
  const fins = new THREE.InstancedMesh(new THREE.BoxGeometry(0.016, 1.26, 0.27), m.metal, finCount);
  const place = new THREE.Matrix4();
  for (let i = 0; i < finCount; i += 1) fins.setMatrixAt(i, place.makeTranslation(-1.53 + i * 0.06, 0, -0.075));
  fins.castShadow = true;
  group.add(fins);

  add(new THREE.RoundedBoxGeometry(3.3, 1.42, 0.2, 5, 0.085), m.bone, 0, 0, 0.16); // shroud
  add(new THREE.BoxGeometry(2.9, 0.035, 0.03), m.glow, 0, 0.705, 0.2);
  add(new THREE.RoundedBoxGeometry(0.42, 0.12, 0.2, 2, 0.02), m.graphite, 1.05, 0.74, -0.05); // power socket

  add(new THREE.BoxGeometry(0.045, 1.72, 0.62), m.metal, -1.67, -0.12, -0.02); // bracket
  for (let i = 0; i < 3; i += 1) add(new THREE.BoxGeometry(0.05, 0.26, 0.11), m.pit, -1.675, 0.42 - i * 0.37, 0.08);

  const well = puck(0.5, 0.5, 0.02, 56);
  const rim = new THREE.TorusGeometry(0.5, 0.028, 12, 72);
  const hub = puck(0.13, 0.145, 0.07, 32);
  const cap = new THREE.CircleGeometry(0.085, 32);
  const blade = fanBlade();
  const fans = [];
  for (const x of [-1.08, 0, 1.08]) {
    add(well, m.pit, x, 0, 0.262);
    add(rim, m.graphite, x, 0, 0.275);
    const fan = new THREE.Group();
    fan.position.set(x, 0, 0.3);
    const addToFan = adder(fan);
    addToFan(hub, m.graphite);
    addToFan(cap, m.cobalt, 0, 0, 0.0355).castShadow = false;
    for (let i = 0; i < 9; i += 1) addToFan(blade, m.blade).rotation.z = (i / 9) * TURN;
    group.add(fan);
    fans.push(fan);
  }

  return { group, fans, anchor: new THREE.Vector3(1.52, -0.7, 0.06) };
}

// ------------------------------------------------------------------ TV

export function buildTv(m, screenTexture) {
  const group = new THREE.Group();
  const add = adder(group);
  add(new THREE.RoundedBoxGeometry(4, 2.32, 0.09, 4, 0.045), m.graphite);
  add(new THREE.PlaneGeometry(3.86, 2.17), lit(screenTexture), 0, 0, 0.0465).castShadow = false;
  add(new THREE.BoxGeometry(0.5, 0.44, 0.06), m.metal, 0, -1.32, -0.05);
  add(new THREE.RoundedBoxGeometry(1.7, 0.06, 0.62, 2, 0.03), m.metal, 0, -1.55, 0);
  return { group, anchor: new THREE.Vector3(-1.86, -1.12, 0.05) };
}

// ------------------------------------------------------------------ gamepad

function gamepadBody() {
  const outline = new THREE.Shape();
  outline.moveTo(-0.75, 0.55);
  outline.bezierCurveTo(-0.4, 0.66, 0.4, 0.66, 0.75, 0.55); // top edge
  outline.bezierCurveTo(1.15, 0.5, 1.3, 0.1, 1.38, -0.45); // down the right shoulder
  outline.bezierCurveTo(1.42, -0.85, 1.05, -0.95, 0.9, -0.6); // round the right grip
  outline.bezierCurveTo(0.75, -0.3, 0.45, -0.22, 0, -0.22); // back up to the middle
  // The left half is the right one walked backwards, mirrored.
  outline.bezierCurveTo(-0.45, -0.22, -0.75, -0.3, -0.9, -0.6);
  outline.bezierCurveTo(-1.05, -0.95, -1.42, -0.85, -1.38, -0.45);
  outline.bezierCurveTo(-1.3, 0.1, -1.15, 0.5, -0.75, 0.55);
  const body = new THREE.ExtrudeGeometry(outline, {
    depth: 0.24, bevelEnabled: true, bevelThickness: 0.17, bevelSize: 0.15, bevelSegments: 10, curveSegments: 28,
  });
  body.translate(0, 0, -0.12);
  return body;
}

export function buildGamepad(m) {
  const group = new THREE.Group();
  const add = adder(group);
  const face = 0.29; // how far the front of the body is from its middle
  add(gamepadBody(), m.bone);
  add(new THREE.RoundedBoxGeometry(0.62, 0.32, 0.05, 3, 0.05), m.graphite, 0, 0.3, face);

  for (const x of [-0.42, 0.42]) {
    add(puck(0.2, 0.22, 0.05), m.graphite, x, -0.1, face);
    add(puck(0.07, 0.09, 0.12, 20), m.rubber, x, -0.1, face + 0.06);
    add(puck(0.15, 0.13, 0.06), m.rubber, x, -0.1, face + 0.14);
  }
  add(new THREE.RoundedBoxGeometry(0.36, 0.115, 0.07, 2, 0.03), m.graphite, -0.86, 0.22, face);
  add(new THREE.RoundedBoxGeometry(0.115, 0.36, 0.07, 2, 0.03), m.graphite, -0.86, 0.22, face);

  const button = puck(0.078, 0.078, 0.07, 24);
  const buttons = [[0, 0.15, m.cobalt], [0.15, 0, m.deal], [0, -0.15, m.manila], [-0.15, 0, m.graphite]];
  buttons.forEach(([dx, dy, material]) => add(button, material, 0.86 + dx, 0.22 + dy, face));
  for (const x of [-0.24, 0.24]) add(new THREE.CapsuleGeometry(0.028, 0.08, 4, 10), m.graphite, x, 0.02, face - 0.01).rotation.z = Math.PI / 2;

  return { group, anchor: new THREE.Vector3(1.12, -0.74, 0.05) };
}

// ------------------------------------------------------------------ headphones

export function buildHeadphones(m) {
  const group = new THREE.Group();
  const add = adder(group);
  const span = 0.95;
  add(new THREE.TorusGeometry(span, 0.07, 14, 56, Math.PI), m.graphite, 0, 0.1, 0);
  const pad = add(new THREE.TorusGeometry(span, 0.115, 14, 28, 1.1), m.manila, 0, 0.1, 0);
  pad.rotation.z = (Math.PI - 1.1) / 2;

  for (const side of [-1, 1]) {
    const x = side * span;
    add(new THREE.RoundedBoxGeometry(0.1, 0.46, 0.1, 2, 0.04), m.metal, x, -0.1, 0);
    const cup = new THREE.Group();
    cup.position.set(x, -0.5, 0);
    cup.rotation.z = Math.PI / 2; // the cup's axis runs from ear to ear
    const addToCup = adder(cup);
    addToCup(new THREE.CylinderGeometry(0.44, 0.4, 0.22, 48), m.graphite);
    addToCup(new THREE.CylinderGeometry(0.3, 0.3, 0.02, 40), m.bone, 0, -side * 0.118, 0);
    addToCup(new THREE.TorusGeometry(0.33, 0.12, 14, 44), m.manila, 0, side * 0.15, 0).rotation.x = Math.PI / 2;
    group.add(cup);
  }
  return { group, anchor: new THREE.Vector3(-span, -0.9, 0.05) };
}

// ------------------------------------------------------------------ phone

export function buildPhone(m, screenTexture) {
  const group = new THREE.Group();
  const add = adder(group);
  add(new THREE.RoundedBoxGeometry(1.06, 2.16, 0.11, 5, 0.13), m.graphite);
  add(new THREE.PlaneGeometry(0.98, 2.02), lit(screenTexture), 0, 0, 0.0565).castShadow = false;
  return { group, anchor: new THREE.Vector3(0, -1.08, 0) };
}

// ------------------------------------------------------------------ paper

const TAG_WIDTH = 0.92;
const TAG_HEIGHT = 1.38;
const TAG_TOP = 0.22; // from the hole up to the top edge

/** A hanging tag. Its origin is the hole the string goes through; ``back`` is the other face. */
export function buildTag(m, front, back) {
  const group = new THREE.Group();
  const add = adder(group);
  const half = TAG_WIDTH / 2;
  const cut = 0.2;
  const bottom = TAG_TOP - TAG_HEIGHT;
  const round = 0.05;

  const outline = new THREE.Shape();
  outline.moveTo(-half + cut, TAG_TOP);
  outline.lineTo(half - cut, TAG_TOP);
  outline.lineTo(half, TAG_TOP - cut);
  outline.lineTo(half, bottom + round);
  outline.quadraticCurveTo(half, bottom, half - round, bottom);
  outline.lineTo(-half + round, bottom);
  outline.quadraticCurveTo(-half, bottom, -half, bottom + round);
  outline.lineTo(-half, TAG_TOP - cut);
  outline.closePath();
  outline.holes.push(new THREE.Path().absarc(0, 0, 0.06, 0, TURN, true));

  const thickness = 0.02;
  const card = new THREE.ExtrudeGeometry(outline, { depth: thickness, bevelEnabled: false, curveSegments: 20 });
  card.translate(0, 0, -thickness / 2);
  add(card, m.manila);

  const washer = new THREE.RingGeometry(0.06, 0.125, 40);
  add(washer, m.washer, 0, 0, thickness / 2 + 0.001).castShadow = false;
  add(washer, m.washer, 0, 0, -thickness / 2 - 0.001).rotation.y = Math.PI;

  const face = new THREE.PlaneGeometry(TAG_WIDTH, TAG_HEIGHT);
  const centre = TAG_TOP - TAG_HEIGHT / 2;
  add(face, printed(front), 0, centre, thickness / 2 + 0.002).castShadow = false;
  const reverse = add(face, printed(back), 0, centre, -thickness / 2 - 0.002);
  reverse.rotation.y = Math.PI;
  reverse.castShadow = false;
  return group;
}

/** A shelf-edge label: a white card with one shop's price. */
export function buildShelfLabel(m, texture) {
  const group = new THREE.Group();
  const add = adder(group);
  add(new THREE.RoundedBoxGeometry(1.32, 0.62, 0.03, 3, 0.03), m.card);
  add(new THREE.PlaneGeometry(1.32, 0.62), printed(texture), 0, 0, 0.0165).castShadow = false;
  return group;
}
