(function () {
  "use strict";
  var q = function (m) { return window.matchMedia && window.matchMedia(m).matches; };
  var hero = document.querySelector(".hero");
  var disc = document.querySelector(".disc-wrap");
  if (!hero || !disc || q("(prefers-reduced-motion: reduce)") || !q("(pointer: fine)")) return;
  var tx = 0, ty = 0, x = 0, y = 0, raf = 0;
  function tick() {
    x += (tx - x) * 0.08; y += (ty - y) * 0.08;
    disc.style.transform = "translate3d(" + x.toFixed(2) + "px," + y.toFixed(2) + "px,0)";
    raf = (Math.abs(tx - x) > 0.1 || Math.abs(ty - y) > 0.1) ? requestAnimationFrame(tick) : 0;
  }
  function go() { if (!raf) raf = requestAnimationFrame(tick); }
  hero.addEventListener("pointermove", function (e) {
    var b = hero.getBoundingClientRect();
    tx = ((e.clientX - b.left) / b.width - 0.5) * -44;
    ty = ((e.clientY - b.top) / b.height - 0.5) * -44;
    go();
  });
  hero.addEventListener("pointerleave", function () { tx = 0; ty = 0; go(); });
})();
