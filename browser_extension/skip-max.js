// Presses «Пропустить» on MIREA's offer to confirm sign-ins through МАКС.
//
// Only where the page itself offers to skip: a Keycloak required-action page
// with a form carrying skip=true and a «Пропустить» button — the very control a
// person would press. Nothing is typed, read or sent anywhere else.
(() => {

  const skipButton = () => {
    if (!location.pathname.endsWith("/login-actions/required-action") ||
        new URLSearchParams(location.search).get('execution') !== 'max-account-config') return null;
    for (const skip of document.querySelectorAll("form input[name='skip'][value='true']")) {
      const form = skip.form;
      if (!form || !(form.getAttribute("action") || "").includes("required-action")) continue;
      const button = [...form.querySelectorAll("button, input[type='submit']")].find(
        (control) => (control.value || control.textContent || "").trim() === "Пропустить",
      );
      if (button && !button.disabled && button.getClientRects().length) return button;
    }
    return null;
  };

  let pressed = null;
  // After the extension updates itself, a fresh copy of this script takes over
  // the open tab; the old copy (cut off from the extension) must stand down.
  const connected = !!globalThis.chrome?.runtime?.id;
  const orphaned = () => connected && !globalThis.chrome?.runtime?.id;
  const press = () => {
    if (orphaned()) return true;
    const button = skipButton();
    if (!button) return false;
    if (pressed === button) return false;
    pressed = button;
    console.info("[МИРЭА: пропуск МАКС] нажимаем «Пропустить»");
    button.click();
    return false;
  };

  if (press()) return;
  // The page may draw its form a moment later.
  const watcher = new MutationObserver(() => {
    if (press()) watcher.disconnect();
  });
  watcher.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
  // React can change routes or draw the form long after the initial page load.
  // The exact offered skip form is checked on every mutation; never submit OTP.
})();
