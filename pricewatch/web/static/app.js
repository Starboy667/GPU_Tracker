/* Price Watch: small progressive enhancements. Every page works without this file. */
(function () {
  "use strict";

  // Buttons that start something slow say so and cannot be pressed twice.
  document.addEventListener("submit", function (event) {
    var button = event.submitter;
    if (!button || !button.dataset.busy) return;
    // Wait a tick: a disabled button would not submit its own formaction.
    window.setTimeout(function () {
      button.disabled = true;
      button.textContent = button.dataset.busy;
    }, 0);
  });

  // Close an open inline confirmation when clicking elsewhere or pressing Escape.
  function closeConfirms(except) {
    document.querySelectorAll("details.confirm[open]").forEach(function (details) {
      if (details !== except) details.open = false;
    });
  }
  document.addEventListener("click", function (event) {
    closeConfirms(event.target.closest ? event.target.closest("details.confirm") : null);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeConfirms(null);
  });

  // Watch form: the channel checkboxes only matter in "Only these" mode.
  document.querySelectorAll("[data-channel-picker]").forEach(function (picker) {
    var list = picker.querySelector("[data-channel-list]");
    if (!list) return;
    function sync() {
      var selected = picker.querySelector('input[name="channels_mode"]:checked');
      var off = !selected || selected.value !== "selected";
      list.querySelectorAll("input").forEach(function (input) { input.disabled = off; });
    }
    picker.addEventListener("change", sync);
    sync();
  });

  // Line charts: a crosshair that snaps to the nearest day, with the value in a tooltip.
  // The same values are in the table under the chart, so nothing depends on hovering.
  document.querySelectorAll("[data-chart-root]").forEach(function (root) {
    var tip = root.querySelector("[data-tip]");
    var tipValue = root.querySelector("[data-tip-value]");
    var tipLabel = root.querySelector("[data-tip-label]");
    if (!tip) return;

    root.querySelectorAll("svg[data-chart]").forEach(function (svg) {
      var points;
      try { points = JSON.parse(svg.dataset.points || "[]"); } catch (error) { points = []; }
      var hover = svg.querySelector("[data-hover]");
      var hit = svg.querySelector("[data-hit]");
      if (!points.length || !hover || !hit) return;
      var line = hover.querySelector("line");
      var dot = hover.querySelector("circle");
      var current = points.length - 1;

      function show(index) {
        current = Math.max(0, Math.min(points.length - 1, index));
        var point = points[current];
        line.setAttribute("x1", point.x);
        line.setAttribute("x2", point.x);
        dot.setAttribute("cx", point.x);
        dot.setAttribute("cy", point.y);
        hover.removeAttribute("hidden");
        tipValue.textContent = point.v;
        tipLabel.textContent = point.d;
        tip.hidden = false;

        var box = svg.getBoundingClientRect();
        var rootBox = root.getBoundingClientRect();
        var scale = box.width / svg.viewBox.baseVal.width;
        var left = box.left - rootBox.left + point.x * scale;
        var top = box.top - rootBox.top + point.y * scale;
        var width = tip.offsetWidth;
        var x = left + 12;
        if (x + width > rootBox.width - 4) x = left - width - 12;
        tip.style.left = Math.max(4, x) + "px";
        tip.style.top = Math.max(4, top - tip.offsetHeight - 10) + "px";
      }

      function hide() {
        hover.setAttribute("hidden", "");
        tip.hidden = true;
      }

      function nearest(clientX) {
        var box = svg.getBoundingClientRect();
        var x = (clientX - box.left) / box.width * svg.viewBox.baseVal.width;
        var best = 0;
        for (var i = 1; i < points.length; i += 1) {
          if (Math.abs(points[i].x - x) < Math.abs(points[best].x - x)) best = i;
        }
        return best;
      }

      hit.addEventListener("pointermove", function (event) { show(nearest(event.clientX)); });
      hit.addEventListener("pointerdown", function (event) { show(nearest(event.clientX)); });
      hit.addEventListener("pointerleave", function () { if (document.activeElement !== hit) hide(); });
      hit.addEventListener("focus", function () { show(current); });
      hit.addEventListener("blur", hide);
      hit.addEventListener("keydown", function (event) {
        var step = { ArrowLeft: -1, ArrowRight: 1, Home: -points.length, End: points.length }[event.key];
        if (!step) return;
        event.preventDefault();
        show(current + step);
      });
    });
  });
})();
