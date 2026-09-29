(() => {
  'use strict';
  const $=id=>document.getElementById(id), project=new URL(location.href).searchParams.get('project')||'';
  const selected=new Set(),watchedJobs=new Set();let data=null,token='',timer=null,editing=null,loading=false,noticeTimer=null,noticeDelay=5000,jobTimer=null,finishedJob='';
  function el(tag,text='',cls=''){const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;return node;}
  function hideNotice(){clearTimeout(noticeTimer);$('notice').hidden=true;}
  function scheduleNotice(){clearTimeout(noticeTimer);const target=$('notice');if(noticeDelay&&!target.hidden&&!target.matches(':hover')&&!target.contains(document.activeElement))noticeTimer=setTimeout(hideNotice,noticeDelay);}
  function tell(message,error=false,delay=5000){const target=$(error?'error':'notice');if(error){target.textContent=message;target.hidden=false;return;}clearTimeout(noticeTimer);noticeDelay=delay;const close=el('button','','feedback-close');close.type='button';close.setAttribute('aria-label','关闭提示');close.onclick=hideNotice;target.replaceChildren(el('span',message),close);target.hidden=false;scheduleNotice();}
  $('notice').onmouseenter=()=>clearTimeout(noticeTimer);$('notice').onmouseleave=scheduleNotice;$('notice').addEventListener('focusin',()=>clearTimeout(noticeTimer));$('notice').addEventListener('focusout',()=>queueMicrotask(scheduleNotice));
  const jobCounts=job=>`新增 ${job.added} · 重复 ${job.duplicates} · 未完成项 ${job.issues.length}`;
  function renderIssues(ul,issues){ul.replaceChildren();for(const issue of issues)ul.append(el('li',(typeof issue.item==='string'?issue.item:issue.item?.sent_at||'图片')+'：'+issue.message));}
  function hideJob(){clearTimeout(jobTimer);if(finishedJob)watchedJobs.delete(finishedJob);finishedJob='';$('job').hidden=true;}
  function scheduleJob(){clearTimeout(jobTimer);if(finishedJob&&!$('job').matches(':hover')&&!$('job').contains(document.activeElement))jobTimer=setTimeout(hideJob,8000);}
  function renderJobs(){
    for(const job of data.jobs)if(job.state==='running')watchedJobs.add(job.id);
    const active=data.jobs.find(job=>job.state==='running'),job=active||data.jobs.find(job=>watchedJobs.has(job.id));
    $('job').hidden=!job;
    if(job){
      $('jobMessage').textContent=job.message;$('jobProgress').hidden=!active;$('jobProgress').max=job.total||1;$('jobProgress').value=job.done||0;$('jobCounts').textContent=jobCounts(job);$('jobIssues').hidden=!job.issues.length;renderIssues($('jobIssues').querySelector('ul'),job.issues);$('closeJob').hidden=!!active;
      if(active){clearTimeout(jobTimer);if(finishedJob)watchedJobs.delete(finishedJob);finishedJob='';}else if(finishedJob!==job.id){if(finishedJob)watchedJobs.delete(finishedJob);finishedJob=job.id;scheduleJob();}
    }
    const history=data.jobs.filter(item=>item.state!=='running'),failures=history.filter(item=>item.issues.length||['failed','interrupted','partial'].includes(item.state));
    $('jobHistory').hidden=!history.length;$('jobHistoryLabel').textContent='最近处理记录'+(failures.length?` · ${failures.length}次有未完成项`:'');
    const list=$('jobHistoryList');list.replaceChildren();const labels={folder:'读取文件夹',wechat:'读取微信群',recognize:'识别照片'};
    for(const item of history){const row=el('article','','job-record');row.append(el('strong',(labels[item.action]||'照片处理')+' · '+item.message),el('p',jobCounts(item)));if(item.issues.length){const details=el('details'),ul=el('ul');details.append(el('summary','查看未完成项'),ul);renderIssues(ul,item.issues);row.append(details);}list.append(row);}
  }
  $('closeJob').onclick=hideJob;
  $('job').onmouseenter=()=>clearTimeout(jobTimer);$('job').onmouseleave=scheduleJob;$('job').addEventListener('focusin',()=>clearTimeout(jobTimer));$('job').addEventListener('focusout',()=>queueMicrotask(scheduleJob));
  async function request(action,body){const response=await fetch(body?'/api/photos/'+action:'/api/photos?project='+encodeURIComponent(project),body?{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':token},body:JSON.stringify({...body,project_id:project})}:{});const value=await response.json();if(!response.ok)throw Error(value.error||'操作未完成');return value;}
  function visible(){return(data?.photos||[]).filter(p=>(!$('stateFilter').value||p.state===$('stateFilter').value)&&(!$('kindFilter').value||p.fields.kind===$('kindFilter').value)&&(!$('placeFilter').value||p.fields.place===$('placeFilter').value)&&(!$('dateFilter').value||p.fields.captured_date===$('dateFilter').value));}
  function imageURL(p){return '/photo-file/'+encodeURIComponent(project)+'/'+encodeURIComponent(p.id);}
  function selectStatus(){const rows=visible();$('selectedCount').textContent=`已选${selected.size}张`;$('selectAll').checked=!!rows.length&&rows.every(p=>selected.has(p.id));$('selectAll').indeterminate=rows.some(p=>selected.has(p.id))&&!$('selectAll').checked;$('recognize').disabled=!selected.size||data?.jobs.some(j=>j.state==='running');}
  function render(){
    const brief=data.briefs||{enabled:false,latest:null};$('sendBriefPreview').disabled=!brief.enabled;
    $('reviewFiles').replaceChildren();for(const file of brief.latest?.files||[]){const a=el('a',file.name);a.href=file.url;a.target='_blank';a.rel='noopener';$('reviewFiles').append(a,document.createElement('br'));}
    $('briefStatus').textContent=(brief.enabled?'已启用 · 发送至已绑定的本人飞书':'未启用飞书简报')+(brief.latest?' · '+brief.latest.label+(brief.latest.error?'：'+brief.latest.error:''):'');
    if(brief.preparing)$('briefStatus').textContent='正在读取或准备审阅材料，完成后自动发送。';
    const saved=data.photos.filter(p=>p.state==='saved').length;$('summary').textContent=`${data.photos.length}张照片 · ${data.photos.length-saved}张待核对 · ${saved}张已保存`;
    const list=$('photoList');list.replaceChildren();const rows=visible();
    if(!rows.length)list.append(el('p',data.photos.length?'此筛选下没有照片。':'本项目尚无照片。导入文件、读取文件夹或选择微信群开始。','help'));
    for(const p of rows){const card=el('article','','card');card.dataset.photoId=p.id;const label=el('label'),check=document.createElement('input');check.type='checkbox';check.checked=selected.has(p.id);check.setAttribute('aria-label','选择 '+p.name);check.onchange=()=>{check.checked?selected.add(p.id):selected.delete(p.id);selectStatus();};label.append(check,document.createTextNode(p.fields.label||p.name));
      const img=document.createElement('img');img.src=imageURL(p);img.alt=p.fields.label||p.name;img.loading='lazy';img.onclick=()=>open(p.id);
      card.append(label,img,el('p',[p.fields.kind||'待分类',p.phase?.name,p.fields.movement,p.fields.place,p.fields.plate].filter(Boolean).join(' · ')),el('small',[p.fields.captured_date||'拍摄日期待核对',p.fields.captured_time].filter(Boolean).join(' ')),el('p',p.quality+' · '+p.dimensions.join('×'),'quality'),el('span',p.state==='saved'?(p.saved_by==='automatic'?'自动分类并保存':'已核对并保存'):'待核对','status'));
      const button=el('button',p.state==='saved'?'查看 / 调整分类':'核对与分类');button.onclick=()=>open(p.id);card.append(button);if(p.state==='pending'&&p.warnings.length)card.append(el('p',p.warnings.join('；'),'help'));list.append(card);
    }
    renderJobs();
    $('classificationStatus').textContent=data.settings.auto_classify?'已开启自动分类：新照片获取后提取文字、Jev判断并保存；不明确的列为待核对。':'当前使用手动分类；可在采集与归档设置中开启自动分类。';const active=data.jobs.some(j=>j.state==='running');document.querySelectorAll('[data-job]').forEach(b=>b.disabled=active);selectStatus();
    $('readBrief').disabled=active||!brief.enabled;
    clearTimeout(timer);if(active||brief.preparing||['pending','sending','retry'].includes(brief.latest?.state))timer=setTimeout(()=>load(false),active?1000:3000);
  }
  async function load(settings=false){if(loading)return;loading=true;try{data=await request();token=data.token;for(const id of selected)if(!data.photos.some(p=>p.id===id))selected.delete(id);if(settings){$('autoClassify').checked=!!data.settings.auto_classify;$('projectLocation').value=data.settings.project_location||'';$('outputRoot').value=data.settings.output_root;$('sourceFolder').value=data.settings.output_root;$('account').value=data.settings.account;$('groups').value=data.settings.groups.map(g=>g.name+' | '+g.id).join('\n');$('sourceGroup').replaceChildren();for(const group of data.settings.groups){const opt=el('option',group.name);opt.value=group.id;$('sourceGroup').append(opt);}$('readerStatus').textContent=data.reader_ready?'本机读取器已配置，点击读取时核验当前微信账号。':'本机微信读取器尚未配置；可以先读取文件夹或导入照片。';}render();}catch(e){tell(e.message,true);}finally{loading=false;}}
  function formFields(){return Object.fromEntries(new FormData($('reviewForm')));}
  function toggleFields(){const kind=$('reviewForm').elements.namedItem('kind').value;$('vehicleFields').hidden=kind!=='车辆';$('personFields').hidden=kind!=='人员';}
  function fill(fields){for(const [name,value]of Object.entries(fields)){const input=$('reviewForm').elements.namedItem(name);if(input)input.value=value??'';}toggleFields();}
  function open(id){editing=data.photos.find(p=>p.id===id);if(!editing)return;$('reviewForm').reset();fill(editing.fields);$('confirmed').checked=!!editing.reviewed&&editing.saved_by!=='automatic';$('projectPhase').textContent='项目阶段：'+(editing.phase?.name||'待判定')+' · '+(editing.phase?.reason||'按拍摄时间和项目排期计算');$('reviewError').hidden=true;$('preview').src=imageURL(editing);$('fullImage').href=imageURL(editing);$('imageQuality').textContent=editing.quality+' · '+editing.dimensions.join('×');$('imageSource').textContent=editing.sources.map(s=>[s.type,s.group_name,s.sent_at,s.relative||s.name||s.file].filter(Boolean).join(' · ')).join('\n');$('ocrText').textContent=(editing.ocr_text||'尚未识别；关闭后选中照片，点击“识别所选照片”。')+'\n'+editing.warnings.join('\n')+(editing.classification?.model?'\n分类模型：'+editing.classification.model+'\n'+Object.entries(editing.classification.answers).map(([k,v])=>k+'：'+v.choice+'（'+Math.round(v.confidence*100)+'%）').join('\n'):'');$('savedPath').textContent=editing.saved_path?'已保存：'+editing.saved_path:'保存后会写入当前项目的分类文件夹。';$('editor').showModal();}
  $('closeEditor').onclick=()=>$('editor').close();$('reviewForm').elements.namedItem('kind').onchange=toggleFields;
  $('briefSettings').addEventListener('toggle',()=>{if($('briefSettings').open){$('briefEnabled').checked=!!data?.briefs?.enabled;$('briefPDF').checked=!!data?.briefs?.include_pdf;if(!$('briefReadDate').value)$('briefReadDate').value=$('startDate').value;}});
  $('briefForm').onsubmit=async event=>{event.preventDefault();try{await request('brief-settings',{enabled:$('briefEnabled').checked,include_pdf:$('briefPDF').checked});$('error').hidden=true;tell('飞书简报设置已保存。');await load();}catch(e){tell(e.message,true);}};
  $('readBriefForm').onsubmit=event=>{event.preventDefault();job('brief-read',{start:$('briefReadDate').value,end:$('briefReadDate').value,group_ids:data.settings.groups.map(g=>g.id)});};
  $('sendBriefPreview').onclick=async()=>{$('sendBriefPreview').disabled=true;try{await request('brief-preview',{});$('error').hidden=true;tell('试用简报已加入发送队列，可在此查看送达状态。');await load();}catch(e){tell(e.message,true);$('sendBriefPreview').disabled=!data?.briefs?.enabled;}};
  $('applySuggestions').onclick=()=>{fill(editing.suggestions);$('confirmed').checked=false;};
  $('reviewForm').onsubmit=async event=>{event.preventDefault();$('savePhoto').disabled=true;try{const result=await request('save',{id:editing.id,revision:editing.revision,fields:formFields(),confirmed:$('confirmed').checked});$('editor').close();tell('已保存到：'+result.saved_path);await load();}catch(e){$('reviewError').textContent=e.message;$('reviewError').hidden=false;}finally{$('savePhoto').disabled=false;}};
  $('settingsForm').onsubmit=async event=>{event.preventDefault();try{const groups=$('groups').value.split('\n').filter(s=>s.trim()).map(line=>{const parts=line.split('|');if(parts.length!==2)throw Error('每行请填写：群名 | 稳定群ID');return{name:parts[0].trim(),id:parts[1].trim()};});await request('settings',{output_root:$('outputRoot').value,account:$('account').value,groups,auto_classify:$('autoClassify').checked,project_location:$('projectLocation').value});tell('保存目录和来源设置已保存。');await load(true);}catch(e){tell(e.message,true);}};
  async function job(action,values){$('error').hidden=true;try{const started=await request(action,values);watchedJobs.add(started.id);await load();}catch(e){tell(e.message,true);}}
  $('folderForm').onsubmit=event=>{event.preventDefault();job('folder',{path:$('sourceFolder').value});};
  $('wechatForm').onsubmit=event=>{event.preventDefault();job('wechat',Object.fromEntries(new FormData(event.currentTarget)));};
  $('recognize').onclick=()=>job('recognize',{ids:[...selected]});
  $('uploads').onchange=async event=>{const files=[...event.target.files];if(files.length>500){tell('单次最多500张照片',true);return;}$('error').hidden=true;event.target.disabled=true;let added=0,duplicates=0,failed=0;for(const file of files){tell('正在处理 '+file.name+(data.settings.auto_classify?'：OCR → Jev分类 → 保存…':''),false,0);try{if(file.size>20*1024*1024)throw Error('单张照片最多20MB');const raw=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(Error('文件读取失败'));reader.readAsDataURL(file);});const result=await request('receive',{name:file.name,data:raw});result.duplicate?duplicates++:added++;}catch(e){failed++;tell(file.name+'：'+e.message,true);}tell(`导入中：新增 ${added} · 重复 ${duplicates} · 失败 ${failed}`,false,0);}event.target.disabled=false;event.target.value='';tell(`导入完成：新增 ${added} · 重复 ${duplicates} · 失败 ${failed}。明确的自动保存，待核对项可打开修正或重新识别。`);await load();};
  $('selectAll').onchange=()=>{for(const p of visible())$('selectAll').checked?selected.add(p.id):selected.delete(p.id);render();};
  $('clearSelection').onclick=()=>{selected.clear();render();};
  for(const id of ['stateFilter','kindFilter','placeFilter','dateFilter'])$(id).onchange=render;
  $('clearFilters').onclick=()=>{for(const id of ['stateFilter','kindFilter','placeFilter','dateFilter'])$(id).value='';render();};
  $('refresh').onclick=()=>{$('error').hidden=true;load(true);};
  const now=new Date(),today=[now.getFullYear(),String(now.getMonth()+1).padStart(2,'0'),String(now.getDate()).padStart(2,'0')].join('-');$('startDate').value=today;$('endDate').value=today;
  load(true);
})();
