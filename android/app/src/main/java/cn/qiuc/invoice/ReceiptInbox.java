package cn.qiuc.invoice;

import android.content.*;
import android.graphics.Bitmap;
import android.util.Base64;
import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.*;

/** The only LAN HTTP client. All entry points share the same lock with the job. */
final class ReceiptInbox implements AutoCloseable {
    static final Object LOCK=new Object();
    static final Object SYNC_LOCK=new Object();
    private static final Set<String> SENDING=new HashSet<>();
    private final ReceiptStore store;private final SharedPreferences prefs;private final OptionalServices secrets;
    ReceiptInbox(Context context){store=new ReceiptStore(context);prefs=context.getSharedPreferences("receipt-inbox",Context.MODE_PRIVATE);secrets=new OptionalServices(context);}
    private JSONArray urls(JSONArray values)throws JSONException{JSONArray out=new JSONArray();Set<String> seen=new LinkedHashSet<>();if(values.length()>8)throw new IllegalArgumentException("电脑地址过多");for(int i=0;i<values.length();i++)seen.add(ReceiptRules.address(values.getString(i)));for(String url:seen)out.put(url);if(out.length()==0)throw new IllegalArgumentException("电脑没有可用地址");return out;}
    private void commit(SharedPreferences.Editor edit)throws IOException{if(!edit.commit())throw new IOException("收票设置保存失败");}
    JSONObject catalog()throws JSONException{return new JSONObject(prefs.getString("catalog","{\"categories\":[\"材料采购\",\"酒店\",\"高铁\",\"外卖\",\"餐饮\",\"打车\",\"顺丰\",\"其他\"],\"projects\":[]}"));}
    static List<String> strings(JSONArray a)throws JSONException{List<String> values=new ArrayList<>();for(int i=0;i<a.length();i++)values.add(a.getString(i));return values;}
    private static ReceiptRules.Project ruleProject(JSONObject p)throws JSONException{List<String> people=new ArrayList<>();JSONArray a=p.getJSONArray("people");for(int i=0;i<a.length();i++)people.add(a.getJSONObject(i).getString("id"));return new ReceiptRules.Project(p.getString("id"),p.optString("start_date"),p.optString("end_date"),p.optString("main_person_id"),people);}
    JSONObject defaults(String captured,String recognized)throws JSONException{
        JSONObject catalog=catalog();List<ReceiptRules.Project> projects=new ArrayList<>();JSONArray a=catalog.getJSONArray("projects");for(int i=0;i<a.length();i++)projects.add(ruleProject(a.getJSONObject(i)));
        String project=ReceiptRules.project(projects,captured.substring(0,10),prefs.getString("last_project",""));JSONObject people=new JSONObject();for(ReceiptRules.Project p:projects)people.put(p.id(),ReceiptRules.person(p,prefs.getString("person."+p.id(),"")));
        return new JSONObject().put("project_id",project).put("category",ReceiptRules.category(recognized,prefs.getString("last_category",""),strings(catalog.getJSONArray("categories")))).put("people_defaults",people).put("catalog",catalog);
    }
    JSONObject state()throws Exception{
        JSONArray rows=store.list(true);for(int i=0;i<rows.length();i++){JSONObject row=rows.getJSONObject(i);row.remove("file");}
        return new JSONObject().put("rows",rows).put("catalog",catalog()).put("paired",prefs.contains("key")).put("needs_pairing",prefs.getBoolean("needs_pairing",false)).put("server_name",prefs.getString("server_name","")).put("last_connected",prefs.getString("last_connected","")).put("connection_error",prefs.getString("connection_error",""));
    }
    JSONObject pair(ReceiptRules.Pairing request,String name)throws Exception{
        Reply reply=request(new JSONArray(request.urls()),"pair",new JSONObject().put("code",request.code()).put("device_name",name.substring(0,Math.min(60,name.length()))),"");
        if(reply.status!=200)throw new IOException(reply.body.optString("error","配对失败"));JSONObject b=reply.body;
        if(!b.getString("device_key").matches("[A-Za-z0-9_-]{43}"))throw new IOException("电脑返回的凭据无效");
        JSONArray addresses=urls(b.getJSONArray("urls"));
        commit(prefs.edit().putString("key",secrets.seal(b.getString("device_key"))).putString("device_id",b.getString("device_id")).putString("urls",addresses.toString()).putString("server_name",b.getString("server_name")).putBoolean("needs_pairing",false).remove("catalog").putString("last_connected",now()).putString("connection_error",""));
        try{if(!refreshCatalog(b.getString("device_key")))throw new IOException("目录认证失败");}
        catch(Exception error){unpair();throw new IOException("配对未完成，请在电脑重新生成配对码",error);}
        uploadPending(false,b.getString("device_key"));return state();
    }
    void unpair()throws IOException{commit(prefs.edit().remove("key").remove("device_id").remove("urls").remove("catalog").remove("server_name").remove("last_connected").remove("connection_error").remove("needs_pairing"));}
    static String now(){return LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss"));}
    private JSONObject fields(JSONObject input)throws Exception{
        String amount=input.optString("amount").trim();Object cents=amount.isEmpty()?JSONObject.NULL:Rules.cents(amount);
        String category=input.getString("category"),project=input.optString("project_id"),person=input.optString("person_id"),note=input.optString("note");
        if(!strings(catalog().getJSONArray("categories")).contains(category))throw new IllegalArgumentException("请重新选择类别");
        if(note.codePointCount(0,note.length())>200)throw new IllegalArgumentException("备注最多200字");
        String projectName="";
        if(!project.isEmpty()){
            JSONObject selected=null;JSONArray projects=catalog().getJSONArray("projects");for(int i=0;i<projects.length();i++)if(projects.getJSONObject(i).getString("id").equals(project))selected=projects.getJSONObject(i);
            if(selected==null)throw new IllegalArgumentException("项目已变化，请重新选择");projectName=selected.getString("name");if(!person.isEmpty()&&!ruleProject(selected).people().contains(person))throw new IllegalArgumentException("所属人已变化，请重新选择");
        }else if(!person.isEmpty())throw new IllegalArgumentException("请先选择项目");
        return new JSONObject().put("project_name",projectName).put("project_id",project).put("person_id",person).put("category",category).put("amount_cents",cents).put("note",note);
    }
    private void remember(JSONObject fields)throws IOException{SharedPreferences.Editor edit=prefs.edit().putString("last_category",fields.optString("category"));String project=fields.optString("project_id");if(!project.isEmpty())edit.putString("last_project",project).putString("person."+project,fields.optString("person_id"));commit(edit);}
    String save(File original,String captured,JSONObject input)throws Exception{
        LocalDateTime.parse(captured);JSONObject payload=fields(input).put("captured_at",captured).put("name","IMG_"+captured.replaceAll("[-:T]","")+".jpg");remember(payload);return store.save(original,payload);
    }
    void edit(String id,JSONObject input)throws Exception{
        if(SENDING.contains(id))throw new IllegalArgumentException("正在上传，请稍后再修改");
        JSONObject old=store.get(id),payload=fields(input);for(String key:List.of("client_id","captured_at","name"))payload.put(key,old.get(key));remember(payload);store.update(id,payload);
    }
    void delete(String id)throws Exception{if(SENDING.contains(id))throw new IllegalArgumentException("正在上传，请稍后再删除");store.delete(id);}
    String thumbnail(String id)throws Exception{return thumbnail(new File(store.get(id).getString("file")));}
    static String thumbnail(File file)throws Exception{Bitmap image=ImageFiles.read(file,320);try(ByteArrayOutputStream bytes=new ByteArrayOutputStream()){image.compress(Bitmap.CompressFormat.JPEG,78,bytes);return "data:image/jpeg;base64,"+Base64.encodeToString(bytes.toByteArray(),Base64.NO_WRAP);}finally{image.recycle();}}
    private byte[] image(File source)throws IOException{
        if(source.length()<=20L*1024*1024)return Files.readAllBytes(source.toPath());
        int maximum=4096;while(maximum>=1000){Bitmap b=ImageFiles.read(source,maximum);try(ByteArrayOutputStream out=new ByteArrayOutputStream()){b.compress(Bitmap.CompressFormat.JPEG,95,out);if(out.size()<=20*1024*1024)return out.toByteArray();}finally{b.recycle();}maximum/=2;}throw new IOException("图片缩小后仍超过20MB");
    }
    void sync(boolean force)throws Exception{
        if(Thread.currentThread().isInterrupted())return;
        if(!prefs.contains("key")||prefs.getBoolean("needs_pairing",false))return;
        String key=secrets.unseal(prefs.getString("key",""));
        try{
            if(!refreshCatalog(key))return;
        }catch(IOException error){commit(prefs.edit().putString("connection_error",safe(error)));}
        uploadPending(force,key);
    }
    private boolean refreshCatalog(String key)throws Exception{
        Reply catalog=request(new JSONArray(prefs.getString("urls","[]")),"catalog",null,key);
        if(catalog.status==401){unauthorized();return false;}
        if(catalog.status!=200)throw new IOException(catalog.body.optString("error","目录刷新失败"));
        JSONObject data=catalog.body;data.getJSONArray("projects");data.getJSONArray("categories");
        commit(prefs.edit().putString("catalog",data.toString()).putString("urls",urls(data.getJSONArray("urls")).toString()).putString("last_connected",now()).putString("connection_error",""));
        return true;
    }
    private void uploadPending(boolean force,String key)throws Exception{
        JSONArray rows=store.list(false);
        for(int i=0;i<rows.length();i++){
            if(Thread.currentThread().isInterrupted())return;
            JSONObject row=rows.getJSONObject(i);if(row.optString("project_id").isEmpty()||!force&&row.getLong("next_try")>System.currentTimeMillis())continue;
            String id=row.getString("client_id");int attempts=row.getInt("attempts")+1;
            synchronized(LOCK){try{JSONObject current=store.get(id);if(!Set.of("pending","failed").contains(current.getString("status")))continue;row=current;SENDING.add(id);}catch(IllegalArgumentException deleted){continue;}}
            try{
                JSONObject payload=new JSONObject(row.toString());for(String field:List.of("file","status","error","created","attempts","next_try","project_name"))payload.remove(field);
                payload.put("data",Base64.encodeToString(image(new File(row.getString("file"))),Base64.NO_WRAP));
                Reply reply=request(new JSONArray(prefs.getString("urls","[]")),"receipts",payload,key);String status=ReceiptRules.outcome(reply.status);
                if(status.equals("unpaired")){unauthorized();store.status(id,"pending","请重新配对电脑",attempts,0);return;}
                if(status.equals("received")&&(!Set.of("received","duplicate").contains(reply.body.optString("status"))||reply.body.optString("entry_id").isEmpty()))throw new IOException("电脑回执不完整，将按原编号重试");
                String error=status.equals("received")?"":reply.body.optString("error","电脑暂未接收");store.status(id,status,error,attempts,System.currentTimeMillis()+ReceiptRules.delay(attempts));
                commit(prefs.edit().putString("last_connected",now()));
            }catch(Exception error){store.status(id,"pending",safe(error),attempts,System.currentTimeMillis()+ReceiptRules.delay(attempts));}
            finally{synchronized(LOCK){SENDING.remove(id);}}
        }
    }
    private void unauthorized()throws IOException{commit(prefs.edit().putBoolean("needs_pairing",true).putString("connection_error","配对已失效，请重新配对电脑"));}
    static String safe(Exception e){String message=e.getMessage();return message==null?"连接未完成，请重试":message.substring(0,Math.min(message.length(),160));}
    private record Reply(int status,JSONObject body){}
    static HttpURLConnection connection(String address,String route)throws IOException{
        HttpURLConnection c=(HttpURLConnection)new URL(address+"/device/v1/"+route).openConnection(Proxy.NO_PROXY);
        c.setInstanceFollowRedirects(false);c.setConnectTimeout(5000);c.setReadTimeout(route.equals("receipts")?60000:15000);
        return c;
    }
    private Reply request(JSONArray addresses,String route,JSONObject body,String key)throws Exception{
        IOException last=new IOException("连不上电脑，票据已留在手机");
        for(String address:strings(urls(addresses))){
            HttpURLConnection c=connection(address,route);
            c.setRequestMethod(body==null?"GET":"POST");c.setRequestProperty("Content-Type","application/json; charset=utf-8");if(!key.isEmpty())c.setRequestProperty("Authorization","Bearer "+key);
            try{
                if(body!=null){byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8);if(bytes.length>29L*1024*1024)throw new IOException("图片请求超过29MB");c.setDoOutput(true);try(OutputStream out=c.getOutputStream()){out.write(bytes);}}
                int status=c.getResponseCode();InputStream stream=status>=400?c.getErrorStream():c.getInputStream();JSONObject response=new JSONObject();if(stream!=null)try(InputStream in=stream;ByteArrayOutputStream out=new ByteArrayOutputStream()){byte[] block=new byte[8192];int count;while((count=in.read(block))!=-1){if(out.size()+count>1024*1024)throw new IOException("电脑响应过大");out.write(block,0,count);}if(out.size()>0)response=new JSONObject(out.toString(StandardCharsets.UTF_8.name()));}
                return new Reply(status,response);
            }catch(IOException e){last=new IOException("暂时连不上电脑，请确认电脑收票连接已开启",e);}catch(JSONException e){last=new IOException("电脑响应格式无效");}finally{c.disconnect();}
        }throw last;
    }
    @Override public void close(){store.close();}
}
