/* Where everything is on each sheet of the page, and how to get from one sheet to the next.
   One entry per sheet, in page order. Positions are in the stage's own space: the stage as a
   whole is then pushed to the free side of the text (see ``side``). */

const pose = (position, rotation, scale) => ({ position, rotation, scale });

/* side: where the objects go on wide screens. 1 right of the text, -1 left of it, 0 centred.
   lift: vertical shift, as a fraction of the visible height.
   shops, chart, flip: 0 to 1, how far the shop labels, the price curve and the tag flip are.
   spin: fan speed, in turns of the dial rather than physics. light: 1 for a light sheet.
   tvTag: whether the TV wears its tag (where it would only hide behind something, it does not). */
const SHEETS = [
  { // hero: the whole shelf, loosely
    side: 1, lift: 0, shops: 0, chart: 1, flip: 0, spin: 2.4, light: 0, tvTag: 0,
    gpu: pose([-0.2, -0.45, 0.9], [-0.4, -0.55, 0.16], 1),
    tv: pose([1.15, 2.0, -1.8], [0.04, -0.42, 0.03], 0.9),
    pad: pose([-1.85, -2.2, 0.5], [0.55, 0.5, -0.3], 0.76),
    cans: pose([-2.05, 1.95, -1.2], [0.2, 0.7, 0.2], 0.8),
    phone: pose([5.5, -2.5, -2], [0, -0.6, 0.2], 0),
  },
  { // the rounds: the card in the middle, every shop's label around it
    side: -0.93, lift: 0, shops: 1, chart: 0, flip: 0, spin: 3.4, light: 1, tvTag: 0,
    gpu: pose([0, 0.15, 0.6], [-0.14, 0.26, 0.04], 1.02),
    tv: pose([0.5, 4.0, -5], [0.1, -0.3, 0.05], 0.55),
    pad: pose([-3.5, -3.5, -4], [0.6, 0.8, -0.2], 0.5),
    cans: pose([-3.6, 3.7, -5], [0.3, 0.9, 0.3], 0.5),
    phone: pose([5.5, -2.5, -2], [0, -0.6, 0.2], 0),
  },
  { // the usual price: the TV comes forward and draws the curve
    side: 1, lift: 0.02, shops: 0, chart: 1, flip: 0, spin: 1.6, light: 0, tvTag: 1,
    gpu: pose([-2.2, 3.15, -3], [-0.3, -0.6, 0.22], 0.5),
    tv: pose([0, 0.25, 0.9], [0.02, -0.24, 0], 1.3),
    pad: pose([2.7, -3.0, -2.2], [0.5, 0.2, 0.3], 0.48),
    cans: pose([3.1, 3.3, -3], [0.2, 0.3, -0.2], 0.48),
    phone: pose([5.5, -1.5, -2], [0, -0.6, 0.2], 0),
  },
  { // the alert: the tag turns over, the phone lights up
    side: -1, lift: 0.03, shops: 0, chart: 1, flip: 1, spin: 9, light: 1, tvTag: 0,
    gpu: pose([-0.75, 0.75, 0.5], [-0.3, 0.46, -0.08], 0.98),
    tv: pose([-2.9, 3.4, -5], [0.1, 0.5, 0], 0.5),
    pad: pose([0.7, 4.0, -5], [0.4, -0.4, 0.3], 0.48),
    cans: pose([-3.2, -3.2, -4.5], [0.2, 0.9, 0.2], 0.48),
    phone: pose([1.85, -0.75, 1.7], [-0.12, -0.5, 0.1], 1),
  },
  { // sharing: someone else's things
    side: 1, lift: 0, shops: 0, chart: 1, flip: 1, spin: 2, light: 1, tvTag: 0,
    gpu: pose([2.7, 3.1, -3.4], [-0.3, -0.5, 0.2], 0.52),
    tv: pose([-2.3, -3.3, -4], [0, 0.4, 0.05], 0.5),
    pad: pose([1.05, -0.55, 1.0], [0.36, -0.3, 0.1], 1.08),
    cans: pose([-1.25, 1.5, 0.2], [0.1, 0.45, -0.08], 1.02),
    phone: pose([4.5, 1, -3], [0, -0.6, 0.2], 0),
  },
  { // the door: everything lined up, waiting
    side: 0, lift: -0.17, shops: 0, chart: 1, flip: 1, spin: 2.4, light: 0, tvTag: 1,
    gpu: pose([-1.55, 0.1, 0], [-0.22, 0.3, 0.02], 0.78),
    tv: pose([1.9, 0.35, -0.4], [0, -0.26, 0], 0.72),
    pad: pose([4.65, -0.15, 0.2], [0.32, -0.36, 0.06], 0.74),
    cans: pose([-4.55, 0.2, 0.1], [0.08, 0.5, 0.04], 0.78),
    phone: pose([6.5, -2, -3], [0, -0.6, 0.2], 0),
  },
];

/* On narrow screens there is no room for a row: the last sheet stacks the shelf two by two. */
const NARROW_DOOR = {
  gpu: pose([-0.2, 1.5, 0.2], [-0.22, 0.3, 0.02], 0.9),
  tv: pose([0.3, -1.9, -0.4], [0, -0.26, 0], 0.8),
  pad: pose([2.3, 0.2, 0.6], [0.32, -0.36, 0.06], 0.66),
  cans: pose([-2.4, -0.1, 0.4], [0.08, 0.5, 0.04], 0.7),
};

export const ACTORS = ['gpu', 'tv', 'pad', 'cans', 'phone'];
export const SHEET_COUNT = SHEETS.length;

const mix = (a, b, t) => a + (b - a) * t;
const mixList = (a, b, t) => a.map((value, i) => mix(value, b[i], t));

/* Each sheet holds still around its centre and hands over to the next in between, starting
   slowly and landing slowly. */
function handover(t) {
  const x = Math.min(Math.max((t - 0.16) / 0.68, 0), 1);
  return x * x * x * (x * (x * 6 - 15) + 10);
}

function sheet(index, wide) {
  const base = SHEETS[index];
  return !wide && index === SHEET_COUNT - 1 ? { ...base, ...NARROW_DOOR } : base;
}

/** The scene at ``position`` (0 = first sheet, 1.5 = halfway from the second to the third). */
export function sampleStage(position, wide) {
  const clamped = Math.min(Math.max(position, 0), SHEET_COUNT - 1);
  const index = Math.min(Math.floor(clamped), SHEET_COUNT - 2);
  const from = sheet(index, wide);
  const to = sheet(index + 1, wide);
  const t = handover(clamped - index);
  const sample = {};
  for (const key of ['side', 'lift', 'shops', 'chart', 'flip', 'spin', 'light', 'tvTag']) sample[key] = mix(from[key], to[key], t);
  for (const actor of ACTORS) {
    sample[actor] = {
      position: mixList(from[actor].position, to[actor].position, t),
      rotation: mixList(from[actor].rotation, to[actor].rotation, t),
      scale: mix(from[actor].scale, to[actor].scale, t),
    };
  }
  return sample;
}
