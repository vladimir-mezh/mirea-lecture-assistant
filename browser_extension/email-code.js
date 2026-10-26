(() => {
  // Re-injection and SPA transitions must not create several waiters for one form.
  globalThis.__mireaEmailCodeCleanup?.();
  let nonce = '', field = null, requestedAt = 0, askedTag = '', lastPing = 0;
  let observer = null, timer = null, stopped = false;
  const connected = !!globalThis.chrome?.runtime?.id;
  const pageTag = () => {
    const match = (document.body?.innerText || '').match(/код\S*\s*\(?\s*#\s*([0-9a-z]{1,6})(?![0-9a-z])/i);
    return match ? match[1].toUpperCase() : '';
  };
  const locate = () => {
    if (location.origin !== 'https://sso.mirea.ru' ||
        !location.pathname.includes('/login-actions/authenticate')) return null;
    const explicit = document.querySelector('input[name="emailCode"], input#emailCode');
    const form = explicit?.form || document.querySelector('#email-code-form, #kc-email-code-form, form[name="email-code-form"]');
    if (!form) return null;
    const input = form.querySelector('input[name="emailCode"], input[autocomplete="one-time-code"], input[type="password"]');
    if (!input || input.disabled || input.readOnly || input.hidden || input.type === 'hidden' ||
        input.value || input.name === 'password') return null;
    return input;
  };
  const send = message => {
    try { chrome.runtime.sendMessage(message).catch(() => { lastPing = 0; }); }
    catch (_) { lastPing = 0; }
  };
  const cancel = () => {
    if (nonce) send({type: 'mirea-unwatch', nonce});
    field = null; nonce = ''; requestedAt = 0; lastPing = 0;
  };
  const watch = () => {
    if (stopped) return;
    if (connected && !globalThis.chrome?.runtime?.id) { cleanup(); return; }
    const candidate = locate();
    if (!candidate) { cancel(); return; }
    const tag = pageTag(), now = Date.now();
    if (candidate !== field || tag !== askedTag || now - requestedAt >= 180000) {
      cancel();
      field = candidate; nonce = crypto.randomUUID(); requestedAt = now; askedTag = tag;
    }
    // Wakes a restarted MV3 worker without allocating another watcher or waiting for mail.
    if (now - lastPing >= 2000) {
      lastPing = now;
      send({type: 'mirea-watch', nonce, tag});
    }
  };
  const listener = (message, _sender, respond) => {
    if (message.type === 'mirea-watch-ended' && message.nonce === nonce) {
      lastPing = 0; watch(); respond({}); return;
    }
    if (!['mirea-probe', 'mirea-fill'].includes(message.type)) return;
    const ready = message.nonce === nonce && field && field === locate() && askedTag === pageTag();
    if (message.type === 'mirea-probe') { respond({ready: !!ready}); return; }
    if (!ready || !/^\d{6}$/.test(message.code || '')) { respond({filled: false}); return; }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(field, message.code);
    field.dispatchEvent(new Event('input', {bubbles: true}));
    field.dispatchEvent(new Event('change', {bubbles: true}));
    respond({filled: field.value === message.code});
    // The worker must acknowledge delivery before it cancels its server-side wait.
    field = null; nonce = ''; lastPing = 0;
  };
  const afterInteraction = () => queueMicrotask(watch);
  const cleanup = () => {
    if (stopped) return;
    stopped = true; cancel(); observer?.disconnect(); clearInterval(timer);
    chrome.runtime.onMessage.removeListener?.(listener);
    for (const event of ['click', 'submit', 'input', 'visibilitychange'])
      document.removeEventListener?.(event, afterInteraction, true);
  };
  globalThis.__mireaEmailCodeCleanup = cleanup;
  chrome.runtime.onMessage.addListener(listener);
  observer = new MutationObserver(watch);
  observer.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  for (const event of ['click', 'submit', 'input', 'visibilitychange'])
    document.addEventListener?.(event, afterInteraction, true);
  timer = setInterval(watch, 1000);
  watch();
})();
