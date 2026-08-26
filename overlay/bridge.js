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

  var dragging = false;
  document.addEventListener('mousedown', function (e) {
    if (e.button === 0) { dragging = true; window.junaBridge.dragStart(); }
  });
  window.addEventListener('mouseup', function () {
    if (dragging) { dragging = false; window.junaBridge.dragEnd(); }
  });
  window.addEventListener('blur', function () {
    if (dragging) { dragging = false; window.junaBridge.dragEnd(); }
  });
  document.addEventListener('contextmenu', function (e) {
    e.preventDefault();
    window.junaBridge.contextMenu();
  });
})();
