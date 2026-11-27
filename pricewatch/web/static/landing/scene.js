/* The 3D layer of the home page: products floating between the coloured sheets and the text,
   each with a price tag swinging on its string. Scrolling moves the camera's subject from one
   product to the next; the pointer tilts the whole stage. */
import * as THREE from './three.js';
import { ChartScreen, fontsReady, phoneScreen, shelfLabel, tagFace } from './labels.js';
import {
  buildGamepad, buildGpu, buildHeadphones, buildPhone, buildShelfLabel, buildTag, buildTv, makeMaterials,
} from './models.js';
import { Rope } from './rope.js';
import { ACTORS, SHEET_COUNT, sampleStage } from './stage.js';

const FIELD_OF_VIEW = 32;
const CLOSEST = 14; // camera distance when the window is wide enough
const WIDTH_TO_FIT = { wide: 13.6, narrow: 7.4 }; // how much of the stage must be visible across
const SIDE_SHIFT = 0.235; // of the visible width: objects sit in the half the text leaves free
const NARROW_LIFT = 0.25; // of the visible height: objects above the text
const NARROW_ZOOM = 0.82; // the whole stage a little smaller, to leave the lower half to the words
const WALL_Z = -3; // the sheet of paper the shadows fall on
/* After a pause (a slow frame, a tab coming back) the scene catches up on what it missed, but
   never more than this: beyond it, it simply resumes. */
const LONGEST_CATCH_UP = 0.5;
const ARRIVED = 10; // well past the end of the entrance

/* Drawing quality, best first. The scene steps down when frames take too long, and gives the
   page back to the poster if even the last level cannot keep up. */
const QUALITY = [
  { pixelRatio: 2, shadowMap: 1024 },
  { pixelRatio: 1.5, shadowMap: 512 },
  { pixelRatio: 1, shadowMap: 0 },
];
const SLOW_FRAME = 1 / 36; // seconds: under about 36 pictures a second, try a lower quality
const HOPELESS_FRAME = 1 / 10;
const PATIENCE = 90; // frames in a row before acting
const CSS_COLOURS = {
  cobalt: '--cobalt', ink: '--ink', paper: '--paper', manila: '--manila', manilaDeep: '--manila-deep',
  deal: '--deal-bright', dealInk: '--deal-ink',
};
const OBJECT_COLOURS = {
  bone: '#ece6d8', graphite: '#262932', pit: '#0d0e13', metal: '#c2c5cc', gold: '#d6a94c', board: '#1c2a27',
  screen: '#0e1226',
};

const TAGS = {
  gpu: {
    length: 1.0,
    size: 1.22,
    front: { name: 'RTX 5070 Ti', shop: 'LDLC', euros: '899', cents: '00', note: 'usual 905 €' },
    back: { name: 'RTX 5070 Ti', shop: 'Joybuy', euros: '849', cents: '99', note: '6 % under usual', sticker: '−6 %' },
  },
  tv: { length: 0.72, worn: 'tvTag', front: { name: 'OLED55C5', shop: 'Idealo', euros: '1249', cents: '00', note: 'usual 1 290 €' } },
  pad: { length: 0.62, front: { name: 'DualSense', shop: 'Grosbill', euros: '59', cents: '99', note: 'usual 64 €' } },
  cans: { length: 0.68, front: { name: 'WH-1000XM6', shop: 'Materiel.net', euros: '329', cents: '00', note: 'usual 349 €' } },
};

/* What the shops ask for the card on the hero tag. The cheapest is out of stock, so the label
   that wins is the cheapest one you could actually buy. */
const OFFERS = [
  { shop: 'LDLC', euros: '899', cents: '00', stock: 'In stock' },
  { shop: 'TopAchat', euros: '879', cents: '90', stock: 'In stock', best: true },
  { shop: 'Materiel.net', euros: '899', cents: '95', stock: 'In stock' },
  { shop: 'Joybuy', euros: '869', cents: '00', stock: 'Out of stock' },
  { shop: 'Grosbill', euros: '909', cents: '00', stock: 'In stock' },
  { shop: 'Idealo', euros: '885', cents: '00', stock: 'In stock' },
  { shop: 'Alternate', euros: '915', cents: '90', stock: 'In stock' },
  { shop: 'Dealabs', euros: '889', cents: '00', stock: 'Posted today' },
];

/* Where the shop labels sit around the card, in degrees from its right-hand side. The gap at
   the lower right is where the card's own tag hangs. */
const LABEL_SLOTS = [352, 28, 66, 106, 146, 186, 226, 264];
const LABEL_REACH = { x: 2.42, y: 2.3 };

const DOWN = new THREE.Vector3(0, -1, 0);
const UP = new THREE.Vector3(0, 1, 0);

const clamp01 = (value) => Math.min(Math.max(value, 0), 1);
const easeOutExpo = (t) => (t >= 1 ? 1 : 1 - 2 ** (-10 * t));
const mix = (a, b, t) => a + (b - a) * t;

/* The palette lives in the stylesheet. Painting one pixel with each colour gives the browser's
   own conversion to sRGB, whatever notation the stylesheet uses. */
function readColours() {
  const styles = getComputedStyle(document.documentElement);
  const probe = document.createElement('canvas');
  probe.width = 1;
  probe.height = 1;
  const ctx = probe.getContext('2d', { willReadFrequently: true });
  const css = { ...OBJECT_COLOURS };
  for (const [name, property] of Object.entries(CSS_COLOURS)) {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = styles.getPropertyValue(property).trim();
    ctx.fillRect(0, 0, 1, 1);
    const [red, green, blue] = ctx.getImageData(0, 0, 1, 1).data;
    css[name] = `rgb(${red}, ${green}, ${blue})`;
  }
  const solid = Object.fromEntries(Object.entries(css).map(([name, value]) => [name, new THREE.Color(value)]));
  return { css, solid };
}

/* The soft reflections every material picks up. Baked from a small room that is thrown away
   afterwards, and baked again if the browser loses and restores its graphics context. */
function bakeEnvironment(scene, renderer) {
  const baker = new THREE.PMREMGenerator(renderer);
  const room = new THREE.RoomEnvironment();
  const before = scene.environment;
  scene.environment = baker.fromScene(room, 0.03).texture;
  scene.environmentIntensity = 0.5;
  if (before) before.dispose();
  room.dispose();
  baker.dispose();
}

function lightStage(scene, colours) {
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a93b8, 0.45));

  const key = new THREE.DirectionalLight(0xfff3e2, 2.7);
  key.position.set(-3.6, 5.2, 12);
  key.castShadow = true;
  key.shadow.mapSize.set(QUALITY[0].shadowMap, QUALITY[0].shadowMap);
  key.shadow.camera.left = -13;
  key.shadow.camera.right = 13;
  key.shadow.camera.top = 10;
  key.shadow.camera.bottom = -10;
  key.shadow.camera.near = 1;
  key.shadow.camera.far = 40;
  key.shadow.radius = 5;
  key.shadow.blurSamples = 10;
  key.shadow.bias = -0.0004;
  scene.add(key);

  const rim = new THREE.DirectionalLight(0xc4d2ff, 1.3);
  rim.position.set(7, 2.5, -5);
  scene.add(rim);

  // The shadows land on an invisible wall: whatever sheet is behind the canvas shows through.
  const wall = new THREE.Mesh(
    new THREE.PlaneGeometry(90, 70),
    new THREE.ShadowMaterial({ color: colours.solid.ink, opacity: 0.2 }),
  );
  wall.position.z = WALL_Z;
  wall.receiveShadow = true;
  scene.add(wall);
  return { key, wall };
}

export async function startScene({ canvas, position, sheets, reducedMotion, narrowLayout, insist = false }) {
  if (sheets !== SHEET_COUNT) throw new Error(`The page has ${sheets} sheets, the scene is staged for ${SHEET_COUNT}.`);
  const fontsInTime = await fontsReady();
  const colours = readColours();

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setClearColor(0x000000, 0);
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.VSMShadowMap;
  const anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(FIELD_OF_VIEW, 1, 0.5, 80);
  bakeEnvironment(scene, renderer);
  const lighting = lightStage(scene, colours);

  const printed = []; // every printed texture, to print again if the typeface arrives late
  const print = (texture) => { printed.push(texture); return texture; };
  const materials = makeMaterials(colours.solid);
  const chart = new ChartScreen(colours.css, anisotropy);
  const actors = {
    gpu: buildGpu(materials),
    tv: buildTv(materials, chart.texture),
    pad: buildGamepad(materials),
    cans: buildHeadphones(materials),
    phone: buildPhone(materials, print(phoneScreen(colours.css, anisotropy))),
  };
  const stage = new THREE.Group();
  ACTORS.forEach((name) => stage.add(actors[name].group));
  scene.add(stage);

  const hanging = Object.entries(TAGS).map(([name, spec], index) => {
    const front = print(tagFace(spec.front, colours.css, anisotropy));
    const back = spec.back ? print(tagFace(spec.back, colours.css, anisotropy)) : front;
    const tag = buildTag(materials, front, back);
    const rope = new Rope({ length: spec.length, material: materials.string });
    scene.add(tag, rope.mesh);
    return { actor: actors[name], tag, rope, worn: spec.worn, size: spec.size || 1, flips: Boolean(spec.back), phase: index * 2.1 };
  });

  const labels = OFFERS.map((offer) => {
    const label = buildShelfLabel(materials, print(shelfLabel(offer, colours.css, anisotropy)));
    label.visible = false;
    scene.add(label);
    return label;
  });

  const view = { width: 1, height: 1, wide: true, zoom: 1 };
  const pointer = { x: 0, y: 0, lookX: 0, lookY: 0, pushX: 0, pushY: 0 };
  const motion = { time: 0, arrived: 0, at: position(), fanSpeed: 2.4, fanAngle: 0, scrollY: window.scrollY, drawn: false };
  const anchor = new THREE.Vector3();
  const pull = new THREE.Vector3();
  const centre = new THREE.Vector3();
  const wind = new THREE.Vector3();
  const hang = new THREE.Quaternion();
  const twist = new THREE.Quaternion();
  const pace = { level: 0, slow: 0, hopeless: 0 };
  let needsFrame = true;
  let frame = 0;
  let stopped = false; // for good, on this machine
  let lost = false; // the browser took the graphics context away, and may give it back
  let previous = performance.now();

  function fit() {
    const width = canvas.clientWidth || window.innerWidth || 1;
    const height = canvas.clientHeight || window.innerHeight || 1;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, QUALITY[pace.level].pixelRatio));
    renderer.setSize(width, height, false);
    view.wide = !narrowLayout.matches;
    view.zoom = view.wide ? 1 : NARROW_ZOOM;
    camera.aspect = width / height;
    const spread = 2 * Math.tan(THREE.MathUtils.degToRad(FIELD_OF_VIEW / 2));
    const distance = Math.max(CLOSEST, (view.wide ? WIDTH_TO_FIT.wide : WIDTH_TO_FIT.narrow) / (spread * camera.aspect));
    camera.position.z = distance;
    camera.updateProjectionMatrix();
    view.height = spread * distance;
    view.width = view.height * camera.aspect;
    needsFrame = true;
  }

  function applyQuality() {
    const { shadowMap } = QUALITY[pace.level];
    const { key, wall } = lighting;
    key.castShadow = shadowMap > 0;
    wall.visible = shadowMap > 0;
    if (shadowMap > 0 && key.shadow.mapSize.x !== shadowMap) {
      key.shadow.dispose(); // the map and its blur pass: both are rebuilt at the new size on the next frame
      key.shadow.map = null;
      key.shadow.mapPass = null;
      key.shadow.mapSize.set(shadowMap, shadowMap);
    }
    fit();
  }

  function placeActor(name, index, sample, still) {
    const { group } = actors[name];
    const target = sample[name];
    const arrived = still ? 1 : easeOutExpo(clamp01(motion.arrived - index * 0.12));
    const late = 1 - arrived;
    const sway = still ? 0 : 1;
    const phase = index * 1.7;
    const t = motion.time;
    group.position.set(
      target.position[0],
      target.position[1] + sway * Math.sin(t * 0.9 + phase) * 0.09 + late * 1.4,
      target.position[2] - late * 8,
    );
    group.rotation.set(
      target.rotation[0] + sway * Math.sin(t * 0.6 + phase) * 0.045,
      target.rotation[1] + sway * Math.sin(t * 0.42 + phase * 1.3) * 0.07 - late * 0.9,
      target.rotation[2] + sway * Math.cos(t * 0.5 + phase) * 0.02,
    );
    const scale = target.scale * arrived;
    group.scale.setScalar(Math.max(scale, 1e-4));
    group.visible = scale > 0.01;
  }

  function hangTags(seconds, sample, still, fresh) {
    for (const item of hanging) {
      const size = item.actor.group.scale.x * view.zoom * (item.worn ? sample[item.worn] : 1);
      item.actor.group.localToWorld(anchor.copy(item.actor.anchor));
      item.rope.resize(size);
      if (fresh) item.rope.reset(anchor);
      if (still) item.rope.settle(anchor);
      else item.rope.advance(anchor, seconds, wind);

      item.rope.end(item.tag.position);
      hang.setFromUnitVectors(DOWN, item.rope.pull(pull));
      const swing = still ? 0 : Math.sin(motion.time * 0.7 + item.phase) * 0.22;
      twist.setFromAxisAngle(UP, swing + (item.flips ? sample.flip * Math.PI : 0));
      item.tag.quaternion.copy(hang).multiply(twist);
      item.tag.scale.setScalar(Math.max(size * item.size, 1e-4));
      item.tag.visible = item.actor.group.visible && size > 0.02;
      item.rope.mesh.visible = item.tag.visible;
    }
  }

  function placeLabels(sample, still) {
    actors.gpu.group.getWorldPosition(centre);
    labels.forEach((label, index) => {
      const shown = easeOutExpo(clamp01(sample.shops * 1.9 - index * 0.11));
      label.visible = shown > 0.01;
      if (!label.visible) return;
      const sway = still ? 0 : Math.sin(motion.time * 0.5 + index * 1.9) * 0.05;
      const angle = THREE.MathUtils.degToRad(LABEL_SLOTS[index]) + sway;
      const reach = mix(3.4, 1, shown); // they come in from outside the frame
      label.position.set(
        centre.x + Math.cos(angle) * LABEL_REACH.x * reach * view.zoom,
        centre.y + Math.sin(angle) * LABEL_REACH.y * reach * view.zoom,
        centre.z + (0.2 + Math.sin(angle * 2 + 1) * 0.45) * view.zoom,
      );
      label.rotation.set(Math.sin(angle) * -0.1, Math.cos(angle) * -0.3, Math.sin(angle * 1.5) * 0.07);
      label.scale.setScalar(shown * 0.9 * view.zoom);
    });
  }

  /* Moves everything forward by ``seconds``, in one go: every easing below is exact for any
     length of time, and the strings cut the time into their own small steps. Returns whether
     there is something new to draw. */
  function advance(seconds) {
    const still = reducedMotion.matches;
    const target = position();
    const scrollSpeed = seconds > 0 ? (window.scrollY - motion.scrollY) / seconds : 0;
    motion.scrollY = window.scrollY;

    if (still) {
      const snapped = Math.round(target); // a cut from one sheet's picture to the next, no travel
      if (!needsFrame && snapped === motion.at) return false;
      motion.at = snapped;
      motion.arrived = ARRIVED; // if motion is allowed again later, nothing replays its entrance
    } else {
      motion.time += seconds;
      motion.arrived += seconds * 0.9;
      motion.at += (target - motion.at) * (1 - Math.exp(-seconds * 5.5));
      pointer.lookX += (pointer.x - pointer.lookX) * (1 - Math.exp(-seconds * 3.5));
      pointer.lookY += (pointer.y - pointer.lookY) * (1 - Math.exp(-seconds * 3.5));
    }
    const fresh = !motion.drawn;
    motion.drawn = true;
    needsFrame = false;

    const sample = sampleStage(motion.at, view.wide);
    stage.position.set(
      view.wide ? sample.side * view.width * SIDE_SHIFT : 0,
      (view.wide ? sample.lift : NARROW_LIFT) * view.height,
      0,
    );
    stage.rotation.set(-pointer.lookY * 0.07, pointer.lookX * 0.13, 0);
    stage.scale.setScalar(view.zoom);
    camera.position.x = pointer.lookX * 0.55;
    camera.position.y = pointer.lookY * 0.32;
    camera.lookAt(0, 0, 0);

    ACTORS.forEach((name, index) => placeActor(name, index, sample, still));
    stage.updateMatrixWorld(true);

    motion.fanSpeed += (sample.spin - motion.fanSpeed) * (1 - Math.exp(-seconds * 2.5));
    if (!still) motion.fanAngle -= motion.fanSpeed * seconds * 1.7;
    actors.gpu.fans.forEach((fan, index) => { fan.rotation.z = motion.fanAngle + index * 0.7; });

    // Moving the pointer, or the page, stirs the air around the tags; the gust dies down in a moment.
    wind.set(pointer.pushX * 26, pointer.pushY * 14 + scrollSpeed * 0.0008, 0);
    const calm = Math.exp(-seconds * 9);
    pointer.pushX *= calm;
    pointer.pushY *= calm;
    hangTags(seconds, sample, still, fresh);
    placeLabels(sample, still);
    chart.draw(sample.chart);
    materials.string.color.lerpColors(colours.solid.paper, colours.solid.ink, sample.light);
    return true;
  }

  function onPointerMove(event) {
    const x = (event.clientX / window.innerWidth) * 2 - 1;
    const y = -((event.clientY / window.innerHeight) * 2 - 1);
    pointer.pushX += x - pointer.x;
    pointer.pushY += y - pointer.y;
    pointer.x = x;
    pointer.y = y;
  }

  function askForFrame() {
    needsFrame = true;
  }

  /* Returns false when the scene should stop for good on this machine. */
  function keepsUp(frameSeconds) {
    pace.slow = frameSeconds > SLOW_FRAME ? pace.slow + 1 : 0;
    pace.hopeless = frameSeconds > HOPELESS_FRAME ? pace.hopeless + 1 : 0;
    if (pace.slow >= PATIENCE && pace.level < QUALITY.length - 1) {
      pace.level += 1;
      pace.slow = 0;
      pace.hopeless = 0;
      applyQuality();
    }
    return insist || pace.hopeless < PATIENCE;
  }

  function tick(now) {
    frame = window.requestAnimationFrame(tick);
    const elapsed = Math.max(0, (now - previous) / 1000);
    previous = now;
    if (!advance(Math.min(elapsed, LONGEST_CATCH_UP))) return;
    renderer.render(scene, camera);
    if (!keepsUp(elapsed)) shutDown();
  }

  function run() {
    window.cancelAnimationFrame(frame);
    if (document.hidden || stopped || lost) return;
    previous = performance.now();
    frame = window.requestAnimationFrame(tick);
  }

  function onContextLost(event) {
    event.preventDefault(); // tells the browser we can take the context back
    lost = true;
    window.cancelAnimationFrame(frame);
    document.documentElement.classList.remove('has-scene'); // the poster stands in meanwhile
  }

  function onContextRestored() {
    if (stopped) return;
    lost = false;
    bakeEnvironment(scene, renderer); // what was baked on the graphics card went with the old context
    needsFrame = true;
    document.documentElement.classList.add('has-scene');
    run();
  }

  const resizes = new ResizeObserver(fit);

  /* This machine cannot keep up: hand the page back to the poster and let go of everything. */
  function shutDown() {
    stopped = true;
    window.cancelAnimationFrame(frame);
    document.documentElement.classList.remove('has-scene');
    resizes.disconnect();
    window.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('visibilitychange', run);
    reducedMotion.removeEventListener('change', askForFrame);
    canvas.removeEventListener('webglcontextlost', onContextLost);
    canvas.removeEventListener('webglcontextrestored', onContextRestored);
    scene.traverse((object) => { if (object.geometry) object.geometry.dispose(); });
    Object.values(materials).forEach((material) => material.dispose());
    printed.forEach((texture) => texture.dispose());
    chart.texture.dispose();
    scene.environment.dispose();
    lighting.key.shadow.dispose();
    renderer.dispose();
    renderer.forceContextLoss();
  }

  resizes.observe(canvas);
  window.addEventListener('pointermove', onPointerMove, { passive: true });
  document.addEventListener('visibilitychange', run);
  reducedMotion.addEventListener('change', askForFrame);
  canvas.addEventListener('webglcontextlost', onContextLost);
  canvas.addEventListener('webglcontextrestored', onContextRestored);
  if (!fontsInTime) {
    document.fonts.ready.then(() => {
      if (stopped) return;
      printed.forEach((texture) => texture.userData.print());
      chart.print();
      needsFrame = true;
    });
  }

  applyQuality();
  advance(0);
  renderer.render(scene, camera);
  run();
}
