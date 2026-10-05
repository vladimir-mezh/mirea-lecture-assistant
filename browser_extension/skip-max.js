// Presses «Пропустить» on MIREA's offer to confirm sign-ins through МАКС.
//
// Only where the page itself offers to skip: a Keycloak required-action page
// with a form carrying skip=true and a «Пропустить» button — the very control a
// person would press. Nothing is typed, read or sent anywhere else.
(() => {
  if (!location.pathname.endsWith("/login-actions/required-action")) return;

  const skipButton = () => {
    for (const skip of document.querySelectorAll("form input[name='skip'][value='true']")) {
      const form = skip.form;
      if (!form || !(form.getAttribute("action") || "").includes("required-action")) continue;
      const button = [...form.querySelectorAll("button, input[type='submit']")].find(
        (control) => (control.value || control.textContent || "").trim() === "Пропустить",
      );
      if (button) return button;
    }
    return null;
  };

  let pressed = false;
  const press = () => {
    if (pressed) return true;
    const button = skipButton();
    if (!button) return false;
    pressed = true;
    console.info("[МИРЭА: пропуск МАКС] нажимаем «Пропустить»");
    button.click();
    return true;
  };

  if (press()) return;
  // The page may draw its form a moment later.
  const watcher = new MutationObserver(() => {
    if (press()) watcher.disconnect();
  });
  watcher.observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(() => watcher.disconnect(), 15000);
})();
