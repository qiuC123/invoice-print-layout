'use strict';
(() => {
 const controls=new Map();
 const values=select=>[...select.selectedOptions].map(option=>option.value).filter(Boolean);
 function close(control,focus=false){control.menu.hidden=true;control.button.setAttribute('aria-expanded','false');if(focus)control.button.focus();}
 function closeAll(except){for(const control of controls.values())if(control!==except)close(control);}
 function position(control){const rect=control.wrapper.getBoundingClientRect();control.menu.style.left=Math.max(16-rect.left,Math.min(0,innerWidth-16-rect.left-control.menu.offsetWidth))+'px';}
 function sync(control){
  const selected=values(control.select),options=[...control.select.options],all=options.find(option=>!option.value)?.textContent||'全部';
  const labels=options.filter(option=>selected.includes(option.value)).map(option=>option.textContent);
  const selectable=options.filter(option=>option.value&&!option.disabled),allSelected=selectable.length>0&&selectable.every(option=>selected.includes(option.value));
  control.button.querySelector('span').textContent=allSelected?all:labels.length>1?`${labels[0]} +${labels.length-1}`:labels[0]||all;
  control.button.title=labels.join('、')||all;control.button.disabled=control.select.disabled;if(control.select.disabled)close(control);
  control.menu.replaceChildren();
  for(const option of options){
   const isAll=!option.value;
   const label=document.createElement('label'),input=document.createElement('input'),text=document.createElement('span');
   input.type='checkbox';input.value=option.value;input.checked=isAll?allSelected:selected.includes(option.value);input.disabled=option.disabled||(isAll&&!selectable.length);
   input.indeterminate=isAll&&selected.length>0&&!allSelected;
   text.textContent=isAll?'全选':option.textContent;label.append(input,text);control.menu.append(label);
   // Keep focus inside the control until the label's native checkbox click.
   // Otherwise focusout can hide the menu between pointerdown and pointerup.
   label.addEventListener('pointerdown',event=>{if(event.target!==input&&!input.disabled)event.preventDefault();});
   input.addEventListener('change',()=>{
    const next=new Set(values(control.select));
    if(isAll){next.clear();if(input.checked)for(const option of selectable)next.add(option.value);}
    else if(input.checked)next.add(option.value);else next.delete(option.value);
    set(control.select.id,[...next]);control.select.dispatchEvent(new Event('change',{bubbles:true}));
    const replacement=[...control.menu.querySelectorAll('input')].find(item=>item.value===option.value);replacement?.focus();
   });
  }
 }
 function set(id,selected){
  const select=document.getElementById(id);if(!select)return;
  const chosen=new Set(selected||[]);for(const option of select.options)option.selected=option.value?chosen.has(option.value):chosen.size===0;
  const control=controls.get(id);if(control)sync(control);
 }
 function attach(select){
  if(!select)return;
  if(controls.has(select.id)){sync(controls.get(select.id));return;}
  const wrapper=document.createElement('div'),button=document.createElement('button'),label=document.createElement('span'),menu=document.createElement('div');
  wrapper.className='multi-filter';wrapper.dataset.filter=select.id;
  button.type='button';button.id=select.id+'Toggle';button.setAttribute('aria-label',select.getAttribute('aria-label'));button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-expanded','false');button.append(label);
  menu.id=select.id+'Menu';menu.className='multi-filter-menu';menu.hidden=true;menu.setAttribute('role','dialog');menu.setAttribute('aria-label',select.getAttribute('aria-label')+'选项');button.setAttribute('aria-controls',menu.id);
  select.before(wrapper);wrapper.append(select,button,menu);select.multiple=true;select.hidden=true;select.tabIndex=-1;select.setAttribute('aria-hidden','true');
  const control={select,wrapper,button,menu};controls.set(select.id,control);sync(control);
  button.addEventListener('click',()=>{const open=menu.hidden;closeAll(control);if(open)sync(control);menu.hidden=!open;if(open)position(control);button.setAttribute('aria-expanded',String(open));});
  select.addEventListener('change',()=>sync(control));
  wrapper.addEventListener('keydown',event=>{if(event.key==='Escape'&&!menu.hidden){event.preventDefault();event.stopPropagation();close(control,true);}else if(event.key==='ArrowDown'&&event.target===button){event.preventDefault();closeAll(control);sync(control);menu.hidden=false;position(control);button.setAttribute('aria-expanded','true');menu.querySelector('input')?.focus();}});
  wrapper.addEventListener('focusout',()=>setTimeout(()=>{if(!wrapper.contains(document.activeElement))close(control);},0));
 }
 for(const id of ['categoryFilter','personFilter','issueFilter']){const select=document.getElementById(id);if(select)attach(select);}
 document.addEventListener('pointerdown',event=>{for(const control of controls.values())if(!control.wrapper.contains(event.target))close(control);});
 window.addEventListener('resize',()=>closeAll());
 window.invoiceMultiFilters={attach:id=>attach(document.getElementById(id)),values:id=>{const select=document.getElementById(id);return select?values(select):[];},set,refresh:id=>{const control=controls.get(id);if(control)sync(control);},closeAll};
})();
