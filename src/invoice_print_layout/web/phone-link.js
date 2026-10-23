'use strict';
(() => {
  const button=document.createElement('button');button.type='button';button.id='phoneLink';button.textContent='手机连接';
  document.querySelector('header .actions').append(button);
  button.onclick=async()=>{
    if(document.getElementById('phoneLinkDialog'))return;
    const dialog=document.createElement('dialog');dialog.id='phoneLinkDialog';dialog.className='phone-link-dialog';
    dialog.innerHTML='<div class="dialoghead"><h2>手机连接</h2><button type="button" data-close aria-label="关闭">×</button></div><p>手机上传材料、查看和核对费用，与电脑版共用一份账本。</p><div data-content>正在读取连接状态…</div><p data-error role="alert" hidden></p><div class="actions"><button type="button" data-close>完成</button></div>';
    document.body.append(dialog);dialog.showModal();let busy=false,mode='lan',latest;
    dialog.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>{if(!busy)dialog.remove();});
    dialog.oncancel=e=>{if(busy)e.preventDefault();};dialog.onclose=()=>dialog.remove();
    const content=dialog.querySelector('[data-content]'),error=dialog.querySelector('[data-error]');
    async function request(route,body){const response=await fetch('/api/phone-link'+route,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error||'连接操作未完成');return data;}
    function render(data){
      latest=data;
      if(data.enabled){
        mode=data.mode||'lan';
        content.innerHTML=`<p class="phone-link-on">${mode==='remote'?'异地访问':'同一 Wi-Fi 连接'}已开启 · 已配对 ${Number(data.clients)} 台</p>${data.qr?`<img class="phone-link-qr" alt="手机连接二维码" src="${esc(data.qr)}"><p>扫码或复制配对链接，在手机浏览器打开；${Math.ceil(data.expires_in/60)} 分钟内有效。</p><button type="button" data-copy>复制手机配对链接</button><p data-copy-status role="status"></p><label>手机配对链接<input readonly data-link aria-label="手机配对链接" value="${esc(data.pair_url)}"></label>`:'<p>二维码已过期，已配对手机可继续使用。点击下方更新二维码。</p>'}<p class="phone-link-address">${esc(data.url)}</p>${mode==='remote'?'<p>手机先打开 Tailscale，连接与这台电脑相同的账号，再打开上方配对链接。原件和账本保存在电脑。</p>':''}<p>电脑需保持开机、助手保持运行；关闭连接后所有手机立即失效。</p><div class="actions"><button type="button" data-status>刷新连接状态</button><button type="button" data-refresh>更新二维码</button><button type="button" data-stop>关闭手机连接</button></div>`;
        content.querySelector('[data-refresh]').onclick=()=>run(()=>request('/rotate',{}));
        content.querySelector('[data-stop]').onclick=()=>run(()=>request('/stop',{}));
        const copy=content.querySelector('[data-copy]');
        if(copy)copy.onclick=async()=>{const input=content.querySelector('[data-link]'),status=content.querySelector('[data-copy-status]');try{await navigator.clipboard.writeText(input.value);status.textContent='已复制，请在手机浏览器打开。';}catch(_){input.focus();input.select();status.textContent='请复制选中的完整链接。';}};
      }else{
        const remote=data.remote||{};
        const remoteText=remote.ready?'私人网络已连接，可以开启异地访问。':remote.auth_url?'请在手机打开登录链接，绑定这台电脑；完成后刷新连接状态。':remote.installed?'点击连接，获取账号登录入口。':'请先在电脑运行 scripts/prepare-phone-remote.ps1 准备专用连接组件。';
        content.innerHTML=`<label>连接方式<select data-mode aria-label="连接方式"><option value="lan">同一 Wi-Fi</option><option value="remote">异地访问 · Tailscale</option></select></label>${mode==='remote'?`<p>${remoteText}</p>${remote.auth_url?`<p><a href="${esc(remote.auth_url)}" target="_blank" rel="noopener noreferrer">打开账号登录链接</a></p><p class="phone-link-address">${esc(remote.auth_url)}</p>`:''}<p>手机安装 Tailscale，用同一账号连接。材料直接保存在电脑，共用原账本。</p><button type="button" class="primary" data-remote ${remote.installed?'':'disabled'}>${remote.ready?'开启异地访问':'连接私人网络'}</button>`:`<label>连接网络<select data-address aria-label="连接网络">${(data.addresses||[]).map(row=>`<option value="${esc(row.address)}">${esc(row.label)} · ${esc(row.address)}</option>`).join('')}</select></label><p>手机和电脑连接同一 Wi-Fi，扫码后可上传材料、查看原件和核对费用。</p>${data.addresses?.length?'':'<p>未找到局域网地址，请先让电脑连上 Wi-Fi，再刷新。</p>'}<button type="button" class="primary" data-start ${data.addresses?.length?'':'disabled'}>开启手机连接</button>`}<div class="actions"><button type="button" data-status>刷新连接状态</button></div>`;
        content.querySelector('[data-mode]').value=mode;
        content.querySelector('[data-mode]').onchange=e=>{mode=e.target.value;render(latest);};
        const start=content.querySelector('[data-start]');if(start)start.onclick=()=>run(()=>request('/start',{address:content.querySelector('[data-address]').value}));
        const connect=content.querySelector('[data-remote]');if(connect)connect.onclick=()=>run(()=>request(remote.ready?'/start':'/connect',{mode:'remote'}));
      }
      content.querySelector('[data-status]').onclick=()=>run(()=>request(''));
    }
    async function run(fn){
      if(busy)return;busy=true;error.hidden=true;dialog.querySelectorAll('button,select').forEach(b=>b.disabled=true);
      try{const result=await fn();render(result.enabled?result:await request(''));if(result.warning){error.textContent=result.warning;error.hidden=false;}}
      catch(e){error.textContent=e.message;error.hidden=false;}
      finally{busy=false;dialog.querySelectorAll('button,select').forEach(b=>b.disabled=false);const start=content.querySelector('[data-start]');if(start&&!content.querySelector('[data-address] option'))start.disabled=true;const remote=content.querySelector('[data-remote]');if(remote&&!latest?.remote?.installed)remote.disabled=true;}
    }
    await run(()=>request(''));
  };
})();
