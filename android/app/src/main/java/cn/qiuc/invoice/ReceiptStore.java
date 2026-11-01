package cn.qiuc.invoice;

import android.content.*;
import android.database.Cursor;
import android.database.sqlite.*;
import org.json.*;
import java.io.*;
import java.nio.file.Files;
import java.util.UUID;

/** This database and its photos never participate in the phone's own ledger. */
final class ReceiptStore extends SQLiteOpenHelper {
    private final File photos;
    ReceiptStore(Context context){super(context,"receipt-inbox.sqlite3",null,1);photos=new File(context.getFilesDir(),"receipt-inbox");if(!photos.isDirectory()&&!photos.mkdirs())throw new IllegalStateException("收票目录无法创建");}
    @Override public void onCreate(SQLiteDatabase db){db.execSQL("CREATE TABLE receipts(id TEXT PRIMARY KEY,file TEXT NOT NULL,payload TEXT NOT NULL,created INTEGER NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,next_try INTEGER NOT NULL DEFAULT 0)");}
    @Override public void onUpgrade(SQLiteDatabase db,int old,int next){throw new IllegalStateException("收票箱版本不兼容");}
    String save(File source,JSONObject fields)throws Exception{
        String id=UUID.randomUUID().toString();File file=new File(photos,id+".jpg");boolean saved=false;
        try{Files.copy(source.toPath(),file.toPath());fields.put("client_id",id);ContentValues v=new ContentValues();v.put("id",id);v.put("file",file.getAbsolutePath());v.put("payload",fields.toString());v.put("created",System.currentTimeMillis());v.put("status",fields.optString("project_id").isEmpty()?"reselect":"pending");getWritableDatabase().insertOrThrow("receipts",null,v);saved=true;return id;}finally{if(!saved)file.delete();}
    }
    JSONArray list(boolean recent)throws JSONException{
        JSONArray rows=new JSONArray();String where=recent?"created>=?":"status IN ('pending','failed')";String[] args=recent?new String[]{Long.toString(System.currentTimeMillis()-30L*24*60*60_000)}:null;
        try(Cursor c=getReadableDatabase().query("receipts",null,where,args,null,null,recent?"created DESC":"created ASC")){while(c.moveToNext()){JSONObject row=new JSONObject(c.getString(c.getColumnIndexOrThrow("payload")));for(String key:new String[]{"status","error","file"})row.put(key,c.getString(c.getColumnIndexOrThrow(key)));for(String key:new String[]{"created","attempts","next_try"})row.put(key,c.getLong(c.getColumnIndexOrThrow(key)));rows.put(row);}}return rows;
    }
    JSONObject get(String id)throws JSONException{try(Cursor c=getReadableDatabase().query("receipts",null,"id=?",new String[]{id},null,null,null)){if(!c.moveToFirst())throw new IllegalArgumentException("收票记录不存在");return new JSONObject(c.getString(c.getColumnIndexOrThrow("payload"))).put("file",c.getString(c.getColumnIndexOrThrow("file"))).put("status",c.getString(c.getColumnIndexOrThrow("status")));}}
    void update(String id,JSONObject payload)throws JSONException{JSONObject old=get(id);if(old.getString("status").equals("received"))throw new IllegalArgumentException("票据已到电脑，请在电脑修改");ContentValues v=new ContentValues();v.put("payload",payload.toString());v.put("status",payload.optString("project_id").isEmpty()?"reselect":"pending");v.put("error","");v.put("next_try",0);v.put("attempts",0);getWritableDatabase().update("receipts",v,"id=?",new String[]{id});}
    void status(String id,String status,String error,int attempts,long next){ContentValues v=new ContentValues();v.put("status",status);v.put("error",error);v.put("attempts",attempts);v.put("next_try",next);getWritableDatabase().update("receipts",v,"id=?",new String[]{id});}
    void delete(String id)throws Exception{JSONObject row=get(id);if(row.getString("status").equals("received"))throw new IllegalArgumentException("已到电脑的票据不能在手机删除");getWritableDatabase().delete("receipts","id=?",new String[]{id});File file=new File(row.getString("file"));if(file.exists()&&!file.delete())throw new IOException("记录已删除，照片清理失败");}
}
