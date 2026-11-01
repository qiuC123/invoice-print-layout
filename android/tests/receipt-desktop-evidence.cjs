'use strict';
// Screenshots of the actual desktop service; no route mocks or fake bridge.
const fs = require('node:fs/promises'), path = require('node:path');
const {chromium} = require('playwright');
(async () => {
  const [origin, project, out, name] = process.argv.slice(2);
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1100}});
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  try {
    await page.goto(origin+'/?project='+encodeURIComponent(project));
    await page.locator('#phoneLink').click();
    await page.getByRole('region',{name:'收票 App',exact:true}).getByRole('button',{name:'刷新设备状态',exact:true}).waitFor();
    await page.screenshot({path:path.join(out,name+'-desktop-devices.png')});
    const devices=await page.locator('#phoneLinkDialog').innerText();
    await page.locator('#phoneLinkDialog').getByRole('button',{name:'完成',exact:true}).click();
    await page.locator('#materialMatching').click();
    await page.locator('#matchingDeskDialog').waitFor();
    await page.locator('#matchingRefresh').click();
    await page.waitForTimeout(600);
    await page.screenshot({path:path.join(out,name+'-desktop-synthesis.png')});
    const cards=await page.locator('.matching-card-shell').allTextContents();
    const result={devices,cards,errors};
    await fs.writeFile(path.join(out,name+'-desktop-ui.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
