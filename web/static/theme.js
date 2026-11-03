/* Theme switch: remembers Claro/Oscuro and flips the data-theme attribute.
 *
 * The attribute, not a class, is the single switching mechanism: styles.css
 * maps the dark tokens onto html[data-theme="dark"], and html[data-theme="light"]
 * blocks the system preference. With no stored choice the attribute is absent
 * and the media query decides, exactly as the inline head script leaves it.
 */
(function () {
  "use strict";

  var KEY = "tema";
  var root = document.documentElement;
  var radios = document.querySelectorAll('input[name="tema"]');
  if (!radios.length) {
    return;
  }

  function readStored() {
    try {
      return localStorage.getItem(KEY);
    } catch (e) {
      return null; // private mode: the choice just does not persist
    }
  }

  function store(theme) {
    try {
      localStorage.setItem(KEY, theme);
    } catch (e) {}
  }

  function apply(theme) {
    // A theme flip changes colour on nearly every element at once; without
    // this the switch smears. Disable transitions, commit the new colours,
    // then drop the stylesheet on the next frame.
    var style = document.createElement("style");
    style.appendChild(
      document.createTextNode("*,*::before,*::after{transition:none !important}")
    );
    document.head.appendChild(style);

    root.setAttribute("data-theme", theme);

    void document.body.offsetHeight; // force a reflow while transitions are off

    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        style.remove();
      });
    });
  }

  for (var i = 0; i < radios.length; i++) {
    radios[i].addEventListener("change", function (event) {
      store(event.target.value);
      apply(event.target.value);
    });
  }

  // The system preference, mirrored into the control while nothing is stored: a
  // page in dark must not look like "nothing is selected". It is only reflected,
  // never stored — the media query stays in charge, so a system change is live.
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function reflectSystem() {
    if (readStored() === "dark" || readStored() === "light") {
      return;
    }
    var quiere = media && media.matches ? "dark" : "light";
    for (var k = 0; k < radios.length; k++) {
      radios[k].checked = radios[k].value === quiere;
    }
  }

  var stored = readStored();
  if (stored === "dark" || stored === "light") {
    for (var j = 0; j < radios.length; j++) {
      radios[j].checked = radios[j].value === stored;
    }
  } else {
    reflectSystem();
  }

  if (media) {
    var alCambiar = function () {
      reflectSystem();
    };
    if (media.addEventListener) {
      media.addEventListener("change", alCambiar);
    } else if (media.addListener) {
      media.addListener(alCambiar); // older Safari
    }
  }
})();
