package cn.qiuc.invoice;

import android.app.job.*;
import android.content.*;
import android.database.DatabaseErrorHandler;
import android.database.sqlite.SQLiteDatabase;
import android.graphics.*;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import org.junit.*;
import org.junit.runner.RunWith;
import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import static org.junit.Assert.*;

/** Real Android SQLite/Keystore/HTTP against android/tests/fake_computer.py only. */
@RunWith(AndroidJUnit4.class)
public class ReceiptInstrumentedTest {
    private Context context;private ReceiptInbox inbox;private File photo;private String prefix;
    private final String base=InstrumentationRegistry.getArguments().getString("receipt_url","http://10.0.2.2:8769");
    @Before public void setup()throws Exception{
        Context target=InstrumentationRegistry.getInstrumentation().getTargetContext();prefix="receipt-test-"+UUID.randomUUID();File directory=new File(target.getCacheDir(),prefix);assertTrue(directory.mkdirs());
        context=new ContextWrapper(target){
            @Override public File getFilesDir(){return directory;}
            @Override public SharedPreferences getSharedPreferences(String name,int mode){return target.getSharedPreferences(prefix+name,mode);}
            @Override public File getDatabasePath(String name){return new File(directory,name);}
            @Override public SQLiteDatabase openOrCreateDatabase(String name,int mode,SQLiteDatabase.CursorFactory f){return SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name),f);}
            @Override public SQLiteDatabase openOrCreateDatabase(String name,int mode,SQLiteDatabase.CursorFactory f,DatabaseErrorHandler h){return SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name).getPath(),f,h);}
        };
        inbox=new ReceiptInbox(context);photo=new File(directory,"synthetic.jpg");Bitmap b=Bitmap.createBitmap(900,600,Bitmap.Config.ARGB_8888);Canvas canvas=new Canvas(b);canvas.drawColor(Color.WHITE);Paint paint=new Paint();paint.setColor(Color.BLACK);paint.setTextSize(38);canvas.drawText("SYNTHETIC RECEIPT "+prefix,20,100,paint);canvas.drawText("Amount 89.30",20,180,paint);try(OutputStream out=new FileOutputStream(photo)){b.compress(Bitmap.CompressFormat.JPEG,98,out);}finally{b.recycle();}mode("normal");
    }
    @After public void cleanup()throws Exception{mode("normal");if(inbox!=null)inbox.close();if(context!=null){for(String name:List.of("receipt-inbox","optional-services"))context.getSharedPreferences(name,0).edit().clear().commit();PortableBackup.erase(context.getFilesDir());}}
    private JSONObject control(String path,JSONObject body)throws Exception{HttpURLConnection c=(HttpURLConnection)new URL(base+path).openConnection(Proxy.NO_PROXY);c.setConnectTimeout(3000);c.setReadTimeout(5000);try{if(body!=null){c.setRequestMethod("POST");c.setDoOutput(true);try(OutputStream out=c.getOutputStream()){out.write(body.toString().getBytes(StandardCharsets.UTF_8));}}try(InputStream in=c.getInputStream();ByteArrayOutputStream bytes=new ByteArrayOutputStream()){in.transferTo(bytes);return new JSONObject(bytes.toString("UTF-8"));}}finally{c.disconnect();}}
    private void mode(String value)throws Exception{control("/__control",new JSONObject().put("mode",value));}
    private void pair()throws Exception{inbox.pair(ReceiptRules.pairing("invoicepair:1?code=12345678&url="+base),"合成验收手机");}
    private JSONObject fields()throws Exception{return new JSONObject().put("amount","89.30").put("category","餐饮").put("project_id","synthetic-project").put("person_id","lead").put("note","合成收票备注");}
    private String save()throws Exception{return inbox.save(photo,"2026-11-01T10:00:00",fields());}
    private JSONObject row()throws Exception{return inbox.state().getJSONArray("rows").getJSONObject(0);}
    @Test public void neverPairedCanSaveUnknownAmountAndSelectProjectLater()throws Exception{
        JSONObject defaults=inbox.defaults("2026-11-01T10:00:00","其他");assertEquals("",defaults.getString("project_id"));assertEquals(0,defaults.getJSONObject("catalog").getJSONArray("projects").length());
        String id=inbox.save(photo,"2026-11-01T10:00:00",fields().put("amount","").put("project_id","").put("person_id",""));UUID.fromString(id);assertEquals("reselect",row().getString("status"));assertTrue(row().isNull("amount_cents"));
        inbox.close();inbox=new ReceiptInbox(context);assertEquals(id,row().getString("client_id"));assertFalse(new File(context.getFilesDir(),"originals").exists());
        pair();inbox.edit(id,fields());inbox.sync(true);assertEquals("received",row().getString("status"));
    }
    @Test public void pairingCachesCatalogDefaultsAndEncryptsCredentials()throws Exception{
        pair();JSONObject s=inbox.state();assertTrue(s.getBoolean("paired"));assertEquals(2,s.getJSONObject("catalog").getJSONArray("projects").length());assertFalse(s.toString().contains("A".repeat(43)));String stored=context.getSharedPreferences("receipt-inbox",0).getString("key","");assertFalse(stored.contains("A".repeat(43)));assertTrue(stored.contains("iv"));
        JSONObject d=inbox.defaults("2026-11-01T10:00:00","酒店");assertEquals("synthetic-project",d.getString("project_id"));assertEquals("lead",d.getJSONObject("people_defaults").getString("synthetic-project"));assertEquals("other-lead",d.getJSONObject("people_defaults").getString("synthetic-other"));
        inbox.unpair();assertFalse(inbox.state().getBoolean("paired"));
    }
    @Test public void requestTimeoutsMatchPairCatalogAndUploadBudgets()throws Exception{
        for(String route:List.of("pair","catalog","receipts")){
            HttpURLConnection c=ReceiptInbox.connection(base,route);
            try{assertEquals(5000,c.getConnectTimeout());assertEquals(route.equals("receipts")?60000:15000,c.getReadTimeout());assertFalse(c.getInstanceFollowRedirects());}
            finally{c.disconnect();}
        }
    }
    @Test public void catalogFailureAfterPairingRequiresNewDesktopCode()throws Exception{
        mode("catalog_failure");
        try{pair();fail("pair must fail when initial catalog fails");}
        catch(IOException error){assertEquals("配对未完成，请在电脑重新生成配对码",error.getMessage());}
        assertFalse(inbox.state().getBoolean("paired"));
        assertEquals(0,inbox.catalog().getJSONArray("projects").length());
        mode("normal");pair();assertTrue(inbox.state().getBoolean("paired"));
    }
    @Test public void disconnectedSaveAndRecoveryPreserveEveryFieldAndOriginalBytes()throws Exception{
        pair();mode("disconnected");String id=save();inbox.sync(true);assertEquals("pending",row().getString("status"));assertTrue(row().getLong("next_try")>System.currentTimeMillis());
        inbox.close();inbox=new ReceiptInbox(context);assertEquals(id,row().getString("client_id"));assertEquals(2,inbox.catalog().getJSONArray("projects").length());mode("normal");inbox.sync(true);assertEquals("received",row().getString("status"));
        JSONObject received=control("/__state",null).getJSONObject("entries").getJSONObject(id);assertEquals(PrintExport.sha(photo),received.getString("sha256"));JSONObject f=received.getJSONObject("fields");assertEquals(8930,f.getLong("amount_cents"));assertEquals("餐饮",f.getString("category"));assertEquals("synthetic-project",f.getString("project_id"));assertEquals("lead",f.getString("person_id"));assertEquals("合成收票备注",f.getString("note"));assertEquals("2026-11-01T10:00:00",f.getString("captured_at"));assertFalse(f.has("project_name"));inbox.unpair();assertEquals("收票验收项目",row().getString("project_name"));
    }
    @Test public void projectAndPerson409RequireSelectionBeforeRetry()throws Exception{
        for(String conflict:List.of("project_unavailable","person_unavailable")){pair();mode(conflict);String id=save();inbox.sync(true);try(ReceiptStore store=new ReceiptStore(context)){assertEquals("reselect",store.get(id).getString("status"));}mode("normal");inbox.edit(id,fields().put("project_id","synthetic-other").put("person_id","other-lead"));inbox.sync(true);try(ReceiptStore store=new ReceiptStore(context)){assertEquals("received",store.get(id).getString("status"));}}
    }
    @Test public void unauthorizedPausesAndPairingAgainResumes()throws Exception{
        pair();String id=save();mode("unpaired");inbox.sync(true);assertTrue(inbox.state().getBoolean("needs_pairing"));assertEquals("pending",row().getString("status"));mode("normal");pair();assertFalse(inbox.state().getBoolean("needs_pairing"));assertEquals("received",row().getString("status"));assertEquals(id,row().getString("client_id"));
    }
    @Test public void errorTextAndIdempotentRetransmissionSurviveLostAcknowledgement()throws Exception{
        pair();String id=save();mode("invalid");inbox.sync(true);assertEquals("failed",row().getString("status"));assertEquals("合成字段不合法",row().getString("error"));mode("drop_receipt_reply");inbox.sync(true);inbox.sync(true);assertEquals("received",row().getString("status"));JSONObject state=control("/__state",null);assertTrue(state.getJSONObject("entries").has(id));int count=0;JSONArray attempts=state.getJSONArray("attempts");for(int i=0;i<attempts.length();i++)if(id.equals(attempts.getString(i)))count++;assertTrue("must retry same client id",count>=2);
        assertThrows(IllegalArgumentException.class,()->inbox.delete(id));
    }
    @Test public void periodicJobHasNetworkConstraintAndMinimumPeriod(){Context target=InstrumentationRegistry.getInstrumentation().getTargetContext();ReceiptUploadJob.schedule(target);JobInfo job=target.getSystemService(JobScheduler.class).getPendingJob(2203);assertNotNull(job);assertEquals(JobInfo.NETWORK_TYPE_ANY,job.getNetworkType());assertEquals(15*60_000L,job.getIntervalMillis());}
    @Test public void stoppedJobLeavesPendingPhotosForNextRetry()throws Exception{
        pair();String id=save();Thread task=new Thread(()->{Thread.currentThread().interrupt();try{inbox.sync(true);}catch(Exception e){throw new AssertionError(e);}});task.start();task.join(2000);assertFalse(task.isAlive());assertEquals("pending",row().getString("status"));assertFalse(control("/__state",null).getJSONObject("entries").has(id));
    }
    @Test public void bundledPairingQrAndOrderedAddressFallback()throws Exception{
        String text="invoicepair:1?code=12345678&url=http://127.0.0.2:8769&url="+base;
        com.google.zxing.common.BitMatrix matrix=new com.google.zxing.qrcode.QRCodeWriter().encode(text,com.google.zxing.BarcodeFormat.QR_CODE,900,900);
        Bitmap b=Bitmap.createBitmap(900,900,Bitmap.Config.ARGB_8888);for(int y=0;y<900;y++)for(int x=0;x<900;x++)b.setPixel(x,y,matrix.get(x,y)?Color.BLACK:Color.WHITE);
        File qr=new File(context.getFilesDir(),"pair-qr.jpg");try(OutputStream out=new FileOutputStream(qr)){b.compress(Bitmap.CompressFormat.JPEG,100,out);}finally{b.recycle();}
        JSONArray values=PhoneIntake.qr(qr);assertEquals(text,values.getString(0));inbox.pair(ReceiptRules.pairing(values.getString(0)),"合成二维码验收");assertTrue(inbox.state().getBoolean("paired"));
    }
    @Test public void overTwentyMegabytesShrinksOnlyUploadAndPreservesPhoneOriginal()throws Exception{
        pair();try(OutputStream out=new FileOutputStream(photo,true)){byte[] padding=new byte[65536];for(int i=0;i<337;i++)out.write(padding);}assertTrue(photo.length()>20L*1024*1024);String digest=PrintExport.sha(photo),id=save();inbox.sync(true);assertEquals("received",row().getString("status"));assertEquals(digest,PrintExport.sha(photo));JSONObject received=control("/__state",null).getJSONObject("entries").getJSONObject(id);assertTrue(received.getLong("bytes")<20L*1024*1024);try(ReceiptStore store=new ReceiptStore(context)){assertEquals(digest,PrintExport.sha(new File(store.get(id).getString("file"))));}
    }
}
