package cn.qiuc.invoice;

import android.content.*;
import android.database.Cursor;
import android.database.sqlite.*;
import org.json.*;
import java.io.File;
import java.util.*;

/** Device-private ledger. Original files are immutable; all writes use transactions. */
public final class Ledger extends SQLiteOpenHelper {
    public Ledger(Context context) { super(context,"invoice-assistant.sqlite3",null,2); }
    @Override public void onConfigure(SQLiteDatabase db) { db.setForeignKeyConstraintsEnabled(true); }
    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,reimbursed INTEGER NOT NULL DEFAULT 0)");
        db.execSQL("CREATE TABLE materials(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),digest TEXT NOT NULL,name TEXT NOT NULL,path TEXT NOT NULL,mime TEXT NOT NULL,role TEXT NOT NULL,fields TEXT NOT NULL,text TEXT NOT NULL,expense_id TEXT,UNIQUE(project_id,digest))");
        db.execSQL("CREATE TABLE expenses(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),data TEXT NOT NULL,state TEXT NOT NULL)");
        db.execSQL("CREATE TABLE reports(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),path TEXT NOT NULL,total INTEGER NOT NULL,count INTEGER NOT NULL,created INTEGER NOT NULL)");
        db.execSQL("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,subject TEXT NOT NULL,created INTEGER NOT NULL)");
        upgrade(db);
    }
    @Override public void onUpgrade(SQLiteDatabase db,int old,int version) { if(old<2)upgrade(db); }
    private void upgrade(SQLiteDatabase db) {
        for(String sql:List.of("ALTER TABLE projects ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0","ALTER TABLE projects ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0","ALTER TABLE expenses ADD COLUMN version INTEGER NOT NULL DEFAULT 1","ALTER TABLE expenses ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0","ALTER TABLE materials ADD COLUMN status TEXT NOT NULL DEFAULT 'active'","ALTER TABLE materials ADD COLUMN version INTEGER NOT NULL DEFAULT 1","ALTER TABLE reports ADD COLUMN status TEXT NOT NULL DEFAULT 'legacy'","ALTER TABLE reports ADD COLUMN manifest TEXT NOT NULL DEFAULT '{}'","ALTER TABLE reports ADD COLUMN xlsx_path TEXT NOT NULL DEFAULT ''","ALTER TABLE reports ADD COLUMN digest TEXT NOT NULL DEFAULT ''"))db.execSQL(sql);
        db.execSQL("CREATE TABLE people(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),name TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1)");
        db.execSQL("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)");
        db.execSQL("CREATE TABLE experience(key TEXT PRIMARY KEY,value TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1)");
        // Existing mobile v1 records retain their money, manual category and state.
        try(Cursor c=db.rawQuery("SELECT id,project_id,data FROM expenses",null)){while(c.moveToNext())try{JSONObject data=new JSONObject(c.getString(2));String name=data.optString("owner");if(!name.isBlank())data.put("owner_id",personForName(db,c.getString(1),name));db.execSQL("UPDATE expenses SET data=? WHERE id=?",new Object[]{data.toString(),c.getString(0)});}catch(JSONException e){throw new IllegalStateException("旧账本记录无效，升级已停止，保留原数据",e);}}
    }
    String personForName(SQLiteDatabase db,String project,String name) {
        try(Cursor c=db.rawQuery("SELECT id FROM people WHERE project_id=? AND name=? AND active=1",new String[]{project,name})){if(c.moveToFirst())return c.getString(0);}
        try(Cursor c=db.rawQuery("SELECT id FROM people WHERE project_id=? AND name=? AND active=0",new String[]{project,name})){if(c.moveToFirst())throw new IllegalArgumentException("人员已停用，请先在人员管理中恢复，避免创建重复身份");}
        String id=id();db.execSQL("INSERT INTO people(id,project_id,name) VALUES(?,?,?)",new Object[]{id,project,name});return id;
    }
    static String id() { return UUID.randomUUID().toString().replace("-",""); }
    void event(SQLiteDatabase db,String kind,String subject) { db.execSQL("INSERT INTO events(kind,subject,created) VALUES(?,?,?)",new Object[]{kind,subject,System.currentTimeMillis()}); }
    public synchronized JSONObject project(String name) throws JSONException {
        name=name.trim(); if(name.isEmpty() || name.length()>120) throw new IllegalArgumentException("请填写项目名称（最多120字）");
        String id=id(); SQLiteDatabase db=getWritableDatabase();
        db.execSQL("INSERT INTO projects(id,name) VALUES(?,?)",new Object[]{id,name}); event(db,"project_created",id);
        return new JSONObject().put("id",id).put("name",name);
    }
    public synchronized void reimbursement(String id,boolean value) {
        ContentValues v=new ContentValues();v.put("reimbursed",value?1:0);
        if(getWritableDatabase().update("projects",v,"id=?",new String[]{id})!=1) throw new IllegalArgumentException("项目不存在");
        event(getWritableDatabase(),"project_reimbursement_manual",id);
    }
    public synchronized JSONObject state(String project) throws JSONException {
        JSONObject out=new JSONObject(); JSONArray projects=new JSONArray();SQLiteDatabase db=getReadableDatabase();
        try(Cursor c=db.rawQuery("SELECT id,name,reimbursed,pinned,deleted FROM projects ORDER BY pinned DESC,rowid DESC",null)) { while(c.moveToNext()) projects.put(new JSONObject().put("id",c.getString(0)).put("name",c.getString(1)).put("reimbursed",c.getInt(2)==1).put("pinned",c.getInt(3)==1).put("deleted",c.getInt(4)==1)); }
        out.put("projects",projects); out.put("materials",materials(project,null));
        JSONArray expenses=new JSONArray();
        try(Cursor c=db.rawQuery("SELECT id,data,state,version,deleted FROM expenses WHERE project_id=? ORDER BY rowid DESC",new String[]{project})) { while(c.moveToNext()) { JSONObject e=new JSONObject(c.getString(1));e.put("id",c.getString(0)).put("state",c.getString(2)).put("version",c.getInt(3)).put("deleted",c.getInt(4)==1);JSONArray proof=materials(project,c.getString(0));e.put("materials",proof);Set<String> roles=new HashSet<>();for(int i=0;i<proof.length();i++)roles.add(proof.getJSONObject(i).getString("role"));e.put("missing",new JSONArray(Rules.missing(e.optString("category"),roles,e.optString("basis"))));expenses.put(e); } }
        out.put("expenses",expenses);JSONArray reports=new JSONArray();
        try(Cursor c=db.rawQuery("SELECT id,total,count,created,status,xlsx_path,manifest FROM reports WHERE project_id=? ORDER BY created DESC",new String[]{project})) { while(c.moveToNext()) reports.put(new JSONObject().put("id",c.getString(0)).put("total",c.getLong(1)).put("count",c.getInt(2)).put("created",c.getLong(3)).put("status",c.getString(4)).put("has_xlsx",!c.getString(5).isEmpty()).put("manifest",new JSONObject(c.getString(6)))); }
        JSONArray people=new JSONArray();try(Cursor c=db.rawQuery("SELECT id,name,active FROM people WHERE project_id=? ORDER BY rowid",new String[]{project})){while(c.moveToNext())people.put(new JSONObject().put("id",c.getString(0)).put("name",c.getString(1)).put("active",c.getInt(2)==1));}
        JSONArray archived=new JSONArray();try(Cursor c=db.rawQuery("SELECT id,name,status FROM materials WHERE project_id=? AND status!='active' AND expense_id IS NULL",new String[]{project})){while(c.moveToNext())archived.put(new JSONObject().put("id",c.getString(0)).put("name",c.getString(1)).put("status",c.getString(2)));}
        return out.put("reports",reports).put("people",people).put("material_archive",archived).put("report_types",ReportModel.types());
    }
    public synchronized JSONArray materials(String project,String expense) throws JSONException {
        JSONArray out=new JSONArray();String clause=expense==null?"project_id=? AND expense_id IS NULL AND status='active'":"project_id=? AND expense_id=?";
        String[] args=expense==null?new String[]{project}:new String[]{project,expense};
        try(Cursor c=getReadableDatabase().rawQuery("SELECT id,name,digest,mime,role,fields,text,version FROM materials WHERE "+clause+" ORDER BY rowid",args)) {
            while(c.moveToNext()) out.put(new JSONObject().put("id",c.getString(0)).put("name",c.getString(1)).put("digest",c.getString(2)).put("mime",c.getString(3)).put("role",c.getString(4)).put("fields",new JSONObject(c.getString(5))).put("text",c.getString(6)).put("version",c.getInt(7)));
        }return out;
    }
    public synchronized boolean addMaterial(String id,String project,String digest,String name,File file,String mime,Map<String,String> fields,String text) {
        SQLiteDatabase db=getWritableDatabase(); ContentValues v=new ContentValues();
        v.put("id",id);v.put("project_id",project);v.put("digest",digest);v.put("name",name);v.put("path",file.getAbsolutePath());v.put("mime",mime);v.put("role",fields.getOrDefault("role","other"));v.put("fields",new JSONObject(fields).toString());v.put("text",text);
        // Only an existing digest is a duplicate; other constraint failures propagate.
        try(Cursor c=db.rawQuery("SELECT id FROM materials WHERE project_id=? AND digest=?",new String[]{project,digest})) { if(c.moveToFirst())return false; }
        db.beginTransaction();try{db.insertOrThrow("materials",null,v);event(db,"material_received",id);db.setTransactionSuccessful();return true;}finally{db.endTransaction();}
    }
    public synchronized void materialRole(String id,String role) {
        if(!List.of("invoice","receipt","purchase","order","payment","trip","detail","other").contains(role)) throw new IllegalArgumentException("材料用途无效");
        ContentValues v=new ContentValues();v.put("role",role);
        try(Cursor c=getReadableDatabase().rawQuery("SELECT fields FROM materials WHERE id=? AND expense_id IS NULL",new String[]{id})){if(!c.moveToFirst())throw new IllegalArgumentException("材料已关联，请刷新");try{v.put("fields",new JSONObject(c.getString(0)).put("manual_role",true).toString());}catch(JSONException error){throw new IllegalStateException("材料记录不可读取",error);}}
        if(getWritableDatabase().update("materials",v,"id=? AND expense_id IS NULL AND status='active'",new String[]{id})!=1) throw new IllegalArgumentException("材料已关联，请刷新");
        getWritableDatabase().execSQL("UPDATE materials SET version=version+1 WHERE id=?",new Object[]{id});
        event(getWritableDatabase(),"material_role_manual",id);
    }
    public synchronized JSONObject save(JSONObject input) throws JSONException {
        String project=input.getString("project_id");String id=input.optString("id");boolean fresh=id.isEmpty();
        if(fresh)id=id();
        String amount=input.optString("amount").trim();long cents=amount.isEmpty()?0:Rules.cents(amount);
        String category=input.optString("category"); if(!Rules.CATEGORIES.contains(category))throw new IllegalArgumentException("请选择费用类别");
        JSONObject data=new JSONObject().put("title",input.optString("title").trim()).put("merchant",input.optString("merchant").trim()).put("owner",input.optString("owner").trim()).put("date",input.optString("date").trim()).put("category",category).put("cents",cents).put("note",input.optString("note").trim()).put("missing_confirmed",false);
        for(String key:List.of("project_type","project_name","order_number","shopping_platform","shop_name","payment_channel","basis"))if(input.has(key))data.put(key,input.getString(key));
        JSONObject choice=ReportModel.infer(data);data.put("project_type",choice.getString("project_type")).put("project_name",choice.getString("project_name")).put("category",choice.getString("category"));
        for(String key:List.of("title","merchant","owner","date","note")) if(data.getString(key).length()>2000)throw new IllegalArgumentException("输入内容过长");
        SQLiteDatabase db=getWritableDatabase();db.beginTransaction();try {
            if(!data.optString("owner").isEmpty())data.put("owner_id",personForName(db,project,data.getString("owner")));
            if(input.has("allocations")){JSONArray allocations=input.getJSONArray("allocations");long sum=0;Set<String> owners=new HashSet<>();for(int i=0;i<allocations.length();i++){JSONObject allocation=allocations.getJSONObject(i);String person=allocation.getString("person_id");try(Cursor c=db.rawQuery("SELECT name FROM people WHERE id=? AND project_id=? AND active=1",new String[]{person,project})){if(!c.moveToFirst()||!owners.add(person))throw new IllegalArgumentException("分摊人员无效或重复");allocation.put("name",c.getString(0));}long part=allocation.getLong("cents");if(part<=0)throw new IllegalArgumentException("分摊金额必须大于零");sum=Math.addExact(sum,part);}if(allocations.length()>0&&sum!=cents)throw new IllegalArgumentException("分摊金额合计必须等于费用金额");if(allocations.length()>0)data.put("allocations",allocations).put("owner","多人分摊").put("owner_id","");}
            if(fresh) {
                JSONArray ids=input.getJSONArray("material_ids");if(ids.length()==0)throw new IllegalArgumentException("请先选择材料");Set<String> unique=new HashSet<>();
                for(int i=0;i<ids.length();i++)if(!unique.add(ids.getString(i)))throw new IllegalArgumentException("材料重复选择");
                db.execSQL("INSERT INTO expenses(id,project_id,data,state) VALUES(?,?,?,'review')",new Object[]{id,project,data.toString()});
                for(String material:unique) { ContentValues v=new ContentValues();v.put("expense_id",id);if(db.update("materials",v,"id=? AND project_id=? AND expense_id IS NULL AND status='active'",new String[]{material,project})!=1) throw new IllegalArgumentException("材料已被使用或不属于当前项目，请刷新"); }
            } else {
                ContentValues v=new ContentValues();v.put("data",data.toString());
                String where="id=? AND project_id=? AND state='review' AND deleted=0";String[] params={id,project};if(input.has("version")){where+=" AND version=?";params=new String[]{id,project,String.valueOf(input.getInt("version"))};}
                if(db.update("expenses",v,where,params)!=1)throw new IllegalArgumentException("费用已变化或不在待核对，请刷新");
                db.execSQL("UPDATE expenses SET version=version+1 WHERE id=?",new Object[]{id});
            }
            event(db,"expense_draft_saved",id);db.setTransactionSuccessful();
        } finally { db.endTransaction(); }
        return data.put("id",id);
    }
    public synchronized JSONObject verify(String id,boolean confirmMissing) throws JSONException {
        SQLiteDatabase db=getWritableDatabase();db.beginTransaction();try {
            String project;JSONObject data;
            try(Cursor c=db.rawQuery("SELECT project_id,data FROM expenses WHERE id=? AND state='review' AND deleted=0",new String[]{id})) { if(!c.moveToFirst())throw new IllegalArgumentException("费用不在待核对阶段，请刷新");project=c.getString(0);data=new JSONObject(c.getString(1)); }
            if(data.optLong("cents")<=0)throw new IllegalArgumentException("金额待确认，请先填写实际费用金额");
            for(String field:List.of("title","merchant","owner")) if(data.optString(field).isBlank())throw new IllegalArgumentException("请补齐费用名称、商家和归属人");
            Rules.date(data.optString("date"));
            Set<String> roles=new HashSet<>();JSONArray materials=materials(project,id);for(int i=0;i<materials.length();i++)roles.add(materials.getJSONObject(i).getString("role"));
            if(materials.length()==0)throw new IllegalArgumentException("请至少关联一份原始材料再核对");
            List<String> missing=Rules.missing(data.getString("category"),roles,data.optString("basis"));
            if(!missing.isEmpty() && !confirmMissing) return new JSONObject().put("requires_confirmation",true).put("missing",new JSONArray(missing));
            data.put("missing",new JSONArray(missing)).put("missing_confirmed",!missing.isEmpty());
            db.execSQL("UPDATE expenses SET state='verified',data=?,version=version+1 WHERE id=?",new Object[]{data.toString(),id});event(db,"expense_verified",id);db.setTransactionSuccessful();
            return new JSONObject().put("verified",true);
        } finally { db.endTransaction(); }
    }
    public synchronized void returnReview(String id) {
        ContentValues v=new ContentValues();v.put("state","review");
        if(getWritableDatabase().update("expenses",v,"id=? AND state IN ('verified','submitted') AND deleted=0",new String[]{id})!=1)throw new IllegalArgumentException("仅可退回已核对的费用");
        getWritableDatabase().execSQL("UPDATE expenses SET version=version+1 WHERE id=?",new Object[]{id});
        event(getWritableDatabase(),"expense_returned",id);
    }
    public synchronized File materialFile(String id) {
        try(Cursor c=getReadableDatabase().rawQuery("SELECT path FROM materials WHERE id=?",new String[]{id})) { if(!c.moveToFirst())throw new IllegalArgumentException("材料不存在");return new File(c.getString(0)); }
    }
    public synchronized String materialDigest(String id) {
        try(Cursor c=getReadableDatabase().rawQuery("SELECT digest FROM materials WHERE id=?",new String[]{id})) { if(!c.moveToFirst())throw new IllegalArgumentException("材料不存在");return c.getString(0); }
    }
    public synchronized void report(String project,String id,File file,long total,int count) {
        getWritableDatabase().execSQL("INSERT INTO reports(id,project_id,path,total,count,created) VALUES(?,?,?,?,?,?)",new Object[]{id,project,file.getAbsolutePath(),total,count,System.currentTimeMillis()});event(getWritableDatabase(),"print_package_created",id);
    }
    public synchronized File reportFile(String id) {
        try(Cursor c=getReadableDatabase().rawQuery("SELECT path FROM reports WHERE id=?",new String[]{id})) { if(!c.moveToFirst())throw new IllegalArgumentException("打印包不存在");return new File(c.getString(0)); }
    }
}
