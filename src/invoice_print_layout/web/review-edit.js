'use strict';
(() => {
 window.openReviewFieldsEditor=key=>{
  const item=state.items.find(x=>x.id===key);
  if(!reviewFieldsEditable(item))throw Error('仅可在待核对中修改当前费用；退回合成台或历史费用不能编辑。');
  const existing=$('reviewEditDialog');if(existing){existing.querySelector('input,select')?.focus();return;}
  const types=state.report_types||[];
  if(!types.length)throw Error('报销分类尚未加载，请刷新页面后再试。');
  const choice=item.report_choice||{},fields={project_type:item.project_type||choice.project_type||'',project_name:item.project_name||choice.project_name||'',amount:item.amount_pending?'':item.amount??(Number.isSafeInteger(item.amount_cents)?(item.amount_cents/100).toFixed(2):'')};
  const dialog=document.createElement('dialog');dialog.id='reviewEditDialog';dialog.className='review-edit-dialog';dialog.setAttribute('aria-labelledby','reviewEditTitle');
  dialog.innerHTML=`<div class="dialoghead"><h2 id="reviewEditTitle">修改报销信息</h2><button class="dialog-close" type="button" data-dismiss aria-label="关闭">×</button></div><p class="muted">${esc(item.title)} · 保存后仍待核对。</p>${!item.complete?`<p class="review-edit-missing">还缺：${esc((item.missing||[]).join('、'))}；可从费用详情补充材料。</p>`:''}<form novalidate><div class="review-edit-fields"><label for="reviewEditType">项目类型<select id="reviewEditType" name="project_type" required><option value="">选择项目类型</option>${types.map(type=>`<option value="${esc(type.code)}" ${type.code===fields.project_type?'selected':''}>${esc(type.label)}</option>`).join('')}</select></label><label for="reviewEditAmount">金额（元）<input id="reviewEditAmount" name="amount" required inputmode="decimal" maxlength="20" value="${esc(fields.amount)}"></label><label class="review-edit-name" for="reviewEditName">项目名称<span id="reviewEditNameSlot"></span></label></div><div id="reviewEditSuggestion" class="review-edit-suggestion" hidden></div><p id="reviewEditError" role="alert" hidden></p><div class="actions"><button type="button" data-close>取消</button><button type="submit" id="reviewEditSave" class="primary">保存报销信息</button></div></form>`;
  document.body.append(dialog);dialog.showModal();
  const typeControl=dialog.querySelector('#reviewEditType'),amountControl=dialog.querySelector('#reviewEditAmount'),error=dialog.querySelector('#reviewEditError'),submit=dialog.querySelector('#reviewEditSave');
  let busy=false,stale=false,acknowledged=false;
  function renderName(name=''){
   const type=types.find(x=>x.code===typeControl.value);
   dialog.querySelector('#reviewEditNameSlot').innerHTML=type?.editable_name?`<input id="reviewEditName" name="project_name" required maxlength="200" value="${esc(name)}" placeholder="填写项目名称">`:`<select id="reviewEditName" name="project_name" required ${type?'':'disabled'}><option value="">${type?'选择对应细则':'先选择项目类型'}</option>${(type?.names||[]).map(value=>`<option value="${esc(value)}" ${value===name?'selected':''}>${esc(value)}</option>`).join('')}</select>`;
   dialog.querySelector('#reviewEditName').oninput=update;dialog.querySelector('#reviewEditName').onchange=update;
  }
  const read=()=>({project_type:typeControl.value,project_name:dialog.querySelector('#reviewEditName').value.trim(),amount:amountControl.value.trim()});
  function invalid(){
   const value=read(),type=types.find(x=>x.code===value.project_type);
   if(!type)return '请选择项目类型。';
   if(!/^\d+(?:\.\d{1,2})?$/.test(value.amount)||!Number.isFinite(Number(value.amount))||Number(value.amount)>999999999)return '金额请填非负数字，最多两位小数。';
   if(!value.project_name)return '请选择或填写项目名称。';
   if(value.project_name.length>200)return '项目名称最多200字。';
   if(!type.editable_name&&!type.names.includes(value.project_name))return '项目名称须选择当前类型对应的细则。';
   return '';
  }
  function update(){submit.disabled=busy||stale||acknowledged||!!invalid();if(!stale&&!busy&&!acknowledged){error.textContent=invalid();error.hidden=!error.textContent;}}
  function close(){if(busy)return;dialog.remove();$('editReviewFields')?.focus();}
  function lock(value){busy=value;dialog.querySelectorAll('button,input,select').forEach(x=>x.disabled=value);if(!value)dialog.querySelector('#reviewEditName').disabled=!types.some(x=>x.code===typeControl.value);update();}
  renderName(fields.project_name);typeControl.onchange=()=>{renderName();update();};amountControl.oninput=update;
  dialog.querySelectorAll('[data-close],[data-dismiss]').forEach(button=>button.onclick=close);dialog.oncancel=event=>{event.preventDefault();close();};
  const suggestion=item.report_choice_suggestion;
  if(suggestion?.project_type&&suggestion.project_name){const label=types.find(x=>x.code===suggestion.project_type)?.label||suggestion.project_type,target=dialog.querySelector('#reviewEditSuggestion');target.hidden=false;target.textContent=`建议：${label} · ${suggestion.project_name}`;}
  dialog.querySelector('form').onsubmit=async event=>{
   event.preventDefault();if(busy||stale||acknowledged)return;
   const current=state.items.find(x=>x.id===key);
   if(!reviewFieldsEditable(current)||current.version!==item.version){stale=true;error.textContent='费用或所在阶段已变化，请关闭窗口并重新打开后编辑。';error.hidden=false;update();return;}
   const problem=invalid();if(problem){error.textContent=problem;error.hidden=false;update();return;}
   const body={id:key,expected_version:item.version,...read()};lock(true);error.hidden=true;
   try{
    const result=await api('review/edit-fields',body);acknowledged=true;
    if(result?.id!==key||!result.version)throw Error('报销信息已请求保存，但返回内容无法确认；请关闭窗口并刷新核对，勿重复提交。');
    state.items=state.items.map(x=>x.id===key?result:x);checked.delete(key);active=key;view='review';dialog.remove();render();notify('报销信息已保存，仍在待核对；请核对金额与材料后再提交。');
   }catch(failure){
    if(!dialog.isConnected){notify(failure.message,true);return;}
    stale=acknowledged||failure.status===409||/版本|已变化|冲突|已删除|已合并|退回合成台|不是草稿|已提交/.test(failure.message);
    error.textContent=failure.message+(stale?' 请关闭窗口并重新打开后编辑。':'');error.hidden=false;
    if(stale&&!acknowledged){try{await load();}catch{}}
   }finally{if(dialog.isConnected){busy=false;dialog.querySelectorAll('button,input,select').forEach(x=>x.disabled=false);dialog.querySelector('#reviewEditName').disabled=!types.some(x=>x.code===typeControl.value);submit.disabled=stale||acknowledged||!!invalid();}}
  };
  update();typeControl.focus();
 };
 const button=$('editReviewFields');if(button)button.disabled=false;
})();
