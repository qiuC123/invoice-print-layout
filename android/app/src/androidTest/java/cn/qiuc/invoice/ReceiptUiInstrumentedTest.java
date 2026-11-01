package cn.qiuc.invoice;

import android.app.Activity;
import android.content.*;
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

/** Real WebView + native bridge + bundled ML Kit + synthetic computer; injects a photo. */
@RunWith(AndroidJUnit4.class)
public class ReceiptUiInstrumentedTest {
    private MainActivity activity;private WebView web;
    private final String base=InstrumentationRegistry.getArguments().getString("receipt_url","http://10.0.2.2:8769");
    private String js(String code)throws Exception{CountDownLatch done=new CountDownLatch(1);String[] value={""};InstrumentationRegistry.getInstrumentation().runOnMainSync(()->web.evaluateJavascript(code,v->{value[0]=v;done.countDown();}));assertTrue(done.await(10,TimeUnit.SECONDS));return value[0];}
    private void until(String expression)throws Exception{long end=System.currentTimeMillis()+20000;while(System.currentTimeMillis()<end){if("true".equals(js(expression)))return;Thread.sleep(150);}fail("UI condition timed out: "+expression+"; actual="+js("document.body.innerText"));}
    private void mode(String value)throws Exception{HttpURLConnection c=(HttpURLConnection)new URL(base+"/__control").openConnection(java.net.Proxy.NO_PROXY);try{c.setConnectTimeout(3000);c.setReadTimeout(3000);c.setRequestMethod("POST");c.setDoOutput(true);try(OutputStream out=c.getOutputStream()){out.write(("{\"mode\":\""+value+"\"}").getBytes(StandardCharsets.UTF_8));}assertEquals(200,c.getResponseCode());}finally{c.disconnect();}}
    private void screenshot(String name)throws Exception{Thread.sleep(400);Bitmap image=InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();assertNotNull(image);File folder=new File(activity.getCacheDir(),"receipt-ui-evidence");assertTrue(folder.isDirectory()||folder.mkdirs());try(OutputStream out=new FileOutputStream(new File(folder,name+".png"))){assertTrue(image.compress(Bitmap.CompressFormat.PNG,100,out));}finally{image.recycle();}}
    @Test public void photoConfirmationOfflineSaveAndForegroundAutomaticUpload()throws Exception{
        mode("normal");
        try(ActivityScenario<MainActivity> scenario=ActivityScenario.launch(MainActivity.class)){
            scenario.onActivity(a->{activity=a;try{Field field=MainActivity.class.getDeclaredField("web");field.setAccessible(true);web=(WebView)field.get(a);}catch(Exception e){throw new RuntimeException(e);}});
            until("!!document.getElementById('receiptCamera')");assertEquals("\"receipts\"",js("tab"));
            String ledger=js("JSON.stringify({projects:state.projects,materials:state.materials,expenses:state.expenses,reports:state.reports})");
            js("setTab('settings');receiptPairDialog();document.querySelector('[name=url]').value="+JSONObject.quote(base)+";document.querySelector('[name=code]').value='12345678';document.getElementById('dialogForm').requestSubmit();");
            until("!document.getElementById('dialog').open && !!state.receipt_inbox?.paired");js("receiptPairDialog()");screenshot("01-paired-computer");js("document.getElementById('dialog').close();setTab('receipts')");screenshot("02-inbox");
            File photo=new File(activity.getCacheDir(),"receipt-ui-synthetic.jpg");Bitmap b=Bitmap.createBitmap(1400,1000,Bitmap.Config.ARGB_8888);Canvas canvas=new Canvas(b);canvas.drawColor(Color.WHITE);Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);paint.setColor(Color.BLACK);paint.setTypeface(Typeface.createFromAsset(activity.getAssets(),"fonts/NotoSansSC.ttf"));paint.setTextSize(65);canvas.drawText("收据",80,150,paint);canvas.drawText("客房费",80,300,paint);canvas.drawText("收据合计 ￥89.30",80,500,paint);paint.setTextSize(30);canvas.drawText("合成验收材料，无真实消费",80,750,paint);try(OutputStream out=new FileOutputStream(photo)){b.compress(Bitmap.CompressFormat.JPEG,98,out);}finally{b.recycle();}
            Method inject=MainActivity.class.getDeclaredMethod("receiptPhoto",File.class,String.class);inject.setAccessible(true);JSONObject capture=(JSONObject)inject.invoke(activity,photo,"2026-11-01T10:00:00");js("receiptConfirm("+capture+")");
            until("document.getElementById('receiptOcrStatus').textContent.includes('识别完成')");assertEquals("\"89.30\"",js("document.querySelector('[name=amount]').value"));assertEquals("\"酒店\"",js("document.querySelector('[name=category]').value"));assertEquals("\"lead\"",js("document.querySelector('[name=person_id]').value"));js("document.querySelector('[name=note]').value='设备合成验收'");screenshot("03-confirmation-local-ocr");
            mode("disconnected");long savedAt=System.currentTimeMillis();js("document.getElementById('dialogForm').requestSubmit()");until("!document.getElementById('dialog').open && state.receipt_inbox.rows.some(r=>r.token===undefined && r.note==='设备合成验收' && r.status==='pending')");assertTrue("saving must not wait for network",System.currentTimeMillis()-savedAt<3000);screenshot("04-pending-offline");
            mode("normal");js("window.__resumeF22=state.receipt_inbox.rows.find(r=>r.note==='设备合成验收').client_id");
            // Exercise Activity foreground retry, rather than calling the Retry bridge.
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.CREATED);scenario.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED);
            until("state.receipt_inbox.rows.some(r=>r.client_id===window.__resumeF22 && r.status==='received')");screenshot("05-received-on-computer");
            String firstId=(String)new JSONTokener(js("window.__resumeF22")).nextValue();File jobPhoto=new File(activity.getCacheDir(),"receipt-job-synthetic.jpg");
            try(ReceiptStore store=new ReceiptStore(activity)){java.nio.file.Files.copy(new File(store.get(firstId).getString("file")).toPath(),jobPhoto.toPath(),java.nio.file.StandardCopyOption.REPLACE_EXISTING);}
            JSONObject second=(JSONObject)inject.invoke(activity,jobPhoto,"2026-11-01T10:01:00");js("receiptConfirm("+second+");document.querySelector('[name=note]').value='后台任务合成验收'");mode("disconnected");js("document.getElementById('dialogForm').requestSubmit()");
            until("!document.getElementById('dialog').open && state.receipt_inbox.rows.some(r=>r.note==='后台任务合成验收' && !!r.error)");
            String jobId=(String)new JSONTokener(js("state.receipt_inbox.rows.find(r=>r.note==='后台任务合成验收').client_id")).nextValue();
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.CREATED);mode("normal");
            // Make the saved retry due, then ask Android to execute the actual network job now.
            try(ReceiptStore store=new ReceiptStore(activity)){store.status(jobId,"pending","",0,0);}
            android.os.ParcelFileDescriptor command=InstrumentationRegistry.getInstrumentation().getUiAutomation().executeShellCommand("cmd jobscheduler run -f "+activity.getPackageName()+" 2203");
            try(InputStream in=new android.os.ParcelFileDescriptor.AutoCloseInputStream(command);ByteArrayOutputStream out=new ByteArrayOutputStream()){in.transferTo(out);assertTrue(out.toString("UTF-8").contains("Running job"));}
            long deadline=System.currentTimeMillis()+20000;boolean received=false;while(System.currentTimeMillis()<deadline){try(ReceiptStore store=new ReceiptStore(activity)){received=store.get(jobId).getString("status").equals("received");}if(received)break;Thread.sleep(200);}assertTrue("JobScheduler must upload while the Activity is stopped",received);scenario.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED);
            assertEquals(ledger,js("JSON.stringify({projects:state.projects,materials:state.materials,expenses:state.expenses,reports:state.reports})"));
        }finally{mode("normal");}
    }
}
