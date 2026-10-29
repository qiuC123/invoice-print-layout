'use strict';
(() => {
 const menu=document.createElement('div');menu.id='projectMenu';menu.className='project-menu';menu.setAttribute('role','menu');menu.setAttribute('aria-label','项目操作');menu.hidden=true;document.body.append(menu);
 const trigger=document.createElement('button');trigger.id='projectActions';trigger.type='button';trigger.textContent='项目操作';trigger.setAttribute('aria-haspopup','menu');trigger.hidden=true;document.querySelector('header .actions').append(trigger);
 const compact=matchMedia('(max-width:980px)');
 function syncTrigger(){const project=projectContext();trigger.hidden=!project||!embeddedWorkbench&&!compact.matches;trigger.disabled=!project||busy;if(project)trigger.dataset.projectMenuId=project.id;else delete trigger.dataset.projectMenuId;}
 window.refreshProjectMenuTrigger=syncTrigger;compact.addEventListener('change',syncTrigger);
 let target=null,busy=false,lastX=0,lastY=0;
 const projectLink=id=>[...document.querySelectorAll('[data-project-menu-id]')].find(x=>x.dataset.projectMenuId===id&&x.getClientRects().length);
 const lock=value=>{busy=value;menu.setAttribute('aria-busy',String(value));syncTrigger();};
 const closeMenu=restore=>{const id=target?.id;menu.hidden=true;target=null;if(restore&&id)projectLink(id)?.focus();};
 function place(){const box=menu.getBoundingClientRect();menu.style.left=Math.max(8,Math.min(lastX,innerWidth-box.width-8))+'px';menu.style.top=Math.max(8,Math.min(lastY,innerHeight-box.height-8))+'px';}
 function open(link,x,y){
  if(busy)return;const project=state.projects.find(p=>p.id===link.dataset.projectMenuId);if(!project)return;
  target={...project};lastX=x;lastY=y;
  menu.innerHTML=[['rename','重命名'],['period','修改项目时间'],['pin',project.pinned?'取消置顶':'置顶'],['delete','永久删除'],['reimbursement',projectReimbursementStatus(project)==='reimbursed'?'移回未报销':'移到已报销']].map(([action,label])=>`<button type="button" role="menuitem" data-project-action="${action}">${label}</button>`).join('');
  menu.hidden=false;place();menu.querySelector('button').focus();
 }
 trigger.onclick=()=>{const box=trigger.getBoundingClientRect();open(trigger,box.left,box.bottom);};syncTrigger();
 document.addEventListener('contextmenu',event=>{const link=event.target.closest('[data-project-menu-id]');if(!link)return;event.preventDefault();open(link,event.clientX,event.clientY);});
 document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&!menu.hidden){event.preventDefault();closeMenu(true);return;}
  const link=event.target.closest('[data-project-menu-id]');if(link&&(['ContextMenu','Menu'].includes(event.key)||event.shiftKey&&event.key==='F10')){event.preventDefault();const box=link.getBoundingClientRect();open(link,box.left+12,box.bottom);}
 });
 document.addEventListener('pointerdown',event=>{if(!menu.hidden&&!menu.contains(event.target))closeMenu(false);});
 window.addEventListener('resize',()=>{if(!menu.hidden)place();});
 menu.onkeydown=event=>{if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;event.preventDefault();const buttons=[...menu.querySelectorAll('button')],index=buttons.indexOf(document.activeElement);buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length].focus();};
 async function refreshAfterFailure(){try{await load();}catch{} }
 async function save(project,action,fields={}){
  const route=action==='reimbursement'?'projects/reimbursement':action==='delete'?'projects/delete-permanently':'projects/'+action;
  const expected_revision=action==='reimbursement'?(project.reimbursement_revision??0):(project.project_revision??0);
  return api(route,{[action==='period'?'project_id':'id']:project.id,expected_revision,...fields});
 }
 async function committed(project,result,action){
  if(action==='delete'){
   if(result?.deleted!==true||result.id!==project.id)throw Error('服务已接收操作，但删除结果无法确认；请刷新核对，勿重复提交。');
   state.projects=state.projects.filter(p=>p.id!==project.id);state.items=state.items.filter(x=>x.project_id!==project.id);
   if(projectScope===project.id){location.assign('/');return;}
  }else{
   if(result?.id!==project.id)throw Error('服务已接收操作，但项目结果缺少编号；请刷新后核对，勿重复提交。');
   state.projects=state.projects.map(p=>p.id===project.id?{...p,...result}:p);
   if(action==='rename')state.items=state.items.map(x=>x.project_id===project.id?{...x,project:result.name}:x);
   if(action==='reimbursement')projectReimbursementView=projectReimbursementStatus(result);
  }
  renderProjectContext();render();
  try{await load();}catch{notify('项目操作已保存，列表刷新失败；请刷新页面后继续。');return;}
  if(action==='period'){const messages=['项目时间已保存。'];if(result.overlaps?.length)messages.push(`与 ${result.overlaps.join('、')} 时间重叠，重叠期间的发票需要你手动分配`);if(result.rerouted>0)messages.push(`已把 ${result.rerouted} 封未分配邮件分到本项目`);notify(messages.join(' '));return;}
  notify(action==='delete'?'发票项目已永久删除，原始文件与后勤目录仍保留。':action==='rename'?'项目已更名。':action==='pin'?(result.pinned?'项目已置顶。':'已取消置顶。'):(projectReimbursementStatus(result)==='reimbursed'?'项目已移到已报销。':'项目已移回未报销。'));
 }
 function dialogFor(project,action){
  const dialog=document.createElement('dialog');dialog.id='projectActionDialog';dialog.className='project-action-dialog';dialog.setAttribute('aria-labelledby','projectActionTitle');
  const deleting=action==='delete',period=action==='period';
  dialog.innerHTML=`<div class="dialoghead"><h2 id="projectActionTitle">${deleting?'永久删除项目':period?'修改项目时间':'重命名项目'}</h2><button class="dialog-close" type="button" data-dismiss aria-label="关闭">×</button></div><form>${deleting?`<p>永久删除“${esc(project.name)}”在发票助手中的项目、费用、材料关联和归档记录，删除后不能恢复。</p><p>原始文件、源 PDF 和后勤项目目录仍保留。</p>`:period?projectPeriodFields(project):`<label>项目名称<input name="name" required maxlength="120" autocomplete="off" value="${esc(project.name)}"></label>`}<p data-project-error role="alert" hidden></p><div class="actions"><button type="button" data-cancel>取消</button><button type="submit" ${deleting?'class="project-delete-confirm"':''}>${deleting?'确认永久删除':period?'保存项目时间':'保存名称'}</button></div></form>`;
  document.body.append(dialog);dialog.showModal();
  const input=dialog.querySelector('[name="name"]'),dates=period?bindProjectPeriodFields(dialog):null,submit=dialog.querySelector('[type="submit"]'),error=dialog.querySelector('[data-project-error]');let acknowledged=false,stale=false;
  const dismiss=()=>{if(!busy){dialog.remove();projectLink(project.id)?.focus();}};
  dialog.querySelectorAll('[data-cancel],[data-dismiss]').forEach(button=>button.onclick=dismiss);dialog.oncancel=event=>{event.preventDefault();dismiss();};
  if(input){input.oninput=()=>{submit.disabled=busy||acknowledged||stale||!input.value.trim();};input.focus();input.select();}else if(dates)dates.start.focus();else dialog.querySelector('[data-cancel]').focus();
  dialog.querySelector('form').onsubmit=async event=>{
   event.preventDefault();if(busy||acknowledged||stale||!dialog.querySelector('form').checkValidity()||input&&!input.value.trim())return;
   const fields=deleting?{confirmed:true,confirmation_name:project.name}:dates?{start_date:dates.start.value,end_date:dates.end.value}:{name:input.value.trim()};
   lock(true);dialog.querySelectorAll('button,input').forEach(x=>x.disabled=true);error.hidden=true;
   try{const result=await save(project,action,fields);acknowledged=true;if(deleting&&(result?.deleted!==true||result.id!==project.id))throw Error('删除结果无法确认，请关闭窗口并刷新核对，勿重复提交。');if(!deleting&&result?.id!==project.id)throw Error('项目结果无法确认，请关闭窗口并刷新核对，勿重复提交。');dialog.remove();await committed(project,result,action);}
   catch(failure){error.textContent=failure.message;error.hidden=false;if(acknowledged)return;await refreshAfterFailure();const latest=state.projects.find(p=>p.id===project.id);stale=!latest||latest.name!==project.name||(latest.project_revision??0)!==(project.project_revision??0)||/冲突|版本|已变化|不存在/.test(failure.message);if(stale)error.textContent+=' 项目已变化，请关闭本窗口并重新打开项目操作。';}
   finally{lock(false);if(dialog.isConnected){dialog.querySelectorAll('button,input').forEach(x=>x.disabled=false);submit.disabled=acknowledged||stale||!!input&&!input.value.trim();if(input)input.focus();}}
  };
 }
 window.openProjectPeriod=project=>{if(busy||!project)return;closeMenu(false);dialogFor({...project},'period');};
 menu.onclick=async event=>{
  const button=event.target.closest('[data-project-action]');if(!button||!target||busy)return;
  const project={...target},action=button.dataset.projectAction;closeMenu(false);
  if(action==='rename'||action==='delete'||action==='period'){dialogFor(project,action);return;}
  lock(true);
  try{const fields=action==='pin'?{pinned:!project.pinned}:{status:projectReimbursementStatus(project)==='reimbursed'?'unreimbursed':'reimbursed'};const result=await save(project,action,fields);await committed(project,result,action);}
  catch(failure){notify(failure.message,true);await refreshAfterFailure();}
  finally{lock(false);projectLink(project.id)?.focus();}
 };
})();
