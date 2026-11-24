(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");
  function two(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    var d = new Date(), h = d.getUTCHours(), m = d.getUTCMinutes(), s = d.getUTCSeconds();
    var txt = two(h) + ":" + two(m) + ":" + two(s);
    [].forEach.call(document.querySelectorAll("[data-utc]"), function (el) { el.textContent = txt; });
    [].forEach.call(document.querySelectorAll("[data-utc-short]"), function (el) { el.textContent = two(h) + ":" + two(m) + " UTC"; });
    var set = function (id, deg) { var el = document.getElementById(id); if (el) el.style.transform = "rotate(" + deg + "deg)"; };
    set("h-hr", ((h % 12) + m / 60) * 30);
    set("h-min", (m + s / 60) * 6);
    set("h-sec", s * 6);
  }
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.querySelector && f.querySelector("button.danger") && !window.confirm("Delete this? This cannot be undone.")) e.preventDefault();
  });
  document.addEventListener("DOMContentLoaded", function () {
    tick();
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setInterval(tick, still ? 30000 : 1000);
    var items = [].slice.call(document.querySelectorAll(".rv"));
    if (!items.length) return;
    function show(el) { el.classList.add("in"); }
    if (!("IntersectionObserver" in window)) { items.forEach(show); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { show(e.target); io.unobserve(e.target); } });
    }, { threshold: 0.12 });
    items.forEach(function (s) { io.observe(s); });
    setTimeout(function () { items.forEach(show); }, 6000);
  });
})();
