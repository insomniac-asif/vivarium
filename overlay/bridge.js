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
      var hit = false;
      if (x >= 0 && y >= 0 && x < canvas.width && y < canvas.height) {
        try {
          hit = canvas.getContext('2d').getImageData(x, y, 1, 1).data[3] > 12;
        } catch (err) {}
      }
      if (hit !== lastHit) { lastHit = hit; window.junaBridge.hit(hit); }
    });
    document.addEventListener('mouseleave', function () {
      if (lastHit !== false) { lastHit = false; window.junaBridge.hit(false); }
    });
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
