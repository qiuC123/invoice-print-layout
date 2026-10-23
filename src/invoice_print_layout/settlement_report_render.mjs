// Author only. Python owns native processes and the shared finalization boundary.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const jobPath = process.argv[2];
if (!jobPath) throw new Error('Usage: settlement_report_render.mjs JOB.json');
const job = JSON.parse((await fs.readFile(jobPath, 'utf8')).replace(/^\uFEFF/, ''));
const {FileBlob, PresentationFile} = await import(pathToFileURL(job.runtime.artifact_module).href);
const build = path.join(job.run_dir, 'build');
const candidate = path.join(build, 'authored.pptx');
try { await fs.access(candidate); throw new Error('Authored candidate exists; create a fresh attempt'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const deck = await PresentationFile.importPptx(await FileBlob.load(job.template));
const inspected = (await deck.inspect({kind:'slide,textbox,shape,image,table', maxChars:4000000})).ndjson.split('\n').filter(Boolean).map(JSON.parse);
await fs.writeFile(path.join(build, 'template-inspect.ndjson'), inspected.map(r=>JSON.stringify(r)).join('\n'));
// Explicit, checked anchors are edited before duplicating template pages.
for (const instruction of [...job.edits, ...job.texts]) {
  const element = deck.resolve(instruction.id);
  if (!element?.text) throw new Error(`Missing editable text anchor: ${instruction.id}`);
  if (instruction.old !== undefined) {
    const oldText = String(element.text);
    if (!instruction.old || oldText.split(instruction.old).length !== 2) throw new Error(`Anchor old text must match exactly once: ${instruction.id}`);
    element.text.replace(instruction.old, instruction.text);
  } else element.text = instruction.text;
}
const original = [...deck.slides.items], used = new Set();
const chosen = job.slides.map(spec=>{
  const source = original[spec.template-1];
  if (!source) throw new Error('Template slide no longer exists');
  if (used.has(spec.template)) return source.duplicate();
  used.add(spec.template); return source;
});
chosen.forEach((slide,index)=>slide.moveTo(index));
const width = job.slide_size_emu[0]/9525, height = job.slide_size_emu[1]/9525;
const layout = job.settings.layout ?? {};
const margin = layout.margin ?? width/32;
const bottom = layout.bottom ?? height-39;
const availableWidth = width-2*margin;
const font = job.settings.font_family;
const layouts = [];

function text(slide, words, x, y, w, h, size=14, align='left', color='#222222', bold=false) {
  const shape = slide.shapes.add({geometry:'textbox', name:String(words).slice(0,45), position:{left:x,top:y,width:w,height:h}, fill:'none',line:{fill:'none',width:0}});
  shape.text = String(words);
  shape.text.style = {typeface:font,fontSize:size,color,bold,alignment:align,verticalAlignment:'middle',autoFit:'none',wrap:true};
}
async function image(slide, photo, x, y, w, h) {
  if (w<=0 || h<=0) throw new Error('Insufficient space for photo; split the manifest into more pages');
  const scale = Math.min(w/photo.size[0],h/photo.size[1]), iw=photo.size[0]*scale, ih=photo.size[1]*scale;
  slide.images.add({blob:await fs.readFile(photo.path),contentType:photo.path.toLowerCase().endsWith('.png')?'image/png':'image/jpeg',
    alt:`settlement-photo:${photo.photo_id}`,fit:'contain',position:{left:x+(w-iw)/2,top:y+(h-ih)/2,width:iw,height:ih}});
}
function columnsFor(photos, w, h, cap=7, maxRowHeight=270) {
  let best={columns:1,score:-1};
  for(let columns=1;columns<=Math.min(cap,Math.max(1,photos.length));columns++) {
    const rows=Math.ceil(photos.length/columns),cw=w/columns,rh=Math.min(maxRowHeight,h/Math.max(rows,1)), maxW=cw-16,maxH=rh-31;
    if(maxH<=0 || maxW<=0) continue;
    const area=photos.reduce((sum,p)=>{const scale=Math.min(maxW/p.size[0],maxH/p.size[1]);return sum+p.size[0]*p.size[1]*scale*scale;},0);
    const score=area*(1-(rows*columns-photos.length)/(rows*columns)*0.08);
    if(score>best.score) best={columns,score};
  }
  if(photos.length && best.score<0) throw new Error('Photos do not fit; split the manifest into more pages');
  return best.columns;
}
async function grid(slide, photos, x, y, w, h, maxRowHeight=270) {
  if(!photos.length) { text(slide,'暂无可明确对应的现场照片',x,y+24,w,44,21,'left','#777777'); return {columns:0,rows:0}; }
  const columns=columnsFor(photos,w,h,7,maxRowHeight),rows=Math.ceil(photos.length/columns),cw=w/columns,rh=Math.min(maxRowHeight,h/rows);
  for(let i=0;i<photos.length;i++) {
    const px=x+(i%columns)*cw,py=y+Math.floor(i/columns)*rh;
    await image(slide,photos[i],px+4,py,cw-16,rh-31);
    text(slide,photos[i].caption,px,py+rh-29,cw-8,27,15,'center');
  }
  return {columns,rows};
}
for(let index=0;index<chosen.length;index++) {
  const slide=chosen[index],spec=job.slides[index];
  if(['overview','items','groups'].includes(spec.kind)) {
    slide.shapes.deleteAll(); // Native tables are a separate collection and remain editable.
    if(spec.kind==='overview') {
      const split=width*0.65;
      const overviewHeight=layout.overview_height ?? Math.min(600,height-100);
      const geometry=await grid(slide,spec.photos,margin,70,split-margin,overviewHeight,overviewHeight);
      text(slide,spec.heading ?? job.values.overview_title ?? spec.title ?? job.title,split+28,112,width-split-2*margin,54,30);
      const stamp=spec.date_label ?? job.values.overview_date ?? '';
      if(stamp) text(slide,stamp,split+28,180,width-split-2*margin,38,22);
      text(slide,spec.note ?? job.values.overview_note ?? '照片核对草稿\n实际验收待填写。',split+28,252,width-split-2*margin,height-290,20);
      layouts.push({slide:index+1,kind:spec.kind,...geometry});
    } else {
      const table = slide.tables.items[0];
      if(!table) throw new Error('Native template table was not imported');
      const header=job.settings.reference_quantity_header;
      if(header) table.cells.set(0,header.column,header.text);
      const info=inspected.find(record=>record.kind==='table' && record.slide===spec.template);
      if(!info?.bbox) throw new Error('Native table geometry missing from inspect');
      const titleY=info.bbox[1]+info.bbox[3]+13,top=titleY+35;
      text(slide,spec.title ?? '',margin,titleY,availableWidth-40,29,19,'left','#B85315',true);
      if(spec.kind==='items') {
        const geometry=await grid(slide,spec.photos,margin,top,availableWidth,bottom-top);
        layouts.push({slide:index+1,kind:spec.kind,photos:spec.photos.length,...geometry});
      } else {
        const groups=spec.groups,cols=spec.columns ?? Math.max(1,Math.ceil(Math.sqrt(groups.length)));
        if(!Number.isInteger(cols) || cols<1 || cols>Math.max(1,groups.length)) throw new Error('Invalid group columns');
        const rows=Math.ceil(groups.length/cols),cw=availableWidth/cols,rh=Math.min(310,(bottom-top)/Math.max(rows,1));
        for(let g=0;g<groups.length;g++) {
          const group=groups[g],gx=margin+(g%cols)*cw,gy=top+Math.floor(g/cols)*rh;
          text(slide,group.label,gx,gy,cw-15,25,16,'center','#222222',true);
          if(!group.photos.length) { text(slide,'缺少已确认照片',gx,gy+28,cw-15,32,14,'center','#777777'); continue; }
          const iw=(cw-16)/group.photos.length;
          for(let j=0;j<group.photos.length;j++) {
            const photo=group.photos[j],px=gx+j*iw;
            await image(slide,photo,px+3,gy+28,iw-6,rh-54);
            text(slide,photo.caption,px,gy+rh-23,iw-2,21,12,'center');
          }
        }
        layouts.push({slide:index+1,kind:spec.kind,groups:groups.length,columns:cols,rows});
      }
      if(spec.note) text(slide,spec.note,margin,bottom,availableWidth-40,19,11,'left','#666666');
      text(slide,job.values.footer ?? '模板规格及数量供核对，实际验收待填写',margin,height-18,availableWidth-180,16,10,'left','#777777');
      text(slide,String(index+1).padStart(2,'0'),width-90,height-20,50,18,11,'right','#777777');
    }
  }
  for(const instruction of (job.added_texts ?? []).filter(item=>item.template===spec.template)) {
    const p=instruction.position;
    text(slide,instruction.text,p.left,p.top,p.width,p.height,instruction.font_size??14,instruction.align??'left',instruction.color??'#222222',instruction.bold??false);
  }
  slide.speakerNotes.textFrame.setText('');
}
await fs.writeFile(path.join(build,'item-layouts.json'),JSON.stringify(layouts,null,2));
await fs.writeFile(path.join(build,'authored-inspect.ndjson'),(await deck.inspect({kind:'slide,textbox,shape,image,table',maxChars:4000000})).ndjson);
await (await PresentationFile.exportPptx(deck)).save(candidate);
console.log(JSON.stringify({authored:candidate,selected_pages:chosen.length,imported_pages:deck.slides.items.length}));
