const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '../browser_extension/code-worker.js'), 'utf8');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate) {
  for (let i = 0; i < 200 && !predicate(); i++) await delay(5);
  assert(predicate(), 'worker did not complete within one second');
}
function harness(poll, acknowledge = () => ({accepted: true})) {
  let listener, removed;
  const ready = new Map(), calls = [], fills = [];
  const context = {
    crypto: webcrypto, TextEncoder, Uint8Array, URL, Date, AbortSignal, AbortController,
    setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms, 5)),
    fetch: async (url, options) => {
      if (url === 'extension:config') return {json: async () => ({port: 1, token: 'test-pairing'})};
      const route = new URL(url).pathname, body = JSON.parse(options.body);
      calls.push({route, body});
      const result = route === '/poll' ? await poll(body, options.signal) :
        route === '/ack' ? await acknowledge(body) : {};
      return {ok: true, json: async () => result};
    },
    chrome: {
      alarms: {create() {}, onAlarm: {addListener() {}}},
      storage: {local: {get: async () => ({}), set: async () => {}}},
      runtime: {getURL: () => 'extension:config',
        onMessage: {addListener: fn => {listener = fn;}},
        onInstalled: {addListener() {}}, onStartup: {addListener() {}}},
      tabs: {
        onRemoved: {addListener: fn => {removed = fn;}},
        sendMessage: async (tab, message) => {
          const matches = ready.get(tab) === message.nonce;
          if (message.type === 'mirea-probe') return {ready: matches};
          if (message.type === 'mirea-fill') {
            if (matches) fills.push({tab, nonce: message.nonce});
            return {filled: matches};
          }
          return {};
        },
        get: async () => ({url: 'https://pulse.mirea.ru/'}),
      },
    },
  };
  vm.runInNewContext(source, context);
  return {calls, fills, close: tab => {ready.delete(tab); removed(tab);},
    watch(tab, nonce, tag) {
      ready.set(tab, nonce);
      listener({type: 'mirea-watch', nonce, tag}, {frameId: 0, tab: {id: tab},
        url: 'https://sso.mirea.ru/realms/mirea/login-actions/authenticate'}, () => {});
    }};
}

test('transient bridge failure reconnects and fills without a five-second retry gap', async () => {
  let polls = 0;
  const h = harness(async () => {
    if (++polls === 1) throw new Error('temporary connection reset');
    return {code: '123456', receipt: 'test-receipt'};
  });
  h.watch(7, 'current', 'A2');
  await until(() => h.calls.some(call => call.route === '/ack'));
  assert.equal(polls, 2);
  assert.equal(h.fills.length, 1);
});

test('new challenge aborts the old long poll instead of leaving two waiters', async () => {
  let aborted = false;
  const h = harness(async (body, signal) => {
    if (body.tag === 'OLD') return new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => {aborted = true; reject(new Error('aborted'));}, {once: true});
    });
    return {code: '123456', receipt: 'new-receipt'};
  });
  h.watch(7, 'old', 'OLD');
  await until(() => h.calls.some(call => call.route === '/poll'));
  h.watch(7, 'new', 'A2');
  await until(() => h.calls.some(call => call.route === '/ack'));
  assert(aborted);
  assert.deepEqual(h.fills, [{tab: 7, nonce: 'new'}]);
  assert(h.calls.some(call => call.route === '/cancel'));
});

test('heartbeat does not create duplicate waiters for the same form', async () => {
  let release;
  const h = harness(() => new Promise(resolve => {release = resolve;}));
  h.watch(7, 'same', 'A2');
  h.watch(7, 'same', 'A2');
  await until(() => !!release);
  assert.equal(h.calls.filter(call => call.route === '/poll').length, 1);
  release({code: '123456', receipt: 'r'});
  await until(() => h.calls.some(call => call.route === '/ack'));
});

test('closing a tab immediately cancels its waiting request without typing', async () => {
  const h = harness((_body, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true});
  }));
  h.watch(7, 'waiting', 'A2');
  await until(() => h.calls.some(call => call.route === '/poll'));
  h.close(7);
  await until(() => h.calls.some(call => call.route === '/cancel'));
  assert.equal(h.fills.length, 0);
});

test('lost acknowledgement is retried without entering the code twice', async () => {
  let acknowledgements = 0;
  const h = harness(() => ({code: '123456', receipt: 'r'}), () => {
    if (++acknowledgements === 1) throw new Error('lost response');
    return {accepted: true};
  });
  h.watch(7, 'one', 'A2');
  await until(() => acknowledgements === 2);
  assert.equal(h.fills.length, 1);
});
