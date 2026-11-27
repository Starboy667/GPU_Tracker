/* Everything printed in the scene, drawn on 2D canvases and used as textures: price tags,
   shelf labels, the TV's price chart, the phone's notification. Same typeface as the page. */
import * as THREE from './three.js';

const FAMILY = 'Archivo, system-ui, sans-serif';
const NARROW = 'extra-condensed';
const TIGHT = 'condensed';
const NBSP = String.fromCharCode(160); // keeps a number and its unit on one line

const font = (weight, pixels, stretch = 'normal') => `${weight} ${stretch} ${pixels}px ${FAMILY}`;

/** The canvases need the web font. Waits for it, but never holds the scene hostage: answers
    false when it gave up, and the caller prints everything again once the font is there. */
export async function fontsReady() {
  if (!document.fonts || !document.fonts.load) return true;
  const gaveUp = new Promise((resolve) => { window.setTimeout(() => resolve(false), 2500); });
  const loaded = Promise.all([
    document.fonts.load(font(900, 64, NARROW), '0123456789'),
    document.fonts.load(font(500, 32), 'Price Watch'),
  ]).then(() => true, () => true); // a failed download will not get better by waiting
  return Promise.race([loaded, gaveUp]);
}

function sheet(width, height) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  return [canvas, canvas.getContext('2d')];
}

function asTexture(canvas, anisotropy) {
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = anisotropy;
  return texture;
}

/* A texture drawn by ``draw``, which it can run again (texture.userData.print) on a clean sheet. */
function printed(width, height, anisotropy, draw) {
  const [canvas, ctx] = sheet(width, height);
  const texture = asTexture(canvas, anisotropy);
  texture.userData.print = () => {
    ctx.save();
    ctx.clearRect(0, 0, width, height);
    draw(ctx);
    ctx.restore();
    texture.needsUpdate = true;
  };
  texture.userData.print();
  return texture;
}

function roundedRect(ctx, x, y, width, height, radius) {
  ctx.beginPath();
  ctx.roundRect(x, y, width, height, radius);
}

/* A price the way shop labels print it: the euros large and narrow, the cents raised beside
   them, the currency sign on the baseline. Returns the width it took. */
function drawPrice(ctx, { euros, cents, x, baseline, size, colour, align = 'left' }) {
  const small = size * 0.4;
  const gap = size * 0.035;
  ctx.font = font(900, size, NARROW);
  const eurosWidth = ctx.measureText(euros).width;
  ctx.font = font(800, small, TIGHT);
  const sideWidth = Math.max(ctx.measureText(`,${cents}`).width, ctx.measureText('€').width);
  const width = eurosWidth + gap + sideWidth;
  const left = align === 'right' ? x - width : align === 'center' ? x - width / 2 : x;

  ctx.fillStyle = colour;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  ctx.font = font(900, size, NARROW);
  ctx.fillText(euros, left, baseline);
  ctx.font = font(800, small, TIGHT);
  const capHeight = 0.7; // of the font size, for Archivo's figures
  ctx.fillText(`,${cents}`, left + eurosWidth + gap, baseline - size * capHeight + small * capHeight);
  ctx.fillText('€', left + eurosWidth + gap + small * 0.06, baseline);
  return width;
}

/** One face of a hanging price tag. The top is left blank: that is where the hole is. */
export function tagFace({ name, shop, euros, cents, note, sticker = '' }, colours, anisotropy) {
  return printed(512, 768, anisotropy, (ctx) => {
    const margin = 50;
    ctx.fillStyle = colours.ink;
    ctx.textBaseline = 'alphabetic';
    ctx.font = font(800, 50, TIGHT);
    ctx.fillText(name.toUpperCase(), margin, 232, 512 - margin * 2);
    ctx.globalAlpha = 0.72;
    ctx.font = font(500, 33);
    ctx.fillText(shop, margin, 278);
    ctx.globalAlpha = 0.34;
    ctx.fillRect(margin, 306, 512 - margin * 2, 3);
    ctx.globalAlpha = 1;

    const size = euros.length > 3 ? 196 : 250;
    drawPrice(ctx, { euros, cents, x: margin - 6, baseline: 540, size, colour: colours.ink });

    ctx.globalAlpha = 0.72;
    ctx.font = font(500, 33);
    ctx.fillText(note, margin, 632, 512 - margin * 2);
    ctx.globalAlpha = 1;

    if (sticker) {
      ctx.save();
      ctx.translate(384, 138);
      ctx.rotate(-0.2);
      ctx.fillStyle = colours.deal;
      ctx.beginPath();
      ctx.arc(0, 0, 96, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = colours.dealInk;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = font(900, 78, TIGHT);
      ctx.fillText(sticker, 0, 4);
      ctx.restore();
    }
  });
}

/** A shelf-edge label: one shop's offer for the product. */
export function shelfLabel({ shop, euros, cents, stock, best = false }, colours, anisotropy) {
  return printed(640, 300, anisotropy, (ctx) => {
    if (best) {
      ctx.fillStyle = colours.deal;
      ctx.fillRect(0, 232, 640, 68);
      ctx.fillStyle = colours.dealInk;
      ctx.font = font(800, 34, TIGHT);
      ctx.textBaseline = 'middle';
      ctx.fillText('CHEAPEST IN STOCK', 34, 268);
    }
    ctx.fillStyle = colours.ink;
    ctx.textBaseline = 'alphabetic';
    ctx.font = font(800, 46, TIGHT);
    ctx.fillText(shop, 34, 74, 300);
    if (!best) {
      ctx.globalAlpha = 0.7;
      ctx.font = font(500, 30);
      ctx.fillText(stock, 34, 262);
      ctx.globalAlpha = 1;
    }
    drawPrice(ctx, { euros, cents, x: 608, baseline: best ? 204 : 228, size: best ? 168 : 178, colour: colours.ink, align: 'right' });
  });
}

/* Thirty days of lowest prices for the product on the hero tag. The usual price is their
   median; the last day is the drop the page is about. */
const USUAL = 905;
const DROP = 849.99;
const DAILY_LOWS = [
  912, 909, 909, 915, 905, 905, 899, 905, 909, 912, 905, 899, 899, 905, 909,
  915, 919, 909, 905, 905, 899, 895, 899, 905, 905, 909, 899, 899, 889, DROP,
];
const CHART_LOW = 835;
const CHART_HIGH = 930;

/** The TV: the price curve draws itself as ``progress`` goes from 0 to 1. */
export class ChartScreen {
  constructor(colours, anisotropy) {
    [this.canvas, this.ctx] = sheet(1280, 720);
    this.colours = colours;
    this.texture = asTexture(this.canvas, anisotropy);
    this.drawnAt = -1;
    this.draw(1);
  }

  /** Draw the same picture again, from scratch (the typeface arrived late). */
  print() {
    const progress = Math.max(this.drawnAt, 0);
    this.drawnAt = -1;
    this.draw(progress);
  }

  draw(progress) {
    const clamped = Math.min(Math.max(progress, 0), 1);
    if (Math.abs(clamped - this.drawnAt) < 0.004) return;
    this.drawnAt = clamped;
    const { ctx, colours } = this;
    const left = 150;
    const right = 1180;
    const top = 170;
    const bottom = 600;
    const x = (day) => left + (day / (DAILY_LOWS.length - 1)) * (right - left);
    const y = (price) => bottom - ((price - CHART_LOW) / (CHART_HIGH - CHART_LOW)) * (bottom - top);

    ctx.fillStyle = colours.screen;
    ctx.fillRect(0, 0, 1280, 720);

    ctx.fillStyle = colours.paper;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.font = font(800, 46, TIGHT);
    ctx.fillText('RTX 5070 Ti', left, 92);
    ctx.globalAlpha = 0.62;
    ctx.font = font(450, 30);
    ctx.fillText('Lowest price in stock, per day', left, 132);

    ctx.font = font(500, 26);
    ctx.textAlign = 'right';
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = colours.paper;
    for (const price of [850, 875, 900, 925]) {
      ctx.globalAlpha = 0.12;
      ctx.beginPath();
      ctx.moveTo(left, y(price));
      ctx.lineTo(right, y(price));
      ctx.stroke();
      ctx.globalAlpha = 0.55;
      ctx.fillText(`${price}${NBSP}€`, left - 18, y(price) + 9);
    }

    // What it usually costs: the reference every alert is measured against.
    ctx.globalAlpha = 0.85;
    ctx.setLineDash([16, 12]);
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(left, y(USUAL));
    ctx.lineTo(right, y(USUAL));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    const reach = clamped * (DAILY_LOWS.length - 1);
    const whole = Math.floor(reach);
    const tip = { day: reach, price: DAILY_LOWS[whole] };
    if (whole < DAILY_LOWS.length - 1) tip.price += (DAILY_LOWS[whole + 1] - DAILY_LOWS[whole]) * (reach - whole);
    const dropped = tip.price < USUAL * 0.96;

    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.lineWidth = 8;
    ctx.strokeStyle = colours.paper;
    ctx.beginPath();
    ctx.moveTo(x(0), y(DAILY_LOWS[0]));
    for (let day = 1; day <= whole; day += 1) ctx.lineTo(x(day), y(DAILY_LOWS[day]));
    ctx.lineTo(x(tip.day), y(tip.price));
    ctx.stroke();

    ctx.fillStyle = dropped ? colours.deal : colours.paper;
    ctx.beginPath();
    ctx.arc(x(tip.day), y(tip.price), dropped ? 17 : 12, 0, Math.PI * 2);
    ctx.fill();

    // Captioned after the curve, on a patch of screen, so the line never runs through the words.
    const caption = `usual price ${USUAL}${NBSP}€`;
    ctx.font = font(650, 28);
    ctx.textAlign = 'left';
    const captionWidth = ctx.measureText(caption).width;
    ctx.fillStyle = colours.screen;
    ctx.fillRect(left + 28, y(USUAL) - 22, captionWidth + 28, 44);
    ctx.fillStyle = colours.paper;
    ctx.globalAlpha = 0.9;
    ctx.textBaseline = 'middle';
    ctx.fillText(caption, left + 42, y(USUAL) + 2);
    ctx.textBaseline = 'alphabetic';
    ctx.globalAlpha = 1;

    if (clamped > 0.985) {
      ctx.textAlign = 'right';
      ctx.fillStyle = colours.deal;
      roundedRect(ctx, right - 150, y(DROP) - 132, 150, 62, 12);
      ctx.fill();
      ctx.fillStyle = colours.dealInk;
      ctx.textBaseline = 'middle';
      ctx.font = font(900, 42, TIGHT);
      ctx.fillText(`−6${NBSP}%`, right - 22, y(DROP) - 99);
      ctx.textBaseline = 'alphabetic';
      ctx.fillStyle = colours.paper;
      ctx.font = font(700, 30);
      ctx.fillText(`849,99${NBSP}€ at Joybuy`, right - 168, y(DROP) - 90);
    }
    this.texture.needsUpdate = true;
  }
}

/** The phone: the alert as it lands. */
export function phoneScreen(colours, anisotropy) {
  return printed(540, 1110, anisotropy, (ctx) => {
    ctx.fillStyle = colours.cobalt;
    ctx.fillRect(0, 0, 540, 1110);

    const now = new Date(); // the visitor's own lock screen
    ctx.fillStyle = colours.paper;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'alphabetic';
    ctx.font = font(300, 156, TIGHT);
    ctx.fillText(now.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }), 270, 250);
    ctx.globalAlpha = 0.8;
    ctx.font = font(500, 32);
    ctx.fillText(now.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' }), 270, 304);
    ctx.globalAlpha = 1;

    const card = (top, height, alpha, lines) => {
      ctx.globalAlpha = alpha;
      ctx.fillStyle = colours.paper;
      roundedRect(ctx, 26, top, 488, height, 36);
      ctx.fill();
      ctx.textAlign = 'left';
      ctx.fillStyle = colours.ink;
      lines.forEach(([text, weight, size, offset, opacity]) => {
        ctx.globalAlpha = alpha * opacity;
        ctx.font = font(weight, size, weight >= 700 ? TIGHT : 'normal');
        ctx.fillText(text, 58, top + offset, 424);
      });
      ctx.globalAlpha = 1;
    };
    card(392, 318, 1, [
      ['PRICE WATCH · now', 700, 27, 60, 0.62],
      ['RTX 5070 Ti', 850, 52, 128, 1],
      ['849,99 € at Joybuy', 850, 52, 186, 1],
      ['In stock. 6 % under its usual price.', 480, 29, 250, 0.85],
    ]);
    ctx.fillStyle = colours.deal;
    ctx.beginPath();
    ctx.arc(462, 444, 15, 0, Math.PI * 2);
    ctx.fill();
    card(734, 190, 0.45, [
      ['PRICE WATCH · yesterday', 700, 27, 60, 0.62],
      ['Elden Ring is at 35,99 €', 800, 38, 124, 1],
    ]);
  });
}
