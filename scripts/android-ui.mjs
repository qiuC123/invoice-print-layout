// Observed native UI actions over the installed Mobile MCP (local emulator only).
import {createRequire} from 'node:module';
import fs from 'node:fs';import path from 'node:path';
import {execFileSync} from 'node:child_process';
const root=process.env.MOBILE_MCP_ROOT||path.join(process.env.USERPROFILE||process.env.HOME,'.codex','tools','mobile-mcp');
const require=createRequire(path.join(root,'package.json'));
const {Client}=require('@modelcontextprotocol/sdk/client/index.js');
const {StdioClientTransport}=require('@modelcontextprotocol/sdk/client/stdio.js');
const device='emulator-5560';
const transport=new StdioClientTransport({command:process.execPath,args:[path.join(root,'node_modules','@mobilenext','mobile-mcp','lib','index.js')],env:{...process.env,ANDROID_HOME:process.env.ANDROID_HOME||'E:\\Tools\\Android\\sdk',ANDROID_AVD_HOME:'E:\\Tools\\Android\\avd',MOBILEMCP_DISABLE_TELEMETRY:'1',MOBILEMCP_LEGACY_ROBOT:'1'},stderr:'pipe'});
const client=new Client({name:'invoice-native-ui',version:'0.1.0'});const trace=[];
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function call(name,args={}){const result=await client.callTool({name,arguments:{device,...args}},undefined,{timeout:90000});if(result.isError)throw Error(JSON.stringify(result));return result;}
async function elements(){const result=await call('mobile_list_elements_on_screen',{format:'json'});const text=result.content.filter(x=>x.type==='text').map(x=>x.text).join('\n');const start=text.indexOf('[');if(start<0)throw Error(text);return JSON.parse(text.slice(start));}
async function find(step){const deadline=Date.now()+20000;do{const found=(await elements()).filter(e=>(!step.text||e.text===step.text||e.label===step.text)&&(!step.contains||e.text?.includes(step.contains)||e.label?.includes(step.contains))&&(!step.type||e.type===step.type||e.type.split('.').at(-1)===step.type));if(found.length>(step.index||0))return found[step.index||0];await pause(400);}while(Date.now()<deadline);throw Error('UI element not found: '+JSON.stringify(step));}
try{
 await client.connect(transport);const steps=JSON.parse(fs.readFileSync(process.argv[2],'utf8').replace(/^\uFEFF/,''));
 for(const step of steps){
  if(step.action==='click'){const e=await find(step),r=e.coordinates;await call('mobile_click_on_screen_at_coordinates',{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});trace.push({step,observed:e});}
  else if(step.action==='longPress'){const e=await find(step),r=e.coordinates;await call('mobile_long_press_on_screen_at_coordinates',{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),duration:700});trace.push({step,observed:e});}
  else if(step.action==='wait'){trace.push({step,observed:await find(step)});}
  else if(step.action==='type'){await call('mobile_type_keys',{text:step.value});trace.push({step});}
  else if(step.action==='clear'){const adb=path.join(process.env.ANDROID_HOME||'E:\\Tools\\Android\\sdk','platform-tools','adb.exe');execFileSync(adb,['-s',device,'shell','input','keycombination','113','29']);execFileSync(adb,['-s',device,'shell','input','keyevent','67']);trace.push({step,method:'focused native input Ctrl+A / Delete'});}
  else if(step.action==='key'){await call('mobile_press_button',{button:step.button});trace.push({step});}
  else if(step.action==='tool'){await call(step.name,step.arguments);trace.push({step});}
  else if(step.action==='dump'){const current=await elements();console.log(JSON.stringify(current,null,2));trace.push({step,elements:current});}
  else if(step.action==='screenshot'){const result=await call('mobile_take_screenshot',{});const block=result.content.find(x=>x.type==='image');if(!block)throw Error('No screenshot');fs.writeFileSync(step.path,Buffer.from(block.data,'base64'));trace.push({step});}
  else throw Error('Unknown UI step');
  await pause(250);
 }
 console.log(JSON.stringify({completed:steps.length}));
}finally{
 fs.writeFileSync(process.argv[2]+'.trace.json',JSON.stringify(trace,null,2));
 // Keep the event loop alive through SDK close when the stdio child exits first.
 let timer;try{await Promise.race([client.close(),new Promise(resolve=>{timer=setTimeout(resolve,1500);})]);}finally{clearTimeout(timer);}
}
