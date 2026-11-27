/* Price Watch home page. The page is complete without this file. It adds the reveals as the
   sheets scroll in and, where the browser can draw it, the 3D scene. */

const root = document.documentElement;
const folds = Array.from(document.querySelectorAll('[data-fold]'));
const canvas = document.querySelector('[data-scene]');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
// Word for word the last media query of landing.css: when it matches, the objects sit above the
// text instead of beside it, and the camera frames them that way.
const narrowLayout = window.matchMedia('(max-width: 59.99rem), (max-aspect-ratio: 5/4)');

root.classList.add('js');

function revealOnScroll() {
  const blocks = Array.from(document.querySelectorAll('[data-reveal]'));
  if (!('IntersectionObserver' in window)) {
    blocks.forEach((block) => block.classList.add('is-in'));
    return;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('is-in');
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.2 });
  blocks.forEach((block) => observer.observe(block));
}

/* Where the middle of the window is along the page, counted in sheets: 0 is the centre of the
   first one, 2.5 is halfway between the third and the fourth. */
function foldPosition() {
  const middle = window.scrollY + window.innerHeight / 2;
  let before = folds[0].offsetTop + folds[0].offsetHeight / 2;
  if (middle <= before) return 0;
  for (let index = 1; index < folds.length; index += 1) {
    const centre = folds[index].offsetTop + folds[index].offsetHeight / 2;
    if (middle <= centre) return index - 1 + (middle - before) / (centre - before);
    before = centre;
  }
  return folds.length - 1;
}

/* ?scene=off keeps the page flat; ?scene=on insists on 3D even where it would be skipped. */
const wish = new URLSearchParams(window.location.search).get('scene');
const SOFTWARE_RENDERER = /swiftshader|llvmpipe|softpipe|software|basic render/i;

function canDraw3d() {
  if (wish === 'off') return false;
  let context = null;
  try {
    context = document.createElement('canvas').getContext('webgl2');
  } catch (error) {
    return false;
  }
  if (!context) return false;
  const info = context.getExtension('WEBGL_debug_renderer_info');
  const renderer = info ? String(context.getParameter(info.UNMASKED_RENDERER_WEBGL)) : '';
  const release = context.getExtension('WEBGL_lose_context'); // browsers only allow a few contexts
  if (release) release.loseContext();
  if (wish === 'on') return true;
  if (navigator.connection && navigator.connection.saveData) return false; // they asked for less
  // Without a graphics chip the browser draws on the processor: a slideshow that blocks scrolling.
  return !SOFTWARE_RENDERER.test(renderer);
}

revealOnScroll();

if (canvas && folds.length && canDraw3d()) {
  import('./scene.js')
    .then(({ startScene }) => startScene({
      canvas, position: foldPosition, sheets: folds.length, reducedMotion, narrowLayout, insist: wish === 'on',
    }))
    .then(() => root.classList.add('has-scene'))
    .catch((error) => {
      // The page stays as it is, with its poster. Worth knowing about when developing.
      console.error('Price Watch: the 3D scene could not start.', error);
    });
}
