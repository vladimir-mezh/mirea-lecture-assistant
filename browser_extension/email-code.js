(() => {
  let nonce = '', field = null, requestedAt = 0;
  const locate = () => {
    if (location.origin !== 'https://sso.mirea.ru' ||
        !location.pathname.includes('/login-actions/authenticate')) return null;
    // Never fill passwords, MAX/SMS/authenticator challenges, or chat editors.
    const explicit = document.querySelector('input[name="emailCode"], input#emailCode');
    const form = explicit?.form || document.querySelector('#email-code-form, #kc-email-code-form, form[name="email-code-form"]');
    if (!form) return null;
    const input = form.querySelector('input[name="emailCode"], input[autocomplete="one-time-code"], input[type="password"]');
    if (!input || input.disabled || input.readOnly || input.hidden || input.type === 'hidden' ||
        input.value || input.name === 'password') return null;
    return input;
  };
  let observer = null, timer = null;
  const connected = !!globalThis.chrome?.runtime?.id;
  const watch = () => {
    if (connected && !globalThis.chrome?.runtime?.id) {
      // The extension updated itself and runs a fresh copy of this script here.
      observer?.disconnect(); clearInterval(timer); return;
    }
    const candidate = locate();
    if (!candidate) { field = null; nonce = ''; return; }
    if (candidate === field && Date.now() - requestedAt < 180000) return;
    field = candidate; nonce = crypto.randomUUID(); requestedAt = Date.now();
    chrome.runtime.sendMessage({type: 'mirea-watch', nonce}).catch(() => {});
  };
  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message.type === 'mirea-watch-ended' && message.nonce === nonce) {
      requestedAt = 0; respond({}); return;
    }
    if (!['mirea-probe', 'mirea-fill'].includes(message.type)) return;
    const ready = message.nonce === nonce && field && field === locate();
    if (message.type === 'mirea-probe') { respond({ready: !!ready}); return; }
    if (!ready || !/^\d{6}$/.test(message.code || '')) { respond({filled: false}); return; }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(field, message.code);
    field.dispatchEvent(new Event('input', {bubbles: true}));
    field.dispatchEvent(new Event('change', {bubbles: true}));
    respond({filled: field.value === message.code});
    field = null; nonce = '';
  });
  observer = new MutationObserver(watch);
  observer.observe(document.documentElement, {subtree: true, childList: true, attributes: true});
  timer = setInterval(watch, 5000);
  watch();
})();
