(function () {
  "use strict";

  var reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var fine = !!(window.matchMedia && window.matchMedia("(pointer: fine)").matches);
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  var items = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
  var bar = document.querySelector(".progress");
  var fill = bar ? bar.querySelector("i") : null;
  var cursor = document.querySelector(".cursor");

  function reveal(el) { if (el) el.classList.add("is-in"); }
  function revealAll() { for (var i = 0; i < items.length; i++) reveal(items[i]); }

  function paint() {
    if (!bar) return;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    var p = max > 0 ? Math.min(1, Math.max(0, window.pageYOffset / max)) : 0;
    fill.style.transform = "scaleX(" + p + ")";
    bar.classList.toggle("on", window.pageYOffset > 4);
  }
  window.addEventListener("scroll", paint, { passive: true });
  window.addEventListener("resize", paint, { passive: true });
  paint();

  if (reduce) { revealAll(); return; }

  var cancelled = false;
  var raf = 0;

  function stopMotion() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
  }

  function clearDragStyles() {
    for (var i = 0; i < items.length; i++) {
      var el = items[i];
      el.style.transition = "";
      el.style.position = "";
      el.style.zIndex = "";
      el.style.opacity = "";
      el.style.transform = "";
    }
  }

  function cancel() {
    if (cancelled) return;
    cancelled = true;
    stopMotion();
    clearDragStyles();
    revealAll();
    if (cursor) {
      cursor.classList.remove("on");
      cursor.classList.remove("grab");
    }
    if (bar) bar.classList.remove("on");
  }

  ["wheel", "touchstart", "pointerdown", "keydown"].forEach(function (type) {
    window.addEventListener(type, cancel, { passive: true });
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) cancel();
  });

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function easeInOutCubic(p) {
    return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
  }

  function easeOutCubic(p) {
    return 1 - Math.pow(1 - p, 3);
  }

  function scrollToY(y, ms) {
    return new Promise(function (resolve) {
      var from = window.pageYOffset;
      var dist = y - from;
      if (Math.abs(dist) < 2) { resolve(); return; }
      var t0 = 0;
      function step(now) {
        if (cancelled) { resolve(); return; }
        if (!t0) t0 = now;
        var p = Math.min(1, (now - t0) / ms);
        window.scrollTo(0, from + dist * easeInOutCubic(p));
        if (p < 1) raf = requestAnimationFrame(step);
        else resolve();
      }
      raf = requestAnimationFrame(step);
    });
  }

  function revealOnScroll() {
    if (!("IntersectionObserver" in window)) { revealAll(); return; }
    var io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) {
          reveal(entries[i].target);
          io.unobserve(entries[i].target);
        }
      }
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    for (var j = 0; j < items.length; j++) io.observe(items[j]);
  }

  function revealPassed() {
    var limit = window.pageYOffset + window.innerHeight * 0.9;
    for (var i = 0; i < items.length; i++) {
      if (!items[i].classList.contains("is-in") &&
          items[i].getBoundingClientRect().top + window.pageYOffset < limit) {
        reveal(items[i]);
      }
    }
  }

  function autoScroll() {
    var max = document.documentElement.scrollHeight - window.innerHeight;
    if (max < 160) return;
    var duration = Math.min(11000, Math.max(3600, max * 4.2));
    var t0 = 0;
    revealPassed();
    function step(now) {
      if (cancelled) return;
      if (!t0) t0 = now;
      var p = Math.min(1, (now - t0) / duration);
      var bottom = document.documentElement.scrollHeight - window.innerHeight;
      window.scrollTo(0, bottom * easeInOutCubic(p));
      revealPassed();
      if (p < 1) raf = requestAnimationFrame(step);
      else if (bar) bar.classList.remove("on");
    }
    setTimeout(function () { if (!cancelled) raf = requestAnimationFrame(step); }, 700);
  }

  /* ---- the cursor that drags the page together ---- */
  var cx = 0;
  var cy = 0;

  function placeCursor(x, y) {
    cx = x;
    cy = y;
    cursor.style.transform = "translate3d(" + x + "px," + y + "px,0)";
  }

  function moveCursor(x, y) {
    return new Promise(function (resolve) {
      var fx = cx;
      var fy = cy;
      var dist = Math.sqrt((x - fx) * (x - fx) + (y - fy) * (y - fy));
      var duration = Math.max(210, Math.min(520, dist * 0.8));
      var t0 = 0;
      function step(now) {
        if (cancelled) { resolve(); return; }
        if (!t0) t0 = now;
        var p = Math.min(1, (now - t0) / duration);
        var e = easeInOutCubic(p);
        var arc = Math.sin(p * Math.PI) * Math.min(26, 7 + dist / 9);
        placeCursor(fx + (x - fx) * e, fy + (y - fy) * e - arc);
        if (p < 1) raf = requestAnimationFrame(step);
        else { placeCursor(x, y); resolve(); }
      }
      raf = requestAnimationFrame(step);
    });
  }

  function releaseCursor() {
    cursor.classList.remove("grab");
    var pulse = cursor.querySelector(".pulse");
    if (pulse) {
      pulse.classList.remove("go");
      void pulse.offsetWidth;
      pulse.classList.add("go");
    }
  }

  function ensureVisible(el) {
    var r = el.getBoundingClientRect();
    var h = window.innerHeight;
    if (r.top >= h * 0.1 && r.bottom <= h * 0.9) return Promise.resolve();
    var max = document.documentElement.scrollHeight - h;
    var target = window.pageYOffset + r.top - h * 0.3;
    return scrollToY(Math.max(0, Math.min(max, target)), 480);
  }

  function dragIntoPlace(el, i) {
    return new Promise(function (resolve) {
      var h = el.getBoundingClientRect().height;
      var dir = i % 2 ? -1 : 1;
      var dx = 66 * dir;
      var dy = 72;
      var rot = 1.1 * dir;
      var gx = 17;
      var gy = Math.min(28, Math.max(10, h / 2));

      el.style.transition = "none";
      el.style.transform = "none";
      var r = el.getBoundingClientRect();

      el.style.position = "relative";
      el.style.zIndex = "5";
      el.style.opacity = "0.3";
      el.style.transform = "translate(" + dx + "px," + dy + "px) rotate(" + rot + "deg)";

      var grabX = r.left + dx + gx;
      var grabY = r.top + dy + gy;

      moveCursor(grabX, grabY).then(function () {
        if (cancelled) { resolve(); return; }
        cursor.classList.add("grab");

        var t0 = 0;
        var duration = 400;
        function step(now) {
          if (cancelled) { resolve(); return; }
          if (!t0) t0 = now;
          var p = Math.min(1, (now - t0) / duration);
          var e = easeOutCubic(p);
          var tx = dx * (1 - e);
          var ty = dy * (1 - e);
          var tr = rot * (1 - e);
          el.style.transform = "translate(" + tx + "px," + ty + "px) rotate(" + tr + "deg)";
          el.style.opacity = String(0.3 + 0.7 * e);
          placeCursor(r.left + tx + gx, r.top + ty + gy);
          if (p < 1) {
            raf = requestAnimationFrame(step);
          } else {
            releaseCursor();
            reveal(el);
            el.style.transition = "";
            el.style.position = "";
            el.style.zIndex = "";
            el.style.opacity = "";
            el.style.transform = "";
            resolve();
          }
        }
        raf = requestAnimationFrame(step);
      });
    });
  }

  async function play() {
    placeCursor(window.innerWidth * 0.6, window.innerHeight * 0.6);
    await sleep(220);
    if (cancelled) return;
    cursor.classList.add("on");
    await sleep(220);

    for (var i = 0; i < items.length; i++) {
      if (cancelled) return;
      var el = items[i];
      await ensureVisible(el);
      if (cancelled) return;
      await dragIntoPlace(el, i);
      if (cancelled) return;
      await sleep(90);
    }

    if (cancelled) return;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    await scrollToY(max, 650);
    if (cancelled) return;
    await moveCursor(window.innerWidth - 70, window.innerHeight - 70);
    if (cancelled) return;
    await sleep(220);
    if (cancelled) return;
    cursor.classList.remove("on");
    if (bar) bar.classList.remove("on");
  }

  if (location.hash) {
    revealOnScroll();
    return;
  }

  window.scrollTo(0, 0);

  if (!cursor || !fine) {
    revealOnScroll();
    autoScroll();
    return;
  }

  play().catch(function () { cancel(); });
})();
