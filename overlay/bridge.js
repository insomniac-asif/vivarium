// Wires a pet page (sprite renderer or procedural) into the Electron shell.
// Handles: pet + state push from main, manual drag, right-click menu.
(function () {
  if (!window.junaBridge) return; // plain-browser debug mode

  function engine() { return window.vivarium || window.juna; }

  window.junaBridge.onState(function (s) {
    var e = engine();
    if (e && e.setState) e.setState(s);
  });
  window.junaBridge.onPet(function (meta, sheetSrc) {
    var e = engine();
    if (e && e.setPet) e.setPet(meta, sheetSrc);
  });

  // report whether the cursor is over actual pet pixels, so the overlay can
  // stay click-through everywhere else
  var canvas = document.getElementById('stage');
  var lastHit = null;
  if (canvas) {
    document.addEventListener('mousemove', function (e) {
      var r = canvas.getBoundingClientRect();
      var x = Math.floor((e.clientX - r.left) * (canvas.width / r.width));
      var y = Math.floor((e.clientY - r.top) * (canvas.height / r.height));
      // sample a small neighbourhood, not one pixel: a single-pixel test on a
      // textured edge flickers between hit and miss as the cursor drifts, and
      // the flicker was cancelling the hover that opens the session tray
      var hit = false;
      var R = 5;
      var x0 = Math.max(0, x - R), y0 = Math.max(0, y - R);
      var x1 = Math.min(canvas.width, x + R + 1), y1 = Math.min(canvas.height, y + R + 1);
      if (x1 > x0 && y1 > y0) {
        try {
          var d = canvas.getContext('2d').getImageData(x0, y0, x1 - x0, y1 - y0).data;
          for (var i = 3; i < d.length; i += 4) {
            if (d[i] > 12) { hit = true; break; }
          }
        } catch (err) {}
      }
      if (hit !== lastHit) { lastHit = hit; window.junaBridge.hit(hit); }
    });
    document.addEventListener('mouseleave', function () {
      if (lastHit !== false) { lastHit = false; window.junaBridge.hit(false); }
    });
    // The pointer can leave this window without a mouseleave -- it crosses onto
    // the session card, which is a window of its own, and this page simply stops
    // hearing about the mouse. Reporting only changes then leaves 'over the pet'
    // latched on forever, and no later hover can reopen the card. Main clears
    // the latch when it knows the pointer has gone elsewhere.
    if (window.junaBridge.onForgetHit) {
      window.junaBridge.onForgetHit(function () { lastHit = null; });
    }
  }

  // Pointer events with capture, not mouse events: capture guarantees the
  // release is delivered even if the pointer leaves the window mid-drag.
  // Without it a lost mouseup leaves the pet glued to the cursor forever.
  var dragging = false;
  document.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    dragging = true;
    try { document.documentElement.setPointerCapture(e.pointerId); } catch (err) {}
    window.junaBridge.dragStart();
  });
  function endDrag(e) {
    if (!dragging) return;
    dragging = false;
    if (e && e.pointerId !== undefined) {
      try { document.documentElement.releasePointerCapture(e.pointerId); } catch (err) {}
    }
    window.junaBridge.dragEnd();
  }
  document.addEventListener('pointerup', endDrag);
  document.addEventListener('pointercancel', endDrag);
  window.addEventListener('mouseup', function () { endDrag(null); });
  window.addEventListener('blur', function () { endDrag(null); });
  document.addEventListener('contextmenu', function (e) {
    e.preventDefault();
    window.junaBridge.contextMenu();
  });
})();
