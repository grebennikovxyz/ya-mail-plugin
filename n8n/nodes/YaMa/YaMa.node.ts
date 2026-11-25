import { NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';
import type {
  IDataObject,
  IExecuteFunctions,
  INodeExecutionData,
  INodeProperties,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import catalog from './catalog.json';

type Field = {
  name: string;
  type: string;
  default?: string | number | boolean;
  required?: boolean;
  options?: string[];
  minimum?: number;
  maximum?: number;
};
const operations: Record<string, { description: string; fields: Field[] }> = catalog;
const title = (s: string) =>
  s
    .split('_')
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ');
const hints: Record<string, string> = {
  account: 'Full connected Yandex Mail address',
  ref: 'Message reference from a previous search or read (account, folder, validity, uid)',
  refs: 'Array of message references',
  filters: 'Search criteria, for example {"unread":true,"has_attachment":true}',
  letter:
    'Complete message, for example {"to":["recipient@example.org"],"subject":"Order","text":"Hello"}',
  changes: 'For example {"add_labels":["Orders"],"starred":true}',
  scopes: 'For example [{"account":"orders@example.org","folders":["INBOX"]}]',
  request_id: 'Stable UUID for this send. Reuse it on retry.',
  receipt: 'Receipt returned by the poll for this account, folder and feed',
  feed: 'Stable name for this scheduled workflow',
};
function property(tool: string, field: Field): INodeProperties {
  const types: Record<string, INodeProperties['type']> = {
    integer: 'number',
    string: 'string',
    boolean: 'boolean',
  };
  const optionalInt = field.type === 'integer' && !field.required && field.default === undefined;
  const type = field.options ? 'options' : optionalInt ? 'string' : (types[field.type] ?? 'json');
  const defaultValue =
    field.default ??
    (type === 'number'
      ? (field.minimum ?? 0)
      : type === 'boolean'
        ? false
        : type === 'json' && field.required
          ? field.type === 'array'
            ? '[]'
            : '{}'
          : '');
  const options = field.options?.map((value) => ({ name: title(value), value }));
  if (options && !field.required) options.unshift({ name: 'Unspecified', value: '' });
  return {
    displayName: title(field.name),
    name: tool + '_' + field.name,
    type,
    default: field.options && field.required ? field.options[0] : defaultValue,
    required: !!field.required,
    description: hints[field.name],
    displayOptions: { show: { operation: [tool] } },
    ...(options ? { options } : {}),
    ...(type === 'number'
      ? { typeOptions: { minValue: field.minimum, maxValue: field.maximum } }
      : {}),
  };
}

type Reply = {
  id?: number;
  error?: { message: string };
  result?: {
    protocolVersion?: string;
    isError?: boolean;
    content?: { type: string; text?: string }[];
    structuredContent?: IDataObject;
  };
};
const protocolVersion = '2025-11-25';

export class YaMa implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Яндекс Почта (Ya-Ma)',
    name: 'yaMa',
    icon: 'file:ya-ma.png',
    group: ['transform'],
    version: 1,
    description: 'Read, organize and send Yandex Mail messages through your Ya-Ma gateway',
    defaults: { name: 'Яндекс Почта' },
    inputs: [NodeConnectionTypes.Main],
    outputs: [NodeConnectionTypes.Main],
    credentials: [{ name: 'yaMaApi', required: true }],
    properties: [
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        default: 'list_accounts',
        options: Object.entries(operations)
          .map(([value, op]) => ({
            name: title(value),
            value,
            action: title(value),
            description: op.description,
          }))
          .sort((a, b) => a.name.localeCompare(b.name)),
      },
      ...Object.entries(operations).flatMap(([tool, op]) =>
        op.fields.map((f) => property(tool, f)),
      ),
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const output: INodeExecutionData[] = [];
    for (const [i] of this.getInputData().entries()) {
      try {
        const tool = this.getNodeParameter('operation', i) as string;
        if (!Object.hasOwn(operations, tool)) throw new Error('Unknown Ya-Ma operation');
        const args: IDataObject = {};
        for (const field of operations[tool].fields) {
          const val = this.getNodeParameter(tool + '_' + field.name, i);
          if (val === '' || val === null || val === undefined) {
            if (field.required) throw new Error('Required field: ' + title(field.name));
            continue;
          }
          const nested = ['object', 'array'].includes(field.type);
          if (field.type === 'integer' && typeof val === 'string') {
            const n = Number(val);
            if (!/^-?\d+$/.test(val) || !Number.isSafeInteger(n) ||
                (field.minimum !== undefined && n < field.minimum) ||
                (field.maximum !== undefined && n > field.maximum)) throw new Error('Invalid integer: ' + field.name);
            args[field.name] = n;
          } else {
            args[field.name] = nested && typeof val === 'string' ? JSON.parse(val) : val;
          }
        }
        const credentials = await this.getCredentials('yaMaApi', i);
        const url = new URL(String(credentials.url));
        if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash)
          throw new Error('A plain HTTPS gateway URL is required');
        const rpc = async (
          method: string,
          params: IDataObject,
          id?: number,
          version?: string,
        ): Promise<Reply> => {
          let response;
          try {
            response = await this.helpers.httpRequestWithAuthentication.call(this, 'yaMaApi', {
              method: 'POST',
              url: url.href,
              disableFollowRedirect: true,
              timeout: 120000,
              json: true,
              headers: {
                Accept: 'application/json, text/event-stream',
                ...(version ? { 'MCP-Protocol-Version': version } : {}),
              },
              body: { jsonrpc: '2.0', method, params, ...(id === undefined ? {} : { id }) },
            });
          } catch (error) {
            const err = error as { httpCode?: string; statusCode?: number };
            throw new Error(
              'Ya-Ma gateway request failed' +
                (err.httpCode || err.statusCode
                  ? ' (HTTP ' + (err.httpCode || err.statusCode) + ')'
                  : ''),
            );
          }
          if (id === undefined) return {};
          if (!response || response.id !== id || response.error || !response.result)
            throw new Error(response?.error?.message || 'Invalid response from Ya-Ma');
          return response as Reply;
        };
        const hello = await rpc(
          'initialize',
          {
            protocolVersion,
            capabilities: {},
            clientInfo: { name: 'n8n-nodes-ya-ma', version: '0.5.0' },
          },
          1,
        );
        const version = hello.result?.protocolVersion;
        if (!version || ![protocolVersion, '2025-06-18', '2025-03-26'].includes(version))
          throw new Error('Unsupported MCP version');
        await rpc('notifications/initialized', {}, undefined, version);
        const result = (await rpc('tools/call', { name: tool, arguments: args }, 2, version))
          .result!;
        if (result.isError)
          throw new Error(result.content?.[0]?.text || 'Ya-Ma rejected the operation');
        // FastMCP returns separate text blocks for list results, including empty and singleton lists.
        const list = ['list_folders', 'read_messages', 'modify_messages', 'list_outbox'].includes(tool);
        const parts = () =>
          (result.content || []).map((part) =>
            part.type === 'text' && part.text ? JSON.parse(part.text) : part,
          );
        const value = list
          ? (result.structuredContent?.result ?? parts())
          : (result.structuredContent ?? parts()[0]);
        output.push({ json: { result: value }, pairedItem: { item: i } });
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Ya-Ma error';
        if (!this.continueOnFail())
          throw new NodeOperationError(this.getNode(), message, { itemIndex: i });
        output.push({ json: { error: message }, pairedItem: { item: i } });
      }
    }
    return [output];
  }
}
