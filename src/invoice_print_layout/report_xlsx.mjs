// Template-based report. Uses a configured local artifact-tool runtime, no network.
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const [payloadPath, modulesPath]=process.argv.slice(2);
const require=createRequire(pathToFileURL(modulesPath+'/../package.json'));
const {FileBlob,SpreadsheetFile}=await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const data=JSON.parse(await fs.readFile(payloadPath,'utf8'));
const book=await SpreadsheetFile.importXlsx(await FileBlob.load(data.template));
const sheet=book.worksheets.getItemAt(0);
const mainName=sheet.name;
if(sheet.getRange('A1').values[0][0]!=='活动运营费用统计表')throw Error('不支持的报销模板');
// Clear the fill-in cells, including empty strings which some importers read as zero.
for(const range of ['C8:M34','D35:M35','A5','A35','B36','E36','H36','L36'])sheet.getRange(range).clear({applyTo:'contents'});
sheet.getRange('D2').values=[['项目名称：'+data.project]];
sheet.getRange('A4').values=[['此表格报销区间日期：'+data.period+'；报销人：'+(data.person||'待填写')]];
const detail=book.worksheets.add('报销明细');
detail.showGridLines=false;
const headers=['序号','事项编号','项目','消费日期','类别','事项／用途','商家','金额（元）','凭证方式','付款来源','PDF页码','订单号'];
detail.getRange('A1:L1').values=[headers];
const literal=v=>typeof v==='string'&&v.startsWith('=')?"'"+v:v;
// Identifiers are text, never floating-point numbers (orders exceed Excel's 15 digits).
for(const col of ['B','K','L'])detail.getRange(`${col}2:${col}${data.items.length+1}`).setNumberFormat('@');
const rows=data.items.map((x,i)=>[i+1,x.id,x.project,x.expense_date?new Date(x.expense_date+'T00:00:00Z'):null,x.category,x.title,x.merchant,x.amount_cents/100,x.material_basis==='payment_only'?'仅支付凭证（已确认）':x.alternative?'扣费记录替代发票':'发票',data.payment_label,x.pages,x.order_number].map(literal));
detail.getRange(`A2:L${rows.length+1}`).values=rows;
for(const col of ['B','K','L'])detail.getRange(`${col}2:${col}${data.items.length+1}`).setNumberFormat('@');
for(let i=0;i<data.items.length;i++){
  if(String(detail.getRange(`L${i+2}`).values[0][0]??'')!==String(data.items[i].order_number??''))throw Error('Excel订单号未完整保留');
}
detail.getRange(`A1:L${rows.length+2}`).format.font={name:'Microsoft YaHei',size:10};
detail.getRange('A1:L1').format={fill:'#E9EDF2',font:{bold:true},rowHeight:28};
detail.getRange(`A2:L${rows.length+1}`).format.rowHeight=44;
detail.getRange(`A2:L${rows.length+1}`).format.verticalAlignment='center';
for(const [col,width] of Object.entries({A:6,B:15,C:20,D:13,E:12,F:35,G:27,H:14,I:20,J:24,K:16,L:35}))detail.getRange(`${col}1:${col}${rows.length+2}`).format.columnWidth=width;
detail.getRange(`C2:C${rows.length+1}`).format.wrapText=true;
detail.getRange(`F2:G${rows.length+1}`).format.wrapText=true;
detail.getRange(`I2:L${rows.length+1}`).format.wrapText=true;
detail.getRange(`D2:D${rows.length+1}`).setNumberFormat('yyyy-mm-dd');
detail.getRange(`H2:H${rows.length+2}`).setNumberFormat('0.00');
detail.getRange(`G${rows.length+2}`).values=[['合计']];
detail.getRange(`H${rows.length+2}`).formulas=[[`=SUM(H2:H${rows.length+1})`]];
detail.freezePanes.freezeRows(1);
for(const group of data.snapshot.rows){
  const r=group.row;
  sheet.getRange(`H${r}`).formulas=[['=SUM('+group.items.map(x=>`'报销明细'!H${x.detail_row}`).join(',')+')']];
  if(r>=24)sheet.getRange(`B${r}`).values=[[literal(group.name)]];
}
sheet.getRange('H8:H35').setNumberFormat('0.00');
sheet.getRange('H35').formulas=[['=SUM(H8:H34)']];
book.recalculate();
const total=sheet.getRange('H35').values[0][0];
const detailTotal=detail.getRange(`H${rows.length+2}`).values[0][0];
if(Math.round(Number(total)*100)!==data.total_cents||Math.round(Number(detailTotal)*100)!==data.total_cents)throw Error('Excel金额合计不一致');
const scan=await book.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!',options:{useRegex:true,maxResults:10},maxChars:1500});
if(/"kind":"match"/.test(scan.ndjson))throw Error('Excel存在公式错误');
await (await SpreadsheetFile.exportXlsx(book)).save(data.output);
if(data.preview){
  for(const [name,range,suffix] of [[mainName,'A1:M36','summary'],['报销明细',`A1:L${Math.min(rows.length+2,12)}`,'detail']]){
    const blob=await book.render({sheetName:name,range,scale:1,format:'png'});
    await fs.writeFile(data.preview+'-'+suffix+'.png',new Uint8Array(await blob.arrayBuffer()));
  }
}
console.log(JSON.stringify({total_cents:data.total_cents,rows:rows.length,sheet:mainName}));
