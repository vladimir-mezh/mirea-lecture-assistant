// Code delivery never depends on which tab/window currently has keyboard focus.
const active = new Set();
async function request(path, watch, receipt) {
  const config = await (await fetch(chrome.runtime.getURL('bridge-config.json'),
    {cache: 'no-store'})).json();
  if (!config.port || !config.token) throw new Error('Bridge not paired');
  const response = await fetch(`http://127.0.0.1:${config.port}${path}`, {
    method: 'POST', headers: {'Content-Type': 'application/json',
      Authorization: `Bearer ${config.token}`},
    body: JSON.stringify({watch, receipt}), signal: AbortSignal.timeout(25000),
  });
  if (!response.ok) throw new Error('Bridge unavailable');
  return response.json();
}
chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.type !== 'mirea-watch' || sender.frameId !== 0 || !sender.tab) return;
  const url = new URL(sender.url || 'about:blank');
  if (url.origin !== 'https://sso.mirea.ru' ||
      !url.pathname.includes('/login-actions/authenticate')) return;
  const id = `${sender.tab.id}:${message.nonce}`;
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
        const result = await request('/poll', watch);
        if (result.code) {
          const filled = await chrome.tabs.sendMessage(sender.tab.id,
            {type: 'mirea-fill', nonce: message.nonce, code: result.code}, {frameId: 0});
          if (filled?.filled) await request('/ack', watch, result.receipt);
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
