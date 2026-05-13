// Shell-level JS — kept tiny on purpose. htmx + Alpine in the template
// handle most interactivity; this is just nav side-effects.

(function () {
  // Highlight in-flight links so navigation feels instant on slow networks.
  document.addEventListener("click", function (e) {
    var link = e.target.closest("a.nav-item");
    if (link && link.href && !link.classList.contains("is-active")) {
      link.style.opacity = "0.6";
    }
  });
})();
