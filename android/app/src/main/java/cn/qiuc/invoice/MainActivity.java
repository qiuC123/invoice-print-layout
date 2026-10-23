package cn.qiuc.invoice;

import android.app.*;
import android.content.*;
import android.graphics.*;
import android.graphics.pdf.PdfRenderer;
import android.net.Uri;
import android.os.*;
import android.provider.OpenableColumns;
import android.webkit.*;
import android.widget.*;
import android.view.*;
import androidx.core.content.FileProvider;
import com.tom_roush.pdfbox.android.PDFBoxResourceLoader;
import org.json.*;
import java.io.*;
import java.nio.file.Files;
import java.util.*;
import java.util.concurrent.*;

public final class MainActivity extends Activity {
    private WebView web; private Ledger ledger; private LocalRecognition recognition;private Workbench workbench;private ReportService reports;private PortableBackup backups;private OptionalServices services;
    private final ExecutorService worker=Executors.newSingleThreadExecutor();
    private String importRequest="",importProject="",exportRequest="";private File exportFile,cameraFile,restoreFile;
    private final List<Uri> shared=Collections.synchronizedList(new ArrayList<>());
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);PDFBoxResourceLoader.init(this);ledger=new Ledger(this);recognition=new LocalRecognition(this);workbench=new Workbench(ledger);reports=new ReportService(this,ledger);backups=new PortableBackup(this,ledger);services=new OptionalServices(this);
        if(state!=null){importRequest=state.getString("importRequest","");importProject=state.getString("importProject","");String camera=state.getString("cameraFile","");if(!camera.isEmpty())cameraFile=new File(camera);}
        web=new WebView(this);web.setBackgroundColor(Color.rgb(247,248,250));WebSettings settings=web.getSettings();settings.setJavaScriptEnabled(true);settings.setDomStorageEnabled(true);settings.setAllowFileAccess(false);settings.setAllowContentAccess(false);settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setWebViewClient(new WebViewClient(){@Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request){return true;}});
        web.addJavascriptInterface(new Bridge(),"Native");web.setWebChromeClient(new WebChromeClient());
        FrameLayout container=new FrameLayout(this);container.setBackgroundColor(Color.WHITE);container.addView(web,new FrameLayout.LayoutParams(-1,-1));setContentView(container);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR|View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        // WebView does not honor content padding reliably; inset its native parent.
        container.setOnApplyWindowInsetsListener((v,insets)->{if(Build.VERSION.SDK_INT>=30){android.graphics.Insets bars=insets.getInsets(WindowInsets.Type.systemBars()|WindowInsets.Type.ime());v.setPadding(bars.left,bars.top,bars.right,bars.bottom);}else v.setPadding(insets.getSystemWindowInsetLeft(),insets.getSystemWindowInsetTop(),insets.getSystemWindowInsetRight(),insets.getSystemWindowInsetBottom());return insets;});
        web.loadUrl("file:///android_asset/index.html");readShare(getIntent());
    }
    @Override public void onSaveInstanceState(Bundle state){super.onSaveInstanceState(state);state.putString("importRequest",importRequest);state.putString("importProject",importProject);if(cameraFile!=null)state.putString("cameraFile",cameraFile.getAbsolutePath());}
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);readShare(intent);web.evaluateJavascript("window.refresh?.()",null);}
    private void readShare(Intent intent) {
        if(Intent.ACTION_SEND.equals(intent.getAction())) { Uri uri=intent.getParcelableExtra(Intent.EXTRA_STREAM);if(uri!=null)shared.add(uri); }
        if(Intent.ACTION_SEND_MULTIPLE.equals(intent.getAction())) { ArrayList<Uri> list=intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM);if(list!=null)shared.addAll(list); }
    }
    private void reply(String id,Object value,String error) {
        JSONObject response=new JSONObject();try{response.put("id",id).put("value",value==null?JSONObject.NULL:value).put("error",error==null?JSONObject.NULL:error);}catch(JSONException ignored){}
        runOnUiThread(()->{if(!isDestroyed())web.evaluateJavascript("window.receiveNative("+response+")",null);});
    }
    public final class Bridge {
        @JavascriptInterface public void invoke(String request) { worker.execute(()->{
            String id="";try {JSONObject req=new JSONObject(request);id=req.getString("id");String method=req.getString("method");JSONObject args=req.optJSONObject("args");if(args==null)args=new JSONObject();Object result;
                switch(method) {
                    case "state": result=ledger.state(args.optString("project_id")).put("shared_count",shared.size()).put("settings",workbench.settings()).put("services",services.status()).put("custom_template",reports.template()!=null);break;
                    case "project": result=ledger.project(args.getString("name"));break;
                    case "reimbursement": ledger.reimbursement(args.getString("id"),args.getBoolean("value"));result=true;break;
                    case "role": ledger.materialRole(args.getString("id"),args.getString("role"));result=true;break;
                    case "save": result=ledger.save(args);break;
                    case "verify": result=ledger.verify(args.getString("id"),args.optBoolean("confirm_missing"));break;
                    case "return": ledger.returnReview(args.getString("id"));result=true;break;
                    case "projectEdit":case "person":case "mergePeople":case "materialEdit":case "materialStatus":case "expenseBatch":case "detach":case "attach":case "experience":case "autoMatch":result=workbench.call(method,args);break;
                    case "recognize":{String material=args.getString("id");File file=ledger.materialFile(material);if(!PrintExport.sha(file).equals(ledger.materialDigest(material)))throw new IOException("原件内容已变化");String text=recognition.read(file,file.getName().endsWith(".pdf"));workbench.recognized(material,Rules.parse(text),text);result=true;break;}
                    case "aiExtract":{String material=args.getString("id");JSONObject m=null;for(JSONObject item:ReportModel.list(ledger.materials(args.getString("project_id"),null)))if(item.getString("id").equals(material))m=item;if(m==null)throw new IllegalArgumentException("请选择未关联的材料");File source=ledger.materialFile(material);if(!PrintExport.sha(source).equals(m.getString("digest")))throw new IOException("原件内容已变化");Map<String,String> fields=services.extract(source,m.getString("mime").equals("application/pdf"),m.getString("text"));String transcript=fields.remove("__transcript");workbench.recognized(material,fields,transcript==null?m.getString("text"):m.getString("text")+"\n[GLM图片转录，待核对]\n"+transcript);result=true;break;}
                    case "classify":{String text=args.optString("goods"),purpose=args.optString("purpose");String local=workbench.category(text+"\n"+purpose);result=!local.equals("其他")?new JSONObject().put("category",local).put("reason","明确商品规则或已保存经验").put("confidence",1):args.optBoolean("remote")?services.classify(text,purpose):new JSONObject().put("category","其他").put("reason","未找到明确商品，请手动选择").put("confidence",0);break;}
                    case "configure":services.configure(args);result=services.status();break;
                    case "previewReport":{File file=reports.file(args.getString("id"),false);runOnUiThread(()->preview(file,"最终报销预览"));result=true;break;}
                    case "submitReport":result=reports.submit(args.getString("id"));break;
                    case "template":case "restoreSelect":{
                        if(!importRequest.isEmpty())throw new IllegalArgumentException("请先完成当前文件选择");importRequest=id;int requestCode=method.equals("template")?12:13;runOnUiThread(()->{Intent intent=new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*");startActivityForResult(intent,requestCode);});return;}
                    case "resetTemplate":{File template=reports.template();if(template!=null&&!template.delete())throw new IOException("模板恢复失败");result=true;break;}
                    case "restoreConfirm":{if(restoreFile==null)throw new IllegalArgumentException("请重新选择备份文件");result=backups.restore(restoreFile,args.getString("digest"));restoreFile.delete();restoreFile=null;break;}
                    case "backup":{exportFile=backups.create();exportRequest=id;createDocument("application/zip","发票助手备份.zip");return;}
                    case "camera":{if(!importRequest.isEmpty())throw new IllegalArgumentException("已有材料选择正在进行");importRequest=id;importProject=args.getString("project_id");File directory=new File(getCacheDir(),"camera");directory.mkdirs();cameraFile=new File(directory,Ledger.id()+".jpg");Uri output=FileProvider.getUriForFile(MainActivity.this,getPackageName()+".files",cameraFile);runOnUiThread(()->{try{startActivityForResult(new Intent(android.provider.MediaStore.ACTION_IMAGE_CAPTURE).putExtra(android.provider.MediaStore.EXTRA_OUTPUT,output).addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION|Intent.FLAG_GRANT_READ_URI_PERMISSION),14);}catch(ActivityNotFoundException error){String callback=importRequest;importRequest="";reply(callback,null,"未找到相机应用，请从相册导入");}});return;}
                    case "qr":result=PhoneIntake.qr(ledger.materialFile(args.getString("id")));break;
                    case "claim":{String project=args.getString("project_id");result=PhoneIntake.claim(args.getString("url"),getCacheDir(),(file,name)->importFile(file,name,project));break;}
                    case "mail":{String project=args.getString("project_id");result=PhoneIntake.mail(services,args,getCacheDir(),(file,name)->importFile(file,name,project));break;}
                    case "import":
                        if(!importRequest.isEmpty())throw new IllegalArgumentException("已有材料选择正在进行");importRequest=id;importProject=args.getString("project_id");
                        runOnUiThread(()->{Intent intent=new Intent(Intent.ACTION_OPEN_DOCUMENT);intent.addCategory(Intent.CATEGORY_OPENABLE);intent.setType("*/*");intent.putExtra(Intent.EXTRA_MIME_TYPES,new String[]{"image/*","application/pdf"});intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE,true);startActivityForResult(intent,10);});return;
                    case "receiveShare":
                        JSONArray received=new JSONArray();List<Uri> pending;synchronized(shared){pending=new ArrayList<>(shared);}for(Uri uri:pending)try{received.put(importOne(uri,args.getString("project_id")));shared.remove(uri);}catch(Exception error){received.put(new JSONObject().put("error",message(error)));}result=received;break;
                    case "preview": {File file=ledger.materialFile(args.getString("id"));runOnUiThread(()->preview(file));result=true;break;}
                    case "export":result=reports.create(args);break;
                    case "shareReport": {File file=reports.file(args.getString("id"),args.optBoolean("excel"));runOnUiThread(()->share(file));result=true;break;}
                    case "saveReport":
                        exportFile=reports.file(args.getString("id"),args.optBoolean("excel"));exportRequest=id;createDocument(args.optBoolean("excel")?"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":"application/pdf",args.optBoolean("excel")?"报销表.xlsx":"报销打印包.pdf");return;
                    default: throw new IllegalArgumentException("不支持的操作");
                }reply(id,result,null);
            }catch(Exception error){reply(id,null,message(error));}
        }); }
    }
    private static String message(Exception error){String text=error.getMessage();return text==null?"处理未完成，请重试或手动核对":text.substring(0,Math.min(text.length(),200));}
    private void createDocument(String mime,String name){runOnUiThread(()->{Intent intent=new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType(mime).putExtra(Intent.EXTRA_TITLE,name);startActivityForResult(intent,11);});}
    @Override protected void onActivityResult(int request,int result,Intent data) {
        super.onActivityResult(request,result,data);
        if(request==10){String callback=importRequest,project=importProject;importRequest="";importProject="";
            if(result!=RESULT_OK||data==null){reply(callback,new JSONArray(),null);return;}
            List<Uri> uris=new ArrayList<>();if(data.getClipData()!=null){for(int i=0;i<data.getClipData().getItemCount();i++)uris.add(data.getClipData().getItemAt(i).getUri());}else if(data.getData()!=null)uris.add(data.getData());
            worker.execute(()->{JSONArray results=new JSONArray();for(Uri uri:uris)try{results.put(importOne(uri,project));}catch(Exception error){try{results.put(new JSONObject().put("error",message(error)));}catch(JSONException ignored){}}reply(callback,results,null);});
        } else if(request==12||request==13){String callback=importRequest;importRequest="";if(result!=RESULT_OK||data==null||data.getData()==null){reply(callback,false,null);return;}Uri uri=data.getData();worker.execute(()->{File temp=null;try{temp=File.createTempFile("selected-",".part",getCacheDir());try(InputStream in=getContentResolver().openInputStream(uri);OutputStream out=new FileOutputStream(temp)){if(in==null)throw new IOException("文件不可读取");byte[] block=new byte[65536];int n;long size=0;while((n=in.read(block))!=-1){size+=n;if(size>(request==12?5L:500L)*1024*1024)throw new IOException("文件过大");out.write(block,0,n);}}if(request==12){new ReportWorkbook(temp);File target=new File(getFilesDir(),"report-template.xlsx");Files.move(temp.toPath(),target.toPath(),java.nio.file.StandardCopyOption.REPLACE_EXISTING);reply(callback,true,null);}else{JSONObject summary=backups.inspect(temp);if(restoreFile!=null)restoreFile.delete();restoreFile=temp;temp=null;reply(callback,summary,null);}}catch(Exception error){reply(callback,null,message(error));}finally{if(temp!=null)temp.delete();}});
        } else if(request==14){String callback=importRequest,project=importProject;importRequest="";importProject="";File file=cameraFile;cameraFile=null;if(result!=RESULT_OK||file==null||!file.isFile()){if(file!=null)file.delete();reply(callback,false,null);return;}worker.execute(()->{try{reply(callback,importFile(file,"拍摄票据.jpg",project),null);}catch(Exception error){reply(callback,null,message(error));}finally{file.delete();}});
        } else if(request==11){String callback=exportRequest;exportRequest="";File file=exportFile;exportFile=null;
            if(result!=RESULT_OK||data==null||data.getData()==null){if(file!=null&&file.getName().startsWith("backup-")&&file.getName().endsWith(".zip"))file.delete();reply(callback,false,null);return;}Uri uri=data.getData();worker.execute(()->{try(InputStream in=new FileInputStream(file);OutputStream out=getContentResolver().openOutputStream(uri)){if(out==null)throw new IOException("目标文件无法写入");byte[] block=new byte[65536];int n;while((n=in.read(block))!=-1)out.write(block,0,n);reply(callback,true,null);}catch(Exception error){reply(callback,null,message(error));}finally{if(file!=null&&file.getName().startsWith("backup-")&&file.getName().endsWith(".zip"))file.delete();}});
        }
    }
    private JSONObject importOne(Uri uri,String project) throws Exception {
        boolean known=false;JSONArray projects=ledger.state(project).getJSONArray("projects");for(int i=0;i<projects.length();i++)known|=projects.getJSONObject(i).getString("id").equals(project)&&!projects.getJSONObject(i).getBoolean("deleted");if(!known)throw new IllegalArgumentException("请先选择当前项目");
        String name="材料";if("file".equals(uri.getScheme()))name=new File(uri.getPath()).getName();else try(android.database.Cursor cursor=getContentResolver().query(uri,new String[]{OpenableColumns.DISPLAY_NAME},null,null,null)){if(cursor!=null&&cursor.moveToFirst())name=cursor.getString(0);}
        File folder=new File(getFilesDir(),"originals");folder.mkdirs();String id=Ledger.id();File temp=new File(folder,id+".part");boolean saved=false;
        try {
            try(InputStream in=getContentResolver().openInputStream(uri);OutputStream out=new FileOutputStream(temp)) { if(in==null)throw new IOException("材料不可读取");byte[] buffer=new byte[65536];int count;long bytes=0;while((count=in.read(buffer))!=-1){bytes+=count;if(bytes>20L*1024*1024)throw new IOException("单份材料不能超过20MB");out.write(buffer,0,count);}if(bytes==0)throw new IOException("材料为空"); }
            byte[] magic=new byte[5];try(InputStream in=new FileInputStream(temp)){in.read(magic);}boolean pdf=new String(magic,java.nio.charset.StandardCharsets.US_ASCII).equals("%PDF-");
            if(!pdf){BitmapFactory.Options options=new BitmapFactory.Options();options.inJustDecodeBounds=true;BitmapFactory.decodeFile(temp.getAbsolutePath(),options);if(options.outWidth<=0||options.outHeight<=0||((long)options.outWidth*options.outHeight)>40_000_000)throw new IOException("不支持此文件或图片过大，请使用图片/PDF");}
            String digest=PrintExport.sha(temp);String text="",warning="";try{text=recognition.read(temp,pdf);}catch(Exception error){warning=message(error);}
            Map<String,String> fields=Rules.parse(text);if(services.enabled("glm")&&(!fields.containsKey("amount")||!fields.containsKey("merchant")||fields.get("role").equals("invoice")&&!fields.containsKey("invoice_number"))){try{Map<String,String> supplement=services.extract(temp,pdf,text);String transcript=supplement.remove("__transcript");if(transcript!=null)text+="\n[GLM图片转录，待核对]\n"+transcript;for(Map.Entry<String,String> entry:supplement.entrySet()){String local=fields.get(entry.getKey());if(local!=null&&!local.isEmpty()&&!local.equals(entry.getValue()))warning="GLM与本地识别字段不同，请对照原件手动确认";else if(entry.getKey().equals("role")&&"other".equals(local))fields.put("role",entry.getValue());else fields.putIfAbsent(entry.getKey(),entry.getValue());}}catch(Exception error){warning="GLM补充未完成，原件保留，可继续手工处理";}}if(!warning.isEmpty())fields.put("warning",warning);File original=new File(folder,id+(pdf?".pdf":".image"));if(!temp.renameTo(original))throw new IOException("原件保存失败");temp=original;
            if(!ledger.addMaterial(id,project,digest,name,original,pdf?"application/pdf":"image/*",fields,text))return new JSONObject().put("duplicate",true).put("name",name);
            saved=true;return new JSONObject().put("id",id).put("name",name).put("warning",warning);
        }finally{if(!saved)temp.delete();}
    }
    private JSONObject importFile(File file,String name,String project)throws Exception{
        // Same validation / immutable original handling as SAF. DISPLAY_NAME fallback uses filename.
        JSONObject result=importOne(Uri.fromFile(file),project);if(result.has("id"))ledger.getWritableDatabase().execSQL("UPDATE materials SET name=? WHERE id=?",new Object[]{name,result.getString("id")});return result;
    }
    private void share(File file) {
        Uri uri=FileProvider.getUriForFile(this,getPackageName()+".files",file);Intent send=new Intent(Intent.ACTION_SEND).setType(file.getName().endsWith(".xlsx")?"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":"application/pdf").putExtra(Intent.EXTRA_STREAM,uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);startActivity(Intent.createChooser(send,"分享报销文件"));
    }
    @Override public void onBackPressed() {
        web.evaluateJavascript("window.handleBack?.() || false",value->{if(!"true".equals(value))super.onBackPressed();});
    }
    private void preview(File file){preview(file,"材料原件");}
    private void preview(File file,String title) {
        LinearLayout layout=new LinearLayout(this);layout.setOrientation(LinearLayout.VERTICAL);ImageView image=new ImageView(this);image.setAdjustViewBounds(true);image.setContentDescription("材料原件预览");ScrollView scroll=new ScrollView(this);scroll.addView(image);layout.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle(title).setView(layout).setPositiveButton("关闭",null).create();dialog.show();dialog.getWindow().setLayout(-1,(int)(getResources().getDisplayMetrics().heightPixels*.85));
        if(file.getName().endsWith(".pdf")){try{ParcelFileDescriptor fd=ParcelFileDescriptor.open(file,ParcelFileDescriptor.MODE_READ_ONLY);PdfRenderer renderer=new PdfRenderer(fd);int[] page={0};Bitmap[] previous={null};TextView label=new TextView(this);LinearLayout buttons=new LinearLayout(this);Button before=new Button(this),next=new Button(this);before.setText("上一页");next.setText("下一页");buttons.addView(before);buttons.addView(label);buttons.addView(next);layout.addView(buttons);
            Runnable show=()->{try(PdfRenderer.Page p=renderer.openPage(page[0])){float scale=Math.min(2f,1200f/p.getWidth());Bitmap bitmap=Bitmap.createBitmap(Math.max(1,(int)(p.getWidth()*scale)),Math.max(1,(int)(p.getHeight()*scale)),Bitmap.Config.ARGB_8888);bitmap.eraseColor(Color.WHITE);p.render(bitmap,null,null,PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY);image.setImageBitmap(bitmap);if(previous[0]!=null)previous[0].recycle();previous[0]=bitmap;label.setText((page[0]+1)+" / "+renderer.getPageCount());before.setEnabled(page[0]>0);next.setEnabled(page[0]<renderer.getPageCount()-1);}};
            before.setOnClickListener(v->{page[0]--;show.run();});next.setOnClickListener(v->{page[0]++;show.run();});dialog.setOnDismissListener(v->{renderer.close();try{fd.close();}catch(IOException ignored){}if(previous[0]!=null)previous[0].recycle();});show.run();
        }catch(Exception error){dialog.dismiss();Toast.makeText(this,message(error),Toast.LENGTH_LONG).show();}}
        else {try{Bitmap bitmap=ImageFiles.read(file,2000);image.setImageBitmap(bitmap);dialog.setOnDismissListener(v->bitmap.recycle());}catch(IOException error){dialog.dismiss();Toast.makeText(this,message(error),Toast.LENGTH_LONG).show();}}
    }
    @Override protected void onDestroy(){worker.execute(()->{recognition.close();ledger.close();});worker.shutdown();super.onDestroy();}
}
