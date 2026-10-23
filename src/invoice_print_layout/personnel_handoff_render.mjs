import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
import vm from 'node:vm';

const [root, artifactModule] = process.argv.slice(2);
const { Workbook, SpreadsheetFile, FileBlob } = await import(pathToFileURL(artifactModule).href);
const data = JSON.parse(await fs.readFile(path.join(root, 'time-records.json'), 'utf8'));
const qa = path.join(root, 'QA'); await fs.mkdir(qa, { recursive: true });
const wb = Workbook.create();
const summary = wb.worksheets.add('岗位时间');
const detail = wb.worksheets.add('照片记录');
const roles = [...new Set(data.records.map(r => r.role))];
const serial = s => Date.parse(s + 'T00:00:00Z') / 86400000 + 25569;
const clock = s => { const [h,m,sec=0] = s.split(':').map(Number); return (h*3600+m*60+sec)/86400; };
const sentValue = s => s ? (Date.parse(s)+8*3600000)/86400000+25569 : null;
const last = data.records.length + 6;
function style(sheet, lastColumn, lastRow, title, widths) {
  sheet.showGridLines = false;
  const all = sheet.getRange(`A1:${lastColumn}${lastRow}`);
  all.format.font = {name:'Arial',size:10,color:'#243B53'};
  all.format.verticalAlignment = 'center'; all.format.rowHeight = 28;
  sheet.getRange('A2').values = [[title]];
  sheet.getRange('A2').format.font = {name:'Arial',size:16,bold:true,color:'#243B53'};
  sheet.getRange('A3').values = [[data.project_title + ' · ' + data.batch_id]];
  sheet.getRange('A4').values = [['照片张数不等于人数；拍照跨度不作为个人工时。']];
  widths.forEach((width,i) => { const c=String.fromCharCode(65+i); sheet.getRange(`${c}:${c}`).format.columnWidthPx=width; });
  sheet.freezePanes.freezeRows(6); sheet.freezePanes.freezeColumns(1);
}
function header(sheet, range) {
  sheet.getRange(range).format = {fill:'#243B53',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},wrapText:true,rowHeight:40,verticalAlignment:'center'};
}
function table(sheet, range, name) { const t=sheet.tables.add(range,true,name); t.showFilterButton=true; }
style(detail,'L',last,'逐张岗位与拍摄时间',[148,112,90,140,114,120,180,240,220,240,290,150]);
detail.getRange('A6:L6').values = [['照片ID','拍摄日期','水印时间','岗位','项目归属','分类状态','发群时间','地点','岗位依据','用户补充','照片相对路径','原交接岗位']];
const matrix = data.records.map(r => [r.photo_id,serial(r.date),clock(r.photo_time),r.role,r.project_status_label,
  r.role_status === 'user_confirmed' ? '用户确认' : r.role_status === 'pending' ? '待确认' : '交接标注',sentValue(r.sent_at),r.location||null,r.role_basis,r.user_note||null,r.path,r.source_role]);
detail.getRange(`A7:L${last}`).values = matrix;
detail.getRange(`B7:B${last}`).setNumberFormat('yyyy-mm-dd');
detail.getRange(`C7:C${last}`).setNumberFormat('hh:mm:ss');
detail.getRange(`G7:G${last}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
detail.getRange(`G7:L${last}`).format.wrapText=true;
detail.getRange(`A7:L${last}`).format.rowHeight=62;
table(detail,`A6:L${last}`,'PhotoRecords'); header(detail,'A6:L6');
const start = roles.length+10, groupLast=start+data.groups.length;
style(summary,'F',groupLast,'岗位照片时间汇总',[120,155,135,115,450,150]);
summary.getRange('A6:E6').values = [['岗位','全部照片（张）','当前项目（张）','归属待核（张）','异地排除（张）']]; header(summary,'A6:E6');
roles.forEach((role,i) => {
  const row=i+7; summary.getRange(`A${row}`).values=[[role]];
  summary.getRange(`B${row}:E${row}`).formulas=[[
    `=COUNTIF('照片记录'!$D$7:$D$${last},A${row})`,
    ...['当前项目','归属待核','异地排除'].map(status=>`=COUNTIFS('照片记录'!$D$7:$D$${last},A${row},'照片记录'!$E$7:$E$${last},"${status}")`)
  ]];
});
summary.getRange(`A${start}:F${start}`).values=[['拍摄日期','岗位','项目归属','照片（张）','拍摄时间 → 照片ID','记录说明']]; header(summary,`A${start}:F${start}`);
const groups=data.groups.map(g=>[serial(g.date),g.role,g.project_status_label,g.photo_ids.length,g.photo_ids.map((id,i)=>`${g.times[i]} · ${id}`).join('\n'),'照片事件时间']);
summary.getRange(`A${start+1}:F${groupLast}`).values=groups;
summary.getRange(`A${start+1}:A${groupLast}`).setNumberFormat('yyyy-mm-dd');
summary.getRange(`E${start+1}:F${groupLast}`).format.wrapText=true;
data.groups.forEach((g,i)=>{ const row=start+i+1; summary.getRange(`A${row}:F${row}`).format.rowHeight=Math.max(42,22*g.photo_ids.length+12); if(g.project_status!=='included') summary.getRange(`C${row}`).format.fill='#FFF1D6'; });
table(summary,`A${start}:F${groupLast}`,'RoleDateTimes');
summary.freezePanes.freezeRows(start);
summary.getRange(`A${roles.length+8}`).values=[['来源：inputs/catalog.json、classifications.csv与user-corrections.json（本次冻结副本）。']];
wb.recalculate();
const counts=roles.map(role=>data.records.filter(r=>r.role===role).length);
assert.deepEqual(summary.getRange(`B7:B${roles.length+6}`).values.flat(),counts);
const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:50},summary:'formula validation'});
assert(!/#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A|#NUM!|#NULL!|#SPILL!|#CALC!/.test(errors.ndjson),errors.ndjson);
const xlsx=path.join(root,'time-records.xlsx'); await (await SpreadsheetFile.exportXlsx(wb)).save(xlsx);
const restored=await SpreadsheetFile.importXlsx(await FileBlob.load(xlsx)); restored.recalculate();
assert.deepEqual(restored.worksheets.getItem('照片记录').getRange(`A7:L${last}`).values,matrix);
assert.deepEqual(restored.worksheets.getItem('岗位时间').getRange(`A${start+1}:F${groupLast}`).values,groups);
assert.deepEqual(restored.worksheets.getItem('岗位时间').getRange(`B7:B${roles.length+6}`).values.flat(),counts);
for(const [sheetName,range,name] of [['岗位时间',`A1:F${groupLast}`,'岗位时间'],['照片记录',`A1:F${Math.min(last,18)}`,'照片记录'],['照片记录',`G6:L${Math.min(last,18)}`,'逐张依据']]) {
  const rendered=await restored.render({sheetName,range,scale:1.25,format:'png'});
  await fs.writeFile(path.join(qa,name+'.png'),new Uint8Array(await rendered.arrayBuffer()));
}
// Test the actual preview filter function without opening or operating a browser.
const preview=await fs.readFile(path.join(root,'preview.html'),'utf8');
const script=preview.match(/<script>([\s\S]*?)<\/script>/)[1]; new vm.Script(script);
const filterCode=script.split('// FILTERS_BEGIN')[1].split('// FILTERS_END')[0];
const context={}; vm.createContext(context); vm.runInContext(filterCode,context);
const select=filters=>context.selectRows(data.records,filters);
assert.equal(select({}).length,data.records.length);
let checked=0;
for(const field of ['date','period','role','project_status']) {
  for(const value of new Set(data.records.map(r=>r[field]))) {
    const filtered=select({[field==='project_status'?'project':field]:value});
    assert.deepEqual(Array.from(filtered,r=>r.photo_id),data.records.filter(r=>r[field]===value).map(r=>r.photo_id)); checked++;
  }
}
assert.equal(select({date:'no-such-date'}).length,0);
for(const r of data.records) assert(await fs.stat(path.join(root,r.path)));
await fs.writeFile(path.join(qa,'workbook-check.json'),JSON.stringify({photo_count:matrix.length,group_count:groups.length,export_reimport_equal:true,role_count_formulas_equal:true,formula_errors:0,rendered_views:3},null,2));
await fs.writeFile(path.join(qa,'preview-check.json'),JSON.stringify({javascript_syntax:true,filter_function_checks:checked+2,image_paths:data.records.length,live_browser_review:false},null,2));
console.log(JSON.stringify({xlsx,photo_count:matrix.length,groups:groups.length}));
