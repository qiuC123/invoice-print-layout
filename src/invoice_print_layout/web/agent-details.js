/* Read-only project materials and expense details. Originals use existing checked routes. */
(() => {
  'use strict';
  const roles = {purchase:'购买明细',invoice:'发票',receipt:'收据',payment:'付款截图',order:'订单记录',trip:'行程单',detail:'运单明细',other:'待分类材料',package:'历史打印包'};
  const statuses = {received:'等待整理',review:'已识别，待整理',linked:'已关联费用',duplicate:'重复材料',converted:'已转为正式材料',unused:'无需使用',archived:'已归档',deleted:'已删除'};
  const fieldLabels = {title:'识别名称',goods:'商品内容',purchase_content:'购买内容',merchant:'商家',shop_name:'实际店铺',shopping_platform:'购物平台',payment_channel:'支付渠道',expense_date:'消费／付款日期',invoice_number:'发票号码',order_number:'订单号',payment_status:'付款状态',category:'识别类别',note:'备注'};
  const sources = {upload:'上传',phone:'手机上传',mobile:'手机上传',mail:'邮箱收件',mail_agent:'邮箱自动收件',qr:'二维码领取'};
  const methods = {manual:'人工填写',native_pdf:'PDF原文',local_ocr:'本机文字识别','glm-5.3-flash':'模型辅助识别'};
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  const node = (tag, value, cls) => {const element=document.createElement(tag);if(value!==undefined)element.textContent=value;if(cls)element.className=cls;return element;};
  function button(label, action, cls) {const element=node('button',label,cls);element.type='button';element.addEventListener('click',action);return element;}
  function link(label, url) {const element=node('a',label);element.href=url;element.target='_blank';element.rel='noopener';return element;}
  function decimalCents(value) {
    const match=/^(\d+)(?:\.(\d{1,2}))?$/.exec(text(value).trim());
    if(!match)return null;
    const cents=Number(match[1])*100+Number((match[2]||'').padEnd(2,'0'));
    return Number.isSafeInteger(cents)?cents:null;
  }
  function money(cents) {
    if(!Number.isSafeInteger(cents)||cents<0)return '金额待确认';
    return '¥ '+Math.floor(cents/100)+'.'+String(cents%100).padStart(2,'0');
  }
  const materialMoney = entry => money(decimalCents((entry.fields||{}).amount));
  const expenseMoney = item => item.amount_pending?'金额待确认':money(item.amount_cents);
  const materialStatus = entry => entry.item_id?'已关联费用':statuses[entry.status]||'待确认处理状态';
  function expenseStatus(item) {
    if(item.stage==='cancelled')return '已取消';
    if(item.stage==='reimbursed')return '历史记录 · 已报销';
    if(item.stage==='submitted')return '已生成报销包';
    if(item.verified===true&&item.ready===true)return '已核对 · 可提交';
    if(item.amount_pending)return '金额待确认 · 待核对';
    return item.complete===true?'材料齐全 · 待核对':'仍缺材料 · 待核对';
  }
  function expenseName(item) {return text(item.project_name)||text((item.report_choice||{}).project_name)||text(item.display_name)||text(item.title)||'待补充费用名称';}
  function expenseGoods(item,linked) {
    // Prepared Agent content already refers to the actual purchase evidence.
    // A payment screen's "goods" field can merely contain the payee/platform.
    const normalized=value=>text(value).normalize('NFKC').replace(/[\s()（）]/g,'').toLocaleLowerCase();
    const identities=new Set([item.merchant,item.shop_name,item.shopping_platform,
      ...linked.flatMap(entry=>{const fields=entry.fields||{};return [fields.merchant,fields.shop_name,fields.shopping_platform];})]
      .map(normalized).filter(Boolean));
    const generic=/^(?:待核对购买凭证|待核对费用|日用百货|超市服务|商品|商品不明|购买凭证|商品说明|其他|订单|购物|消费|付款|支付|付款截图|微信支付|支付宝|(?:美团|淘宝|京东|饿了么|平台)(?:平台)?(?:商户|订单))$/;
    const clean=value=>typeof value==='string'?value.trim():'';
    const meaningful=value=>!!value&&!generic.test(normalized(value))&&!identities.has(normalized(value));
    const saved=(item.agent_content||{}).goods;
    const prepared=(Array.isArray(saved)?saved:[]).map(row=>clean(row&&row.name)).filter(meaningful);
    if(prepared.length)return [...new Set(prepared)].join('；');
    const purchaseRoles=new Set(['purchase','receipt','order','invoice','detail','trip']);
    const candidates=[item.goods,item.purchase_content,...linked.filter(entry=>purchaseRoles.has(entry.role))
      .flatMap(entry=>{const fields=entry.fields||{};return [fields.goods,fields.purchase_content];})];
    return [...new Set(candidates.map(clean).filter(meaningful))].join('；');
  }

  function mount(options) {
    const {materialsHost,expensesHost,detailHost,api,getProject}=options;
    if(!materialsHost||!expensesHost||!detailHost||typeof api!=='function')throw new Error('工作台详情区域未就绪');
    let project='',generation=0,request=0,busy=false,materials=[],expenses=[],selection=null,dataKey='',detailKey='';
    const lists={};
    function createList(host,kind,title,filters) {
      host.classList.add('agent-data-panel');host.replaceChildren();
      const heading=node('div',undefined,'agent-data-heading'),h=node('h2',title),count=node('span','0','agent-data-count');heading.append(h,count);
      const controls=node('div',undefined,'agent-data-controls');
      const search=node('input');search.type='search';search.placeholder=kind==='materials'?'搜索文件名、商品或商家':'搜索报销名称、类别或商家';search.setAttribute('aria-label',title+'搜索');
      const filter=node('select');filter.setAttribute('aria-label',title+'状态');
      for(const [value,label] of filters){const option=node('option',label);option.value=value;filter.append(option);}
      const refresh=button('刷新',()=>refreshData(), 'agent-data-refresh');refresh.setAttribute('aria-label','刷新'+title);
      controls.append(search,filter,refresh);
      const message=node('p','','agent-data-message');message.setAttribute('role','status');
      const body=node('div',undefined,'agent-data-list');body.setAttribute('aria-label',title+'列表');
      const more=button('显示更多',()=>{list.limit+=30;renderList(kind);},'agent-data-more');more.hidden=true;
      host.append(heading,controls,message,body,more);
      const list={host,search,filter,refresh,message,body,more,count,limit:30,key:''};lists[kind]=list;
      search.addEventListener('input',()=>{list.limit=30;renderList(kind);});filter.addEventListener('change',()=>{list.limit=30;renderList(kind);});
    }
    createList(materialsHost,'materials','项目材料',[['','全部材料'],['pending','待整理'],['linked','已关联费用'],['attention','有待确认问题'],['inactive','归档／无需使用']]);
    createList(expensesHost,'expenses','项目费用',[['','全部费用'],['pending','待核对'],['ready','已核对'],['missing','仍缺材料']]);
    detailHost.classList.add('agent-detail-panel');
    function emptyDetail(message='选择一份材料或一笔费用，查看原件和详情。') {
      selection=null;detailKey='';detailHost.replaceChildren(node('p',message,'agent-data-empty'));updateSelection();
    }
    function updateSelection() {
      for(const [kind,list] of Object.entries(lists))for(const row of list.body.querySelectorAll('[data-detail-id]')){
        const selected=!!selection&&selection.kind===kind&&selection.id===row.dataset.detailId;
        row.classList.toggle('is-selected',selected);row.setAttribute('aria-pressed',String(selected));
      }
    }
    function materialMatches(entry,filter) {
      if(!filter)return true;
      if(filter==='linked')return !!entry.item_id;
      if(filter==='attention')return (entry.issues||[]).length>0;
      if(filter==='inactive')return ['archived','unused','converted'].includes(entry.status);
      return !entry.item_id&&['received','review'].includes(entry.status);
    }
    function expenseMatches(item,filter) {
      if(!filter)return true;
      if(filter==='ready')return item.verified===true&&item.ready===true;
      if(filter==='missing')return item.complete!==true;
      return item.stage==='draft'&&!item.verified;
    }
    function renderList(kind) {
      const list=lists[kind],query=list.search.value.trim().toLocaleLowerCase(),filter=list.filter.value;
      const all=kind==='materials'?materials:expenses;
      const visible=all.filter(row=>{
        const values=kind==='materials'?[row.name,...Object.values(row.fields||{})]:[expenseName(row),row.title,row.category,row.merchant,row.expense_date];
        return (!query||values.map(text).join(' ').toLocaleLowerCase().includes(query))&&(kind==='materials'?materialMatches(row,filter):expenseMatches(row,filter));
      });
      list.count.textContent=String(all.length);
      list.message.textContent=project?(visible.length?('共 '+visible.length+(kind==='materials'?' 份':' 笔')+(visible.length>list.limit?'，先显示 '+list.limit:'')):'当前范围没有'+(kind==='materials'?'材料。':'费用。')):'请先选择或新建项目。';
      const key=JSON.stringify([project,visible.slice(0,list.limit),query,filter,list.limit]);
      list.more.hidden=visible.length<=list.limit;
      if(key===list.key){updateSelection();return;}
      list.key=key;const scroll=list.body.scrollTop,fragment=document.createDocumentFragment();
      for(const row of visible.slice(0,list.limit)) {
        const material=kind==='materials';
        const choice=button('',()=>open(kind,row.id),'agent-data-row');choice.dataset.detailId=row.id;
        const title=node('strong',material?text(row.name)||'未命名材料':expenseName(row));
        const meta=node('span',material?(roles[row.role]||'待分类材料')+' · '+materialStatus(row):(text(row.category)||'类别待确认')+' · '+expenseStatus(row),'agent-data-meta');
        choice.append(title,node('span',material?materialMoney(row):expenseMoney(row),'agent-data-money'),meta);
        if(material&&(row.issues||[]).length)choice.append(node('span','有 '+row.issues.length+' 项待确认问题','agent-data-warning'));
        fragment.append(choice);
      }
      list.body.replaceChildren(fragment);list.body.scrollTop=scroll;updateSelection();
    }
    function field(container,label,value,source) {
      if(!text(value))return;
      const row=node('div',undefined,'agent-detail-field');row.append(node('dt',label));const valueNode=node('dd',text(value));
      if(source)valueNode.append(node('small',source,'agent-field-source'));row.append(valueNode);container.append(row);
    }
    function group(title) {const section=node('div',undefined,'agent-detail-group');section.append(node('h3',title));return section;}
    function issues(container,values) {
      const rows=(Array.isArray(values)?values:[]).map(text).filter(Boolean);
      if(!rows.length)return;
      const section=group('需要留意'),list=node('ul',undefined,'agent-data-warning');for(const value of rows)list.append(node('li',value));section.append(list);container.append(section);
    }
    function original(container,entry,attachment=false) {
      const section=group((attachment?(roles[entry.role]||'材料')+' · ':'')+(text(entry.name)||'材料原件'));
      if(attachment&&entry.available===false){section.append(node('p','原件暂时不可读取，请到人工工作台检查材料。','agent-data-warning'));container.append(section);return;}
      const key=encodeURIComponent(entry.id),prefix=attachment?'attachment':'intake';
      const actions=node('div',undefined,'agent-detail-original-actions');
      actions.append(link('打开完整原件（全部页）','/'+prefix+'-preview/'+key),link('下载原文件',attachment?'/file/'+key:'/intake-file/'+key));
      section.append(actions,node('p','下方仅预览第 1 页；多页文件请打开完整原件查看全部页面。','agent-data-hint'));
      const preview=node('a');preview.href='/'+prefix+'-preview/'+key;preview.target='_blank';preview.rel='noopener';preview.setAttribute('aria-label','打开完整原件：'+text(entry.name));
      const image=node('img');image.alt=text(entry.name)+'，第 1 页';image.loading='lazy';image.src='/'+prefix+'-page/'+key+'?page=0';
      const failure=node('p','原件预览暂时不可用，可尝试打开完整原件或到人工工作台检查。','agent-data-warning');failure.hidden=true;
      image.addEventListener('error',()=>{if(!image.isConnected)return;image.hidden=true;failure.hidden=false;});
      preview.append(image);section.append(preview,failure);container.append(section);
    }
    function header(title,kind,id) {
      const bar=node('div',undefined,'agent-detail-heading');const heading=node('h2',title);heading.tabIndex=-1;
      bar.append(heading,button('关闭详情',()=>emptyDetail(),'agent-detail-close'));detailHost.append(bar);
      const manual=link(kind==='expenses'?'到人工工作台查看／核对':'到人工工作台处理材料','/?project='+encodeURIComponent(project)+(kind==='expenses'?'&matter='+encodeURIComponent(id):'&view=synthesis'));
      detailHost.append(manual);return heading;
    }
    function materialDetail(entry) {
      header(text(entry.name)||'材料详情','materials',entry.id);
      const summary=node('dl',undefined,'agent-detail-fields');
      field(summary,'用途',roles[entry.role]||'待分类材料');field(summary,'金额',materialMoney(entry));field(summary,'处理状态',materialStatus(entry));
      field(summary,'来源',sources[entry.source]||'其他本机来源');field(summary,'收到时间',entry.received_at);
      field(summary,'归属人',entry.reimbursement_person_label||'尚未指定');detailHost.append(summary);
      const recognized=group('识别结果'),fields=node('dl',undefined,'agent-detail-fields');
      for(const [key,label] of Object.entries(fieldLabels))field(fields,label,(entry.fields||{})[key],methods[(entry.field_sources||{})[key]]||'');
      if(fields.childElementCount)recognized.append(fields);else recognized.append(node('p','尚未提取出可用字段。','agent-data-hint'));
      if(entry.result)recognized.append(node('p',text(entry.result),'agent-data-hint'));detailHost.append(recognized);issues(detailHost,entry.issues);
      if(entry.item_id) {
        const related=expenses.find(item=>item.id===entry.item_id);
        detailHost.append(related?button('查看关联费用：'+expenseName(related),()=>open('expenses',related.id),'agent-detail-related'):node('p','材料已关联费用；该费用当前不可在本项目费用列表中查看。','agent-data-hint'));
      }
      original(detailHost,entry);
      if(entry.text){const extracted=node('details',undefined,'agent-detail-extracted');extracted.append(node('summary','查看提取的原文'),node('pre',text(entry.text)));detailHost.append(extracted);}
    }
    function expenseDetail(item) {
      header(expenseName(item),'expenses',item.id);
      const summary=node('dl',undefined,'agent-detail-fields');
      field(summary,'报销名称',expenseName(item));field(summary,'费用类别',item.category);field(summary,'金额',expenseMoney(item));field(summary,'核对状态',expenseStatus(item));
      field(summary,'商家',item.merchant||'尚未识别');field(summary,'消费／预订日期',item.expense_date||'尚未识别');field(summary,'归属人',item.reimbursement_person_label||'尚未指定');
      const linked=materials.filter(entry=>entry.item_id===item.id);
      field(summary,'商品内容',expenseGoods(item,linked)||'尚未提取；可对照下方原件查看');field(summary,'备注',item.note);detailHost.append(summary);
      if(!item.verified)detailHost.append(node('p','当前费用尚未核对；自动整理和预览不代表核对完成。','agent-data-warning'));
      issues(detailHost,item.missing);
      const attachments=Array.isArray(item.attachments)?item.attachments:[];
      const proofHeading=node('h3','关联材料 · '+attachments.length+' 份');detailHost.append(proofHeading);
      if(!attachments.length)detailHost.append(node('p','尚无关联原件。','agent-data-hint'));
      for(const attachment of attachments)original(detailHost,attachment,true);
    }
    function renderDetail() {
      if(!selection)return;
      const row=(selection.kind==='materials'?materials:expenses).find(entry=>entry.id===selection.id);
      if(!row){emptyDetail('所选内容已不在当前项目中，请重新选择。');return;}
      const key=JSON.stringify([project,selection,row,selection.kind==='expenses'?materials.filter(entry=>entry.item_id===row.id):null]);
      if(key===detailKey)return;detailKey=key;const scroll=detailHost.scrollTop;detailHost.replaceChildren();
      if(selection.kind==='materials')materialDetail(row);else expenseDetail(row);detailHost.scrollTop=scroll;
    }
    function open(kind,id) {
      const row=(kind==='materials'?materials:expenses).find(entry=>entry.id===id);
      if(!row)return false;
      selection={kind,id};detailKey='';renderDetail();updateSelection();
      const heading=detailHost.querySelector('h2');if(heading)heading.focus({preventScroll:true});
      if(window.matchMedia('(max-width: 900px)').matches)detailHost.scrollIntoView({behavior:'smooth',block:'start'});
      return true;
    }
    async function refreshData() {
      const selectedProject=project,current=generation,number=++request;
      if(!selectedProject)return;
      for(const list of Object.values(lists)){list.refresh.disabled=true;if(!dataKey)list.message.textContent='正在读取项目内容…';}
      try {
        const [queue,state]=await Promise.all([api('/api/intake'),api('/api/state')]);
        if(current!==generation||number!==request||selectedProject!==project)return;
        const nextMaterials=(Array.isArray(queue.queue)?queue.queue:[]).filter(entry=>entry.project_id===project&&entry.status!=='deleted');
        const nextExpenses=(Array.isArray(state.items)?state.items:[]).filter(item=>item.project_id===project&&!item.deleted&&!item.merged_into&&!item.returned_to_synthesis&&!item.undone_material_group);
        const key=JSON.stringify([project,nextMaterials,nextExpenses]);
        if(key!==dataKey){dataKey=key;materials=nextMaterials;expenses=nextExpenses;}
        renderList('materials');renderList('expenses');renderDetail();
      }catch(error){
        if(current!==generation||number!==request)return;
        for(const list of Object.values(lists))list.message.textContent='暂时无法读取项目内容，请点刷新重试。';
      }finally{if(current===generation&&number===request)for(const list of Object.values(lists))list.refresh.disabled=busy;}
    }
    function setProject(id) {
      id=text(id);if(id===project)return refreshData();
      project=id;generation++;request++;materials=[];expenses=[];dataKey='';
      for(const list of Object.values(lists)){list.limit=30;list.key='';list.search.value='';list.filter.value='';}
      emptyDetail(project?undefined:'请先选择或新建项目。');renderList('materials');renderList('expenses');return refreshData();
    }
    function setBusy(value) {busy=!!value;for(const list of Object.values(lists))list.refresh.disabled=busy;}
    emptyDetail();renderList('materials');renderList('expenses');
    if(typeof getProject==='function'&&getProject())setProject(getProject());
    return {setProject,refresh:refreshData,openMaterial:id=>open('materials',id),openExpense:id=>open('expenses',id),setBusy};
  }
  window.AgentDetails={mount};
})();
