// Tiny progressive enhancements. Everything works without JavaScript.
document.querySelectorAll("[data-reveal]").forEach(function (box) {
  var input = document.getElementById(box.getAttribute("data-reveal"));
  if (!input) return;
  box.addEventListener("change", function () { input.type = box.checked ? "text" : "password"; });
});
document.querySelectorAll("input[data-upper]").forEach(function (input) {
  input.addEventListener("input", function () {
    var pos = input.selectionStart;
    input.value = input.value.toUpperCase().replace(/[\s-]/g, "_").replace(/[^A-Z0-9_]/g, "");
    try { input.setSelectionRange(pos, pos); } catch (e) {}
  });
});
// Clear typed values if the page is restored from the back/forward cache.
window.addEventListener("pageshow", function (e) {
  if (e.persisted) document.querySelectorAll("input[type=password]").forEach(function (i) { i.value = ""; });
});
