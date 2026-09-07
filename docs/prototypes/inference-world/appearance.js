// Restore before styles paint. Appearance is independent of research/demo state.
(() => {
  const storageKey = "euboulia.appearance.v1";
  const names = {
    neutral: "雾白",
    graphite: "石墨",
    cream: "奶油黄",
    sage: "暖绿",
  };
  const valid = (value) => (Object.hasOwn(names, value) ? value : "neutral");
  let current = "neutral";
  try {
    current = valid(localStorage.getItem(storageKey));
  } catch {
    /* Session-only fallback. */
  }
  document.documentElement.dataset.theme = current;
  document.addEventListener("DOMContentLoaded", () => {
    const dialog = document.querySelector("#appearance-dialog");
    const trigger = document.querySelector("#appearance-trigger");
    const inputs = [...dialog.querySelectorAll('input[name="appearance"]')];
    const status = document.querySelector("#appearance-status");
    let returnFocus;
    function apply(value, save = false) {
      current = valid(value);
      document.documentElement.dataset.theme = current;
      document.querySelector('meta[name="color-scheme"]').content =
        current === "graphite" ? "dark" : "light";
      inputs.forEach((input) => (input.checked = input.value === current));
      trigger.title = `外观 · ${names[current]}`;
      status.textContent = `${names[current]} · 已应用`;
      if (save) {
        try {
          localStorage.setItem(storageKey, current);
        } catch {
          status.textContent = `${names[current]} · 此次生效，浏览器未允许保存`;
        }
      }
    }
    trigger.addEventListener("click", () => {
      returnFocus = document.activeElement;
      dialog.showModal();
      inputs.find((input) => input.checked)?.focus();
    });
    inputs.forEach((input) =>
      input.addEventListener("change", () => {
        if (input.checked) apply(input.value, true);
      }),
    );
    dialog
      .querySelectorAll("[data-appearance-close]")
      .forEach((button) =>
        button.addEventListener("click", () => dialog.close()),
      );
    dialog.addEventListener("close", () => {
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    });
    dialog.addEventListener("click", (event) => {
      const bounds = dialog.getBoundingClientRect();
      if (
        event.target === dialog &&
        (event.clientX < bounds.left ||
          event.clientX > bounds.right ||
          event.clientY < bounds.top ||
          event.clientY > bounds.bottom)
      )
        dialog.close();
    });
    window.addEventListener("storage", (event) => {
      if (event.key === storageKey) apply(event.newValue);
    });
    apply(current);
  });
})();
