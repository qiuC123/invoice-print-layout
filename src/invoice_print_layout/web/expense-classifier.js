/* Automatic category-only saves; preserve in-progress form edits and explicit user choices. */
(() => {
  let busy=false, refreshPending=false, paused=false, timer=null;
  const formSnapshot=form=>JSON.stringify([...new FormData(form)]);
  const dirty=()=>{const form=document.getElementById('editForm');return !!form&&form.dataset.categoryBaseline!==formSnapshot(form);};
  const editing=()=>dirty()||!!document.querySelector('dialog[open]');
  const sourceName={jev:'Jev 判断',rules:'固定规则',cache:'相同内容缓存',experience:'人工确认经验',human:'你的分类',local:'本地检查'};
  function schedule(){clearTimeout(timer);timer=setTimeout(pump,150);}
  async function runAuto(id,retry=false){
    if(busy)return;
    busy=true;
    const form=document.getElementById('editForm'),originalCategory=form?.elements.namedItem('category')?.value;
    const result=document.getElementById('categorySuggestionResult');
    if(active===id&&result)result.textContent='正在自动分类，结果明确后会保存…';
    try{
      const answer=await api('category/auto',{id,retry});
      if(answer.status==='busy'){timer=setTimeout(pump,3000);return;}
      if(editing()){
        // A note edited during a request must not accidentally save the old category back.
        if(answer.saved&&form?.isConnected&&active===id&&form.elements.namedItem('category').value===originalCategory){
          form.elements.namedItem('category').value=answer.category;
        }
        refreshPending=true;
        if(active===id&&result?.isConnected)result.textContent=answer.saved?`已自动保存“${answer.category}”。你的其他未保存修改仍保留。`:answer.reason;
      }else{await load();}
    }catch(error){paused=true;if(result?.isConnected)result.textContent=error.message||'自动分类暂不可用，可点击重试。';}
    finally{busy=false;}
    if(!editing()&&!paused)schedule();
  }
  async function pump(){
    if(busy||paused||!state.category_auto_ready||editing())return;
    if(refreshPending){refreshPending=false;await load();}
    const item=state.items.find(x=>state.classification?.[x.id]?.pending);
    if(item)await runAuto(item.id);
  }
  function mountAuto(item,form){
    form.dataset.categoryBaseline=formSnapshot(form);
    const box=document.createElement('section');box.className='rule';box.id='categorySuggestion';
    const title=document.createElement('b');title.textContent='自动费用分类';
    const hint=document.createElement('p');hint.className='muted';hint.textContent='工作台打开后自动处理待分类事项。优先复用本地经验和缓存，必要时用 Jev；明确结果自动保存类别，报销核对仍由你完成。';
    const result=document.createElement('div');result.id='categorySuggestionResult';result.setAttribute('role','status');
    const info=state.classification?.[item.id]||{};
    result.textContent=info.pending?'等待自动分类…':info.reason?`${info.saved?'已保存：'+info.category+' · ':''}${sourceName[info.source]||''}。${info.reason}`:'已有分类，可在上方手动修改。';
    box.append(title,hint,result);
    if(info.evidence){const details=document.createElement('details'),summary=document.createElement('summary'),text=document.createElement('p');summary.textContent='查看分类依据';text.textContent=Object.values(info.evidence).filter(Boolean).join('\n');text.style.whiteSpace='pre-wrap';details.append(summary,text);box.append(details);}
    if(item.category==='其他'||['needs_info','error'].includes(info.status)){
      const retry=document.createElement('button');retry.type='button';retry.id='retryCategory';retry.textContent='重试自动分类';
      retry.onclick=()=>{if(editing()){result.textContent='请先保存正在编辑的内容。';return;}paused=false;runAuto(item.id,true);};box.append(retry);
    }
    form.after(box);schedule();
  }
  function mount(item) {
    const form=document.getElementById('editForm');
    if(!form||!state.category_suggestion_ready||item.stage!=='draft')return;
    if(state.category_auto_ready){mountAuto(item,form);return;}
    const box=document.createElement('section');
    box.className='rule';box.id='categorySuggestion';
    const run=document.createElement('button');run.type='button';run.textContent='建议费用分类';run.id='suggestCategory';
    const hint=document.createElement('p');hint.className='muted';
    hint.textContent='当前服务尚未加载自动分类。仍可手动判断，采用后会直接保存类别；其他未保存内容请先保存。';
    const result=document.createElement('div');result.setAttribute('role','status');result.id='categorySuggestionResult';
    box.append(run,hint,result);form.after(box);
    const snapshot=()=>JSON.stringify([...new FormData(form)]),initial=snapshot();
    const current=()=>form.isConnected&&active===item.id&&snapshot()===initial;
    run.onclick=async()=>{
      if(!current()){result.textContent='事项有未保存的修改，请先保存再判断。';return;}
      run.disabled=true;result.textContent='正在本地提取文字并判断…';
      try{
        const answer=await api('category/suggest',{id:item.id});
        if(!form.isConnected||active!==item.id)return;
        if(!current()){result.textContent='判断期间事项已修改，请保存后重新判断。';return;}
        result.replaceChildren();
        const summary=document.createElement('p');
        const source={jev:'Jev 判断',rules:'固定规则',local:'本地检查'}[answer.source]||'分类建议';
        summary.textContent=`${answer.category?'建议：'+answer.category:'待你补充'} · ${source}。${answer.reason}`;
        result.append(summary);
        if(answer.evidence){
          const details=document.createElement('details'),title=document.createElement('summary'),text=document.createElement('p');
          title.textContent='查看用于判断的文字';text.style.whiteSpace='pre-wrap';text.style.overflowWrap='anywhere';
          text.textContent=Object.values(answer.evidence).filter(Boolean).join('\n')||'未提取到明确内容';
          details.append(title,text);result.append(details);
        }
        for(const warning of answer.warnings||[]){const p=document.createElement('p');p.textContent=warning;result.append(p);}
        if(answer.category&&state.categories.includes(answer.category)&&answer.category!=='其他'){
          const apply=document.createElement('button');apply.type='button';apply.textContent='采用并保存分类';apply.id='applyCategorySuggestion';
          apply.onclick=async()=>{
            if(!current()){result.textContent='事项已修改，请保存后重新判断。';return;}
            apply.disabled=true;
            try{await api('update',{id:item.id,...Object.fromEntries(new FormData(form)),category:answer.category});await load();notify(`分类已保存为“${answer.category}”，材料和金额仍需核对。`);}
            catch(error){if(result.isConnected)result.textContent=error.message;apply.disabled=false;}
          };result.append(apply);
        }
      }catch(error){if(form.isConnected)result.textContent=error.message||'判断失败，请重试或手动分类。';}
      finally{run.disabled=false;}
    };
  }
  window.expenseClassifier={mount,schedule};
  setInterval(()=>{if(state.category_auto_ready&&!editing())schedule();},4000);
  if(typeof state!=='undefined'&&state.items)renderDetail();
})();
