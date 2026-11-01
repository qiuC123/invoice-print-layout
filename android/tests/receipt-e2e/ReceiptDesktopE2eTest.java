package cn.qiuc.invoice;

import android.graphics.*;
import android.webkit.WebView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import org.json.*;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.*;
import java.lang.reflect.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import static org.junit.Assert.*;

/** Real merged desktop + installed Android app. Only the camera photo is synthetic. */
@RunWith(AndroidJUnit4.class)
public class ReceiptDesktopE2eTest {
    private MainActivity activity;
    private WebView web;
    private final JSONArray results=new JSONArray();
    private final String control=InstrumentationRegistry.getArguments().getString("e2e_control");
    private JSONObject context;
    private String js(String code)throws Exception {
        CountDownLatch done=new CountDownLatch(1);String[] result={""};
        InstrumentationRegistry.getInstrumentation().runOnMainSync(()->web.evaluateJavascript(code,v->{result[0]=v;done.countDown();}));
        assertTrue(done.await(10,TimeUnit.SECONDS));return result[0];
    }
    private void until(String expression)throws Exception {
        long end=System.currentTimeMillis()+60000;
        while(System.currentTimeMillis()<end){if("true".equals(js(expression)))return;Thread.sleep(200);}
        screenshot("failure");fail("Timed out: "+expression+"; UI="+js("document.body.innerText"));
    }
    private Object value(String code)throws Exception{return new JSONTokener(js(code)).nextValue();}
    private JSONObject async(String expression)throws Exception {
        js("window.__e2eDone=false;window.__e2eValue={};Promise.resolve("+expression+").then(v=>{window.__e2eValue=v;window.__e2eDone=true}).catch(e=>{window.__e2eValue={error:e.message};window.__e2eDone=true})");
        until("window.__e2eDone===true");Object result=new JSONTokener((String)value("JSON.stringify(window.__e2eValue??{})")).nextValue();
        return result instanceof JSONObject?(JSONObject)result:new JSONObject().put("value",result);
    }
    private JSONObject host(String path,JSONObject body)throws Exception {
        HttpURLConnection c=(HttpURLConnection)new URL(control+path).openConnection(java.net.Proxy.NO_PROXY);
        c.setConnectTimeout(5000);c.setReadTimeout(90000);
        try{if(body!=null){c.setRequestMethod("POST");c.setDoOutput(true);try(OutputStream out=c.getOutputStream()){out.write(body.toString().getBytes(StandardCharsets.UTF_8));}}
            try(InputStream in=c.getInputStream();ByteArrayOutputStream out=new ByteArrayOutputStream()){in.transferTo(out);return new JSONObject(out.toString("UTF-8"));}
        }finally{c.disconnect();}
    }
    private void screenshot(String name)throws Exception {
        Thread.sleep(350);Bitmap image=InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();assertNotNull(image);
        File folder=new File(activity.getCacheDir(),"receipt-desktop-e2e");assertTrue(folder.isDirectory()||folder.mkdirs());
        try(OutputStream out=new FileOutputStream(new File(folder,name+".png"))){assertTrue(image.compress(Bitmap.CompressFormat.PNG,100,out));}finally{image.recycle();}
    }
    private void check(String name,boolean passed,Object actual)throws Exception {
        results.put(new JSONObject().put("check",name).put("passed",passed).put("actual",actual));
    }
    private JSONObject checkpoint(String name,boolean desktop)throws Exception {
        return host("/checkpoint",new JSONObject().put("name",name).put("desktop_screenshot",desktop)
            .put("checks",results).put("receipt_inbox",new JSONObject((String)value("JSON.stringify(state.receipt_inbox)"))));
    }
    private void photo(String name)throws Exception {
        js("setTab('receipts')");
        File file=new File(activity.getCacheDir(),"e2e-"+name+".jpg");
        Bitmap b=Bitmap.createBitmap(1400,1000,Bitmap.Config.ARGB_8888);Canvas canvas=new Canvas(b);canvas.drawColor(Color.WHITE);
        Paint p=new Paint(Paint.ANTI_ALIAS_FLAG);p.setColor(Color.BLACK);p.setTypeface(Typeface.createFromAsset(activity.getAssets(),"fonts/NotoSansSC.ttf"));p.setTextSize(65);
        canvas.drawText("收据",80,150,p);canvas.drawText("客房费",80,300,p);canvas.drawText("收据合计 ￥89.30",80,500,p);
        p.setTextSize(30);canvas.drawText("合成验收材料，无真实消费 "+name,80,750,p);
        try(OutputStream out=new FileOutputStream(file)){b.compress(Bitmap.CompressFormat.JPEG,98,out);}finally{b.recycle();}
        Method inject=MainActivity.class.getDeclaredMethod("receiptPhoto",File.class,String.class);inject.setAccessible(true);
        JSONObject capture=(JSONObject)inject.invoke(activity,file,"2026-11-01T10:00:00");
        js("receiptConfirm("+capture+")");until("document.getElementById('receiptOcrStatus').textContent.includes('识别完成')");
    }
    private void fill(String field,String text)throws Exception {
        js("document.querySelector('[name="+field+"]').value="+JSONObject.quote(text)+";document.querySelector('[name="+field+"]').dispatchEvent(new Event('"+(field.equals("category")||field.endsWith("_id")?"change":"input")+"',{bubbles:true}))");
    }
    private String save(String note)throws Exception {
        fill("note",note);js("document.getElementById('dialogForm').requestSubmit()");
        until("!document.getElementById('dialog').open && state.receipt_inbox.rows.some(r=>r.note==="+JSONObject.quote(note)+")");
        return (String)value("state.receipt_inbox.rows.find(r=>r.note==="+JSONObject.quote(note)+").client_id");
    }
    private void received(String id)throws Exception {until("state.receipt_inbox.rows.some(r=>r.client_id==="+JSONObject.quote(id)+"&&r.status==='received')");}
    private void foreground(ActivityScenario<MainActivity> scenario)throws Exception {
        scenario.moveToState(androidx.lifecycle.Lifecycle.State.CREATED);
        scenario.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED);
    }
    private int count(JSONObject snapshot,String id)throws Exception {
        int n=0;JSONArray uploads=snapshot.getJSONArray("uploads");for(int i=0;i<uploads.length();i++)if(id.equals(uploads.getJSONObject(i).getString("client_id")))n++;return n;
    }
    @Test public void mergedDesktopPhotoInboxEndToEnd()throws Exception {
        context=host("/context",null);
        try(ActivityScenario<MainActivity> scenario=ActivityScenario.launch(MainActivity.class)){
            scenario.onActivity(a->{activity=a;try{Field f=MainActivity.class.getDeclaredField("web");f.setAccessible(true);web=(WebView)f.get(a);}catch(Exception e){throw new RuntimeException(e);}});
            until("!!document.getElementById('receiptCamera')");
            try {
                // Parse the real desktop pair_uri, using the same Android parser as QR pairing.
                ReceiptRules.Pairing parsed=ReceiptRules.pairing(context.getString("pair_uri"));
                check("1 pair_uri parser",parsed.code().equals(context.getString("code"))&&parsed.urls().contains(context.getString("device_url")),parsed.urls().toString());
                js("document.getElementById('receiptConnect').click()");fill("url",context.getString("device_url"));fill("code",context.getString("code"));screenshot("01-app-manual-pairing");
                long pairingStarted=System.currentTimeMillis();
                js("document.getElementById('dialogForm').requestSubmit()");until("!document.getElementById('dialog').open && state.receipt_inbox.paired");
                long pairAndCatalogMs=System.currentTimeMillis()-pairingStarted;
                HttpURLConnection catalogConnection=ReceiptInbox.connection(context.getString("device_url"),"catalog");
                String key=new OptionalServices(activity).unseal(activity.getSharedPreferences("receipt-inbox",0).getString("key",""));
                catalogConnection.setRequestProperty("Authorization","Bearer "+key);
                long catalogStarted=System.currentTimeMillis();
                try{assertEquals(200,catalogConnection.getResponseCode());try(InputStream in=catalogConnection.getInputStream()){assertTrue(in.readAllBytes().length>0);}}
                finally{catalogConnection.disconnect();}
                host("/checkpoint",new JSONObject().put("name","pairing-timing").put("pair_and_initial_catalog_ms",pairAndCatalogMs)
                    .put("catalog_round_trip_ms",System.currentTimeMillis()-catalogStarted));
                js("receiptPairDialog()");screenshot("02-app-paired");js("document.getElementById('dialog').close();setTab('receipts')");
                check("1 manual pairing",true,value("state.receipt_inbox.server_name"));
                JSONObject catalog=new JSONObject((String)value("JSON.stringify(state.receipt_inbox.catalog)"));
                check("2 catalog projects people main",catalog.getJSONArray("projects").toString().equals(context.getJSONObject("expected_catalog").getJSONArray("projects").toString()),catalog.getJSONArray("projects"));
                check("2 categories",catalog.getJSONArray("categories").toString().equals(context.getJSONObject("expected_catalog").getJSONArray("categories").toString()),catalog.getJSONArray("categories"));
                photo("online");
                check("3 OCR amount",value("document.querySelector('[name=amount]').value").equals("89.30"),value("document.querySelector('[name=amount]').value"));
                check("3 OCR category",value("document.querySelector('[name=category]').value").equals("酒店"),value("document.querySelector('[name=category]').value"));
                String primary=context.getJSONObject("primary").getString("id"),main=context.getJSONObject("primary").getString("main_person_id"),helper=context.getJSONObject("helper").getString("id"),alternate=context.getJSONObject("alternate").getString("id");
                check("2 default project date",primary.equals(value("document.querySelector('[name=project_id]').value")),value("document.querySelector('[name=project_id]').value"));
                check("2 default main person",main.equals(value("document.querySelector('[name=person_id]').value")),value("document.querySelector('[name=person_id]').value"));
                screenshot("03-app-confirmation-ocr-defaults");
                JSONObject next=async("api('receiptDefaults',{captured_at:'2026-11-02T10:00:00'})");
                check("2 unique date chooses alternate",alternate.equals(next.getString("project_id")),next.getString("project_id"));
                fill("amount","91.23");fill("category","餐饮");fill("person_id",helper);fill("note","E2E人工确认备注");screenshot("04-app-confirmation-edited");
                String first=save("E2E人工确认备注");received(first);screenshot("05-app-received");
                JSONObject firstPc=checkpoint("online",true);JSONArray entries=firstPc.getJSONArray("entries");JSONObject firstEntry=entries.getJSONObject(0),fields=firstEntry.getJSONObject("fields");
                check("4 desktop confirmed fields",fields.optString("amount").equals("91.23")&&fields.optString("category").equals("餐饮")&&helper.equals(firstEntry.optString("reimbursement_person_id")),firstEntry);
                check("4 separate intake note",firstEntry.optString("intake_note").equals("E2E人工确认备注")&&!fields.optString("note").contains("E2E人工确认备注"),fields);
                check("4 desktop screenshot",firstPc.getJSONObject("screenshot").getInt("exit_code")==0,firstPc.getJSONObject("screenshot"));

                JSONObject beforePause=host("/pause",new JSONObject());
                photo("offline");long started=System.currentTimeMillis();String offline=save("E2E离线补传");
                check("5 offline save returns promptly",System.currentTimeMillis()-started<3000,System.currentTimeMillis()-started);
                until("state.receipt_inbox.rows.some(r=>r.client_id==="+JSONObject.quote(offline)+"&&r.status==='pending')");screenshot("06-app-offline-pending");
                checkpoint("offline-pending",false);JSONObject afterResume=host("/resume",new JSONObject());
                check("5 same device key",beforePause.getJSONArray("key_hashes").toString().equals(afterResume.getJSONArray("key_hashes").toString()),afterResume.getJSONArray("key_hashes"));
                foreground(scenario);received(offline);screenshot("07-app-foreground-received");
                // Another foreground retry must leave exactly one desktop intake record.
                foreground(scenario);Thread.sleep(1500);JSONObject offlinePc=checkpoint("offline-recovered",false);
                check("5 desktop exactly once",count(offlinePc,offline)==1,count(offlinePc,offline));

                photo("reselect");fill("project_id",primary);fill("note","E2E项目重选");host("/reimburse",new JSONObject());String reselect=save("E2E项目重选");
                until("state.receipt_inbox.rows.some(r=>r.client_id==="+JSONObject.quote(reselect)+"&&r.status==='reselect')");screenshot("08-app-reselect");
                checkpoint("project-reselect",false);check("7 unavailable project needs selection",true,reselect);
                js("document.querySelector('[data-receipt-edit=\""+reselect+"\"]').click()");until("document.getElementById('dialog').open");fill("project_id",alternate);screenshot("09-app-reselect-alternate");
                js("document.getElementById('dialogForm').requestSubmit()");received(reselect);screenshot("10-app-reselected-received");JSONObject reselected=checkpoint("project-reselected",false);
                check("7 retry after project change",count(reselected,reselect)==1,count(reselected,reselect));

                host("/revoke",new JSONObject());photo("revoked");String revoked=save("E2E撤销设备");
                until("state.receipt_inbox.needs_pairing || state.receipt_inbox.rows.some(r=>r.client_id==="+JSONObject.quote(revoked)+"&&!!r.error)");
                check("6 revoked device prompts pairing",Boolean.TRUE.equals(value("state.receipt_inbox.needs_pairing")),value("JSON.stringify(state.receipt_inbox)"));
                screenshot("11-app-revoked-last-device");checkpoint("revoked-last-device",true);
                // No new pairing code: the revoked device must reach the still-running listener.
                JSONObject revokedPc=host("/snapshot",new JSONObject());
                until("state.receipt_inbox.needs_pairing");screenshot("12-app-revoked-401");
                check("6 401 on listening service",revokedPc.getBoolean("listener"),value("state.receipt_inbox.connection_error"));
                checkpoint("final-results",false);
            } finally {
                if(web!=null){async("api('receiptUnpair')");async("refresh()");check("cleanup unpaired",Boolean.FALSE.equals(value("state.receipt_inbox.paired")),value("state.receipt_inbox.paired"));screenshot("13-app-unpaired");checkpoint("app-cleanup",false);}
            }
        }
        int failed=0;for(int i=0;i<results.length();i++)if(!results.getJSONObject(i).getBoolean("passed"))failed++;
        assertEquals("See e2e checkpoint evidence for failed business checks",0,failed);
    }
}
