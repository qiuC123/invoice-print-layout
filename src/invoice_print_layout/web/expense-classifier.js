/* Suggestions only. Applying fills the existing form; saving remains explicit. */
(() => {
  function mount(item) {
    const form=document.getElementById('editForm');
    if(!form||!state.category_suggestion_ready||item.stage!=='draft')return;
    const box=document.createElement('section');
    box.className='rule';box.id='categorySuggestion';
    const run=document.createElement('button');run.type='button';run.textContent='建议费用分类';run.id='suggestCategory';
    const hint=document.createElement('p');hint.className='muted';
    hint.textContent='先保存用途备注再判断。明确内容按规则分类，模糊内容发送必要文字给 Jev；原件不上传，结果不会自动保存。';
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
          const apply=document.createElement('button');apply.type='button';apply.textContent='采用建议（未保存）';apply.id='applyCategorySuggestion';
          apply.onclick=()=>{
            if(!current()){result.textContent='事项已修改，请保存后重新判断。';return;}
            form.elements.namedItem('category').value=answer.category;
            result.textContent=`已填入“${answer.category}”，请点击“保存事项”。仍需核对材料和金额才能提交。`;
          };result.append(apply);
        }
      }catch(error){if(form.isConnected)result.textContent=error.message||'判断失败，请重试或手动分类。';}
      finally{run.disabled=false;}
    };
  }
  window.expenseClassifier={mount};
  if(typeof state!=='undefined'&&state.items)renderDetail();
})();
