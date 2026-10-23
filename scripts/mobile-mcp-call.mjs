// Local Mobile MCP client for emulator acceptance; never selects remote devices.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
const home = process.env.USERPROFILE || process.env.HOME;
const root = process.env.MOBILE_MCP_ROOT || path.join(home, '.codex', 'tools', 'mobile-mcp');
const require = createRequire(path.join(root, 'package.json'));
const {Client} = require('@modelcontextprotocol/sdk/client/index.js');
const {StdioClientTransport} = require('@modelcontextprotocol/sdk/client/stdio.js');
const entry = path.join(root, 'node_modules', '@mobilenext', 'mobile-mcp', 'lib', 'index.js');
const env = {...process.env, ANDROID_HOME: process.env.ANDROID_HOME || 'E:\\Tools\\Android\\sdk',
  ANDROID_AVD_HOME: process.env.ANDROID_AVD_HOME || 'E:\\Tools\\Android\\avd',
  MOBILEMCP_DISABLE_TELEMETRY: '1', MOBILEMCP_LEGACY_ROBOT: '1'};
const transport = new StdioClientTransport({command: process.execPath, args: [entry], env, stderr: 'pipe'});
transport.stderr?.on('data', chunk => process.stderr.write(chunk));
const client = new Client({name: 'invoice-android-acceptance', version: '0.1.0'});
try {
  await client.connect(transport);
  const [tool, argument = '{}', output] = process.argv.slice(2);
  if (!tool || tool === 'list') {
    console.log(JSON.stringify(await client.listTools(), null, 2));
  } else {
    const result = await client.callTool({name: tool, arguments: JSON.parse(argument)}, undefined, {timeout: 90000});
    for (const block of result.content || []) {
      if (block.type === 'image' && output) {
        fs.mkdirSync(path.dirname(path.resolve(output)), {recursive: true});
        fs.writeFileSync(output, Buffer.from(block.data, 'base64'));
        delete block.data; block.savedTo = path.resolve(output);
      } else if (block.type === 'image') { delete block.data; block.omitted = true; }
    }
    console.log(JSON.stringify(result, null, 2));
    if (result.isError) process.exitCode = 1;
  }
} finally { await client.close(); }
