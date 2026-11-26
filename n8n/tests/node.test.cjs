const assert = require('node:assert/strict');
const { test } = require('node:test');
const { YaMa } = require('../dist/nodes/YaMa/YaMa.node.js');
const { YaMaApi } = require('../dist/credentials/YaMaApi.credentials.js');
const node = new YaMa();
function context(items, toolReply, url = 'https://gateway.example/mcp', tolerate = false) {
  const calls = [];
  return {
    calls,
    getInputData: () => items,
    getNode: () => ({ name: 'Яндекс Почта' }),
    continueOnFail: () => tolerate,
    getNodeParameter: (key, i) => items[i][key],
    getCredentials: async () => ({ url, token: 'fixture' }),
    helpers: {
      httpRequestWithAuthentication: async (name, options) => {
        assert.equal(name, 'yaMaApi');
        assert.equal(options.disableFollowRedirect, true);
        calls.push(options.body);
        const { method, id } = options.body;
        if (method === 'initialize') return { id, result: { protocolVersion: '2025-11-25' } };
        if (method === 'notifications/initialized') return;
        return { id, result: toolReply };
      },
    },
  };
}
test('n8n package registers its node, credentials and all operations', () => {
  const pkg = require('../package.json');
  assert.equal(require('../' + pkg.n8n.nodes[0]).YaMa, YaMa);
  assert.equal(require('../' + pkg.n8n.credentials[0]).YaMaApi, YaMaApi);
  assert.equal(new YaMaApi().properties[1].typeOptions.password, true);
  assert.equal(new Set(node.description.properties[0].options.map((o) => o.value)).size, Object.keys(require('../nodes/YaMa/catalog.json')).length);
});
test('poll keeps receipt and never auto-acknowledges', async () => {
  const ctx = context(
    [
      {
        operation: 'poll_new',
        poll_new_feed: 'morning',
        poll_new_account: 'qa@example.org',
        poll_new_folder: 'INBOX',
        poll_new_limit: 1,
        poll_new_include_existing: false,
      },
    ],
    { content: [{ type: 'text', text: '{"receipt":"batch","messages":[]}' }] },
  );
  const [items] = await node.execute.call(ctx);
  assert.equal(items[0].json.result.receipt, 'batch');
  assert.deepEqual(
    ctx.calls.map((r) => r.method),
    ['initialize', 'notifications/initialized', 'tools/call'],
  );
  assert.deepEqual(ctx.calls[2].params.arguments, {
    account: 'qa@example.org',
    feed: 'morning',
    folder: 'INBOX',
    limit: 1,
    include_existing: false,
  });
});
test('batch results keep item links and singleton lists', async () => {
  const ctx = context(
    [
      { operation: 'list_folders', list_folders_account: 'a@example.org' },
      { operation: 'list_folders', list_folders_account: 'b@example.org' },
    ],
    { content: [{ type: 'text', text: '{"folder":"INBOX"}' }] },
  );
  const [items] = await node.execute.call(ctx);
  assert.deepEqual(
    items.map((x) => x.pairedItem.item),
    [0, 1],
  );
  assert.deepEqual(items[1].json.result, [{ folder: 'INBOX' }]);
});
test('failed tools never become successes and insecure URLs are rejected before auth', async () => {
  const ctx = context(
    [{ operation: 'list_accounts' }],
    { isError: true, content: [{ type: 'text', text: 'Access denied' }] },
    undefined,
    true,
  );
  assert.match((await node.execute.call(ctx))[0][0].json.error, /Access denied/);
  const bad = context([{ operation: 'list_accounts' }], {}, 'http://bad.example/mcp', true);
  assert.match((await node.execute.call(bad))[0][0].json.error, /HTTPS/);
  assert.equal(bad.calls.length, 0);
});
test('structured list results retain their array shape', async () => {
  const folders = [{ folder: 'INBOX' }];
  const ctx = context([{ operation: 'list_folders', list_folders_account: 'a@example.org' }], {
    structuredContent: { result: folders },
  });
  const [items] = await node.execute.call(ctx);
  assert.deepEqual(items[0].json.result, folders);
});

test('cleanup retains server retention unless the owner supplies days', async () => {
  const days = node.description.properties.find(p => p.name === 'cleanup_folder_days');
  assert.equal(days.default, '');
  const item = { operation: 'cleanup_folder', cleanup_folder_account: 'qa@example.org',
    cleanup_folder_role: 'drafts', cleanup_folder_days: days.default };
  const ctx = context([item, { ...item, cleanup_folder_days: '5' }], { structuredContent: {} });
  await node.execute.call(ctx);
  assert.equal(Object.hasOwn(ctx.calls[2].params.arguments, 'days'), false);
  assert.equal(ctx.calls[5].params.arguments.days, 5);
});
