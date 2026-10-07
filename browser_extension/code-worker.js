// Code delivery never depends on which tab/window currently has keyboard focus.
const active = new Set();
async function request(path, watch, receipt, tag) {
  const config = await (await fetch(chrome.runtime.getURL('bridge-config.json'),
    {cache: 'no-store'})).json();
  if (!config.port || !config.token) throw new Error('Bridge not paired');
  const response = await fetch(`http://127.0.0.1:${config.port}${path}`, {
    method: 'POST', headers: {'Content-Type': 'application/json',
      Authorization: `Bearer ${config.token}`},
    body: JSON.stringify({watch, receipt, tag}), signal: AbortSignal.timeout(25000),
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
chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.type !== 'mirea-watch' || sender.frameId !== 0 || !sender.tab) return;
  const url = new URL(sender.url || 'about:blank');
  if (url.origin !== 'https://sso.mirea.ru' ||
      !url.pathname.includes('/login-actions/authenticate')) return;
  const id = `${sender.tab.id}:${message.nonce}`;
  const tag = typeof message.tag === 'string' && /^[0-9A-Z]{1,6}$/.test(message.tag) ?
    message.tag : undefined;
  if (active.has(id) || typeof message.nonce !== 'string' || message.nonce.length > 64) return;
  active.add(id);
  (async () => {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(id + sender.url));
    const watch = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    const deadline = Date.now() + 180000;
    try {
      while (Date.now() < deadline) {
        const probe = await chrome.tabs.sendMessage(sender.tab.id,
          {type: 'mirea-probe', nonce: message.nonce}, {frameId: 0});
        if (!probe?.ready) break;
        const result = await request('/poll', watch, undefined, tag);
        if (result.code) {
          const filled = await chrome.tabs.sendMessage(sender.tab.id,
            {type: 'mirea-fill', nonce: message.nonce, code: result.code}, {frameId: 0});
          if (filled?.filled) {
            await request('/ack', watch, result.receipt);
            // The page submits a full code itself; once it has left the code form
            // the sign-in went through, and the app says so instead of «Ctrl+V».
            if (await leftCodeForm(sender.tab.id)) await request('/signed-in', watch);
          }
          break;
        }
      }
    } catch (_) { /* Clipboard fallback remains available; no secrets in console. */ }
    finally {
      active.delete(id);
      try { await request('/cancel', watch); } catch (_) {}
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
