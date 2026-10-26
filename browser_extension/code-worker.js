// Code delivery never depends on which tab/window currently has keyboard focus.
const active = new Map(); // One current form per tab, not one task per old nonce.
chrome.tabs.onRemoved?.addListener(tabId => active.get(tabId)?.controller.abort());
chrome.tabs.onUpdated?.addListener((tabId, change) => {
  if (change.url || change.status === 'loading') active.get(tabId)?.controller.abort();
});
async function request(path, watch, receipt, tag, signal) {
  const config = await (await fetch(chrome.runtime.getURL('bridge-config.json'),
    {cache: 'no-store'})).json();
  if (!config.port || !config.token) throw new Error('Bridge not paired');
  const response = await fetch(`http://127.0.0.1:${config.port}${path}`, {
    method: 'POST', headers: {'Content-Type': 'application/json',
      Authorization: `Bearer ${config.token}`},
    body: JSON.stringify({watch, receipt, tag}),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(path === '/poll' ? 25000 : 2000)]) :
      AbortSignal.timeout(path === '/poll' ? 25000 : 2000),
  });
  if (!response.ok) throw new Error('Bridge unavailable');
  return response.json();
}
async function leftCodeForm(tabId) {
  for (let i = 0; i < 30; i++) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    let tab;
    try { tab = await chrome.tabs.get(tabId); } catch (_) { return false; }
    // No URL: the tab moved on to a site this extension has no access to (Пульс, СДО).
    if (!tab.url || !tab.url.includes('/login-actions/authenticate')) return true;
  }
  return false;
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (!['mirea-watch', 'mirea-unwatch'].includes(message.type) || sender.frameId !== 0 || !sender.tab) return;
  const url = new URL(sender.url || 'about:blank');
  if (url.origin !== 'https://sso.mirea.ru' ||
      !url.pathname.includes('/login-actions/authenticate')) return;
  if (typeof message.nonce !== 'string' || !message.nonce || message.nonce.length > 64) return;
  const existing = active.get(sender.tab.id);
  if (message.type === 'mirea-unwatch') {
    if (existing?.nonce === message.nonce) existing.controller.abort();
    respond?.({}); return;
  }
  if (existing?.nonce === message.nonce) {
    respond?.({watching: true}); return;
  }
  existing?.controller.abort();
  const session = {nonce: message.nonce, controller: new AbortController()};
  active.set(sender.tab.id, session);
  respond?.({watching: true});
  const id = `${sender.tab.id}:${message.nonce}`;
  const tag = typeof message.tag === 'string' && /^[0-9A-Z]{1,6}$/.test(message.tag) ?
    message.tag : undefined;
  (async () => {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(id + sender.url));
    const watch = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    const deadline = Date.now() + 180000;
    const signal = session.controller.signal;
    let failures = 0;
    try {
      while (Date.now() < deadline && !signal.aborted) {
       try {
        const probe = await chrome.tabs.sendMessage(sender.tab.id,
          {type: 'mirea-probe', nonce: message.nonce}, {frameId: 0});
        if (!probe?.ready) break;
        // Fast registration is independent of the long-held response waiting for mail.
        const registered = await request('/watch', watch, undefined, tag, signal);
        if (registered.watching === false) throw new Error('Bridge is busy');
        const result = await request('/poll', watch, undefined, tag, signal);
        if (signal.aborted) break;
        failures = 0;
        if (result.code) {
          const filled = await chrome.tabs.sendMessage(sender.tab.id,
            {type: 'mirea-fill', nonce: message.nonce, code: result.code}, {frameId: 0});
          if (filled?.filled) {
            // Filling is never repeated just because the receipt response was lost.
            for (let attempt = 0; attempt < 3; attempt++) {
              try {
                await request('/ack', watch, result.receipt, undefined, signal);
                break;
              } catch (_) {
                if (signal.aborted) break;
                await new Promise(resolve => setTimeout(resolve, 100));
              }
            }
            // The page submits a full code itself; once it has left the code form
            // the sign-in went through, and the app says so instead of «Ctrl+V».
            if (await leftCodeForm(sender.tab.id)) await request('/signed-in', watch);
          }
          break;
        }
       } catch (_) {
         if (signal.aborted) break;
         // A transient bridge/worker failure must not leave a form silent for five seconds.
         await new Promise(resolve => setTimeout(resolve, Math.min(1000, 100 * 2 ** Math.min(failures++, 4))));
       }
      }
    } catch (_) { /* Clipboard fallback remains available; no secrets in console. */ }
    finally {
      try { await request('/cancel', watch); } catch (_) {}
      if (active.get(sender.tab.id) === session) active.delete(sender.tab.id);
      try { await chrome.tabs.sendMessage(sender.tab.id,
        {type: 'mirea-watch-ended', nonce: message.nonce}, {frameId: 0}); } catch (_) {}
    }
  })();
});

// --- Keeping itself current, with nothing for the person to do -------------
// The app rewrites this folder at every start and stamps the manifest's
// version_name with a hash of the files. The loaded manifest keeps the old
// stamp until a reload, so a difference means new code is waiting on disk.
const RELOAD_PAUSE_MS = 10 * 60 * 1000;  // never a reload loop, whatever happens
async function updateIfChanged() {
  try {
    const disk = await (await fetch(chrome.runtime.getURL('manifest.json'),
      {cache: 'no-store'})).json();
    const loaded = chrome.runtime.getManifest().version_name;
    if (!disk.version_name || disk.version_name === loaded) return false;
    // Never in the middle of someone's sign-in: the next minute will do.
    if ((await chrome.tabs.query({url: 'https://sso.mirea.ru/*'})).length) return false;
    const {reloadedAt = 0} = await chrome.storage.local.get('reloadedAt');
    if (Date.now() - reloadedAt < RELOAD_PAUSE_MS) return false;
    await chrome.storage.local.set({reloadedAt: Date.now()});
    chrome.runtime.reload();
    return true;
  } catch (_) { return false; }
}
// Tells the app the extension is installed and working, so it can say so.
async function hello() {
  if (await updateIfChanged()) return;
  try { await request('/hello', '0'.repeat(64)); } catch (_) { /* app not running */ }
}
// Open sign-in tabs keep the old scripts after an install or update: give them
// the current ones, so a code arriving right now still lands in the field.
async function refreshOpenSignInTabs() {
  try {
    for (const tab of await chrome.tabs.query({url: 'https://sso.mirea.ru/*'})) {
      try {
        await chrome.scripting.executeScript({target: {tabId: tab.id},
          files: ['skip-max.js', 'email-code.js']});
      } catch (_) { /* a tab that is closing or not a page */ }
    }
  } catch (_) {}
}
chrome.alarms.create('mirea-hello', {periodInMinutes: 1});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'mirea-hello') hello(); });
chrome.runtime.onStartup.addListener(hello);
chrome.runtime.onInstalled.addListener(details => {
  // Not on a browser update: the open tabs already run the current scripts.
  if (details.reason === 'install' || details.reason === 'update') refreshOpenSignInTabs();
  hello();
});
