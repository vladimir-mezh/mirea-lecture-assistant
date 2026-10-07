const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const {webcrypto} = require('node:crypto');
const source = name => fs.readFileSync(path.join(__dirname, '..', 'browser_extension', name), 'utf8');

// The chrome.* parts of the worker that only its self-update and hello use.
function lifecycle() {
  return {alarms: {create() {}, onAlarm: {addListener() {}}},
    storage: {local: {get: async () => ({}), set: async () => {}}}};
}

function page(origin = 'https://sso.mirea.ru', fieldName = 'emailCode') {
  let listener, watch;
  const sent = [];
  class Input {
    constructor() { this.name = fieldName; this._value = ''; this.events = []; }
    get value() { return this._value; }
    set value(value) { this._value = value; }
    dispatchEvent(event) { this.events.push(event.type); }
  }
  const input = new Input();
  input.form = {querySelector: () => input};
  const context = {
    location: {origin, pathname: '/realms/mirea/login-actions/authenticate'},
    crypto: webcrypto, HTMLInputElement: Input, Event,
    Date, setInterval: () => {},
    MutationObserver: class {constructor(callback) {watch = callback;} observe() {}},
    document: {documentElement: {}, querySelector: () => input},
    chrome: {runtime: {
      sendMessage: message => {sent.push(message); return Promise.resolve();},
      onMessage: {addListener: callback => {listener = callback;}},
    }},
  };
  vm.runInNewContext(source('email-code.js'), context);
  const message = value => {let response; listener(value, {}, r => {response = r;}); return response;};
  return {input, sent, message, watch};
}

test('inactive browser tab receives code only in the explicit email field', () => {
  const p = page();
  const nonce = p.sent[0].nonce;
  assert.equal(p.message({type: 'mirea-probe', nonce}).ready, true);
  assert.equal(p.message({type: 'mirea-fill', nonce, code: '123456'}).filled, true);
  assert.equal(p.input.value, '123456');
  assert.deepEqual(p.input.events, ['input', 'change']);
  assert.equal(p.message({type: 'mirea-fill', nonce, code: '654321'}).filled, false);
});
test('chat origin and password field never receive a code', () => {
  assert.equal(page('https://discord.com').sent.length, 0);
  assert.equal(page('https://sso.mirea.ru', 'password').sent.length, 0);
});
test('stale challenge and nonempty input are never overwritten', () => {
  const p = page(), nonce = p.sent[0].nonce;
  assert.equal(p.message({type: 'mirea-fill', nonce: 'old', code: '123456'}).filled, false);
  p.input.value = 'user-entered';
  assert.equal(p.message({type: 'mirea-fill', nonce, code: '123456'}).filled, false);
  assert.equal(p.input.value, 'user-entered');
});
test('worker delivers to the original tab, not the currently active one', async () => {
  let listener;
  const destinations = [], requests = [];
  let filled = false;
  const context = {
    crypto: webcrypto, TextEncoder, Uint8Array, URL, Date, AbortSignal,
    fetch: async (url, options) => {
      if (url === 'extension:config') return {json: async () => ({port: 1, token: 'fixture'})};
      requests.push([url, options]);
      return {ok: true, json: async () => url.endsWith('/poll') ?
        {code: '123456', receipt: 'fixture-receipt'} : {}};
    },
    chrome: {
      ...lifecycle(),
      runtime: {getURL: () => 'extension:config', onMessage: {addListener: f => {listener = f;}},
        onStartup: {addListener() {}}, onInstalled: {addListener() {}}},
      tabs: {sendMessage: async (tab, message) => {
        destinations.push(tab);
        if (message.type === 'mirea-probe') return {ready: true};
        if (message.type === 'mirea-fill') {filled = true; return {filled: true};}
        return {};
      }},
    },
  };
  vm.runInNewContext(source('code-worker.js'), context);
  listener({type: 'mirea-watch', nonce: 'fixture'}, {frameId: 0, tab: {id: 37},
    url: 'https://sso.mirea.ru/realms/mirea/login-actions/authenticate'});
  for (let i = 0; i < 50 && requests.length < 3; i++) await new Promise(r => setTimeout(r, 5));
  assert.equal(filled, true);
  assert(destinations.every(tab => tab === 37));
  assert(requests.some(([url]) => url.endsWith('/ack')));
  const count = requests.length;
  listener({type: 'mirea-watch', nonce: 'chat'}, {frameId: 0, tab: {id: 99}, url: 'https://discord.com'});
  await new Promise(r => setTimeout(r, 20));
  assert.equal(requests.length, count);
});

function worker({disk, loaded, signInTabs = [], reloadedAt = 0, appUp = true}) {
  const calls = {reloads: 0, hellos: 0, injected: [], stored: []};
  const handlers = {};
  const context = {
    crypto: webcrypto, TextEncoder, Uint8Array, URL, Date, AbortSignal,
    fetch: async (url) => {
      if (url === 'extension:manifest.json') return {json: async () => disk};
      if (url === 'extension:bridge-config.json') return {json: async () => ({port: 1, token: 't'})};
      if (!appUp) throw new Error('connection refused');
      if (url.endsWith('/hello')) calls.hellos++;
      return {ok: true, json: async () => ({})};
    },
    chrome: {
      alarms: {create() {}, onAlarm: {addListener: f => {handlers.alarm = f;}}},
      storage: {local: {get: async () => ({reloadedAt}), set: async v => {calls.stored.push(v);}}},
      tabs: {query: async () => signInTabs, sendMessage: async () => ({})},
      scripting: {executeScript: async ({target}) => {calls.injected.push(target.tabId);}},
      runtime: {
        getURL: name => `extension:${name}`, getManifest: () => loaded,
        reload: () => {calls.reloads++;},
        onMessage: {addListener() {}},
        onStartup: {addListener: f => {handlers.startup = f;}},
        onInstalled: {addListener: f => {handlers.installed = f;}},
      },
    },
  };
  vm.runInNewContext(source('code-worker.js'), context);
  const settle = () => new Promise(r => setTimeout(r, 20));
  return {calls, handlers, settle};
}

test('new files from an app update reload the extension by itself', async () => {
  const w = worker({disk: {version_name: '1.2.0 new'}, loaded: {version_name: '1.2.0 old'}});
  w.handlers.alarm({name: 'mirea-hello'});
  await w.settle();
  assert.equal(w.calls.reloads, 1);
  assert.equal(w.calls.hellos, 0);
});
test('unchanged files: no reload, the app hears hello', async () => {
  const w = worker({disk: {version_name: '1.2.0 same'}, loaded: {version_name: '1.2.0 same'}});
  w.handlers.alarm({name: 'mirea-hello'});
  await w.settle();
  assert.equal(w.calls.reloads, 0);
  assert.equal(w.calls.hellos, 1);
});
test('never reloads in the middle of a sign-in, nor twice in ten minutes', async () => {
  const busy = worker({disk: {version_name: 'b'}, loaded: {version_name: 'a'},
    signInTabs: [{id: 5}]});
  busy.handlers.alarm({name: 'mirea-hello'});
  await busy.settle();
  assert.equal(busy.calls.reloads, 0);
  const recent = worker({disk: {version_name: 'b'}, loaded: {version_name: 'a'},
    reloadedAt: Date.now() - 60000});
  recent.handlers.alarm({name: 'mirea-hello'});
  await recent.settle();
  assert.equal(recent.calls.reloads, 0);
});
test('a fresh install serves sign-in tabs already open; a browser update does not', async () => {
  const w = worker({disk: {}, loaded: {}, signInTabs: [{id: 7}]});
  w.handlers.installed({reason: 'install'});
  await w.settle();
  assert.deepEqual(w.calls.injected, [7]);
  const again = worker({disk: {}, loaded: {}, signInTabs: [{id: 7}]});
  again.handlers.installed({reason: 'chrome_update'});
  await again.settle();
  assert.deepEqual(again.calls.injected, []);
});
test('app not running: hello fails quietly', async () => {
  const w = worker({disk: {}, loaded: {}, appUp: false});
  w.handlers.startup();
  await w.settle();
  assert.equal(w.calls.reloads, 0);
});
