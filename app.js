(function () {
  "use strict";
  document.documentElement.classList.add("js");
  document.addEventListener("DOMContentLoaded", function () {
    var shots = [].slice.call(document.querySelectorAll(".shot"));
    if (!shots.length) return;
    function show(el) { el.classList.add("in"); }
    if (!("IntersectionObserver" in window)) { shots.forEach(show); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { show(e.target); io.unobserve(e.target); } });
    }, { threshold: 0.15 });
    shots.forEach(function (s) { io.observe(s); });
    setTimeout(function () { shots.forEach(show); }, 6000);
  });
})();
