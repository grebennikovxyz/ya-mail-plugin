const assert = require('node:assert/strict');
const { YaMa } = require('../dist/nodes/YaMa/YaMa.node.js');
const node = new YaMa();
const url = process.argv[2];

async function execute(operation, parameters, token) {
  const context = {
    getInputData: () => [{}],
    getNode: () => ({ name: 'Яндекс Почта' }),
    continueOnFail: () => true,
    getNodeParameter: (key) => (key === 'operation' ? operation : parameters[key]),
    getCredentials: async () => ({ url, token }),
    helpers: {
      async httpRequestWithAuthentication(name, options) {
        assert.equal(name, 'yaMaApi');
        const response = await fetch(options.url, {
          method: options.method,
          headers: {
            ...options.headers,
            'Content-Type': 'application/json',
            Authorization: 'Bearer ' + token,
          },
          body: JSON.stringify(options.body),
          redirect: 'error',
        });
        if (!response.ok) throw Object.assign(new Error(), { statusCode: response.status });
        const body = await response.text();
        return body ? JSON.parse(body) : undefined;
      },
    },
  };
  return (await node.execute.call(context))[0][0].json;
}
(async () => {
  const token = process.env.TEST_YA_MAIL_TOKEN;
  const listed = await execute('list_accounts', {}, token);
  assert.deepEqual(listed.result.accounts, [{ email: 'fixture@example.org', mode: 'read' }]);
  const unknown = await execute(
    'list_folders',
    { list_folders_account: 'absent@example.org' },
    token,
  );
  assert.match(unknown.error, /Подключите этот ящик/);
  const denied = await execute('list_accounts', {}, 'invalid');
  assert.match(denied.error, /HTTP 401/);
  console.log('n8n node verified over HTTPS: account list, tool error, rejected credential.');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
