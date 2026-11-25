import type { IAuthenticateGeneric, ICredentialType, INodeProperties } from 'n8n-workflow';

export class YaMaApi implements ICredentialType {
  name = 'yaMaApi';
  displayName = 'Ya-Ma Gateway';
  documentationUrl = 'https://github.com/grebennikovxyz/ya-mail-plugin#readme';
  properties: INodeProperties[] = [
    {
      displayName: 'Gateway URL',
      name: 'url',
      type: 'string',
      default: '',
      required: true,
      placeholder: 'https://mail.example.org/mcp',
    },
    {
      displayName: 'Gateway Token',
      name: 'token',
      type: 'string',
      typeOptions: { password: true },
      default: '',
      required: true,
      description: 'Bearer token configured on the Ya-Ma gateway (YA_MAIL_MCP_TOKEN)',
    },
  ];
  authenticate: IAuthenticateGeneric = {
    type: 'generic',
    properties: { headers: { Authorization: '=Bearer {{$credentials.token}}' } },
  };
}
