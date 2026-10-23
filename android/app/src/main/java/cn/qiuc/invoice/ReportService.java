package cn.qiuc.invoice;

import android.content.*;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.graphics.*;
import com.tom_roush.pdfbox.pdmodel.*;
import com.tom_roush.pdfbox.pdmodel.common.PDRectangle;
import com.tom_roush.pdfbox.pdmodel.graphics.image.*;
import com.tom_roush.pdfbox.multipdf.PDFMergerUtility;
import org.json.*;
import java.io.*;
import java.util.*;

/** Immutable previews and transactional submission. Every output pins the same snapshot. */
public final class ReportService {
    private final Context context;private final Ledger ledger;
    public ReportService(Context context,Ledger ledger){this.context=context;this.ledger=ledger;}
    File template(){File f=new File(context.getFilesDir(),"report-template.xlsx");return f.exists()?f:null;}
    public JSONObject create(JSONObject args) throws Exception{
        String project=args.getString("project_id");JSONObject state=ledger.state(project);String projectName="";for(JSONObject p:ReportModel.list(state.getJSONArray("projects")))if(p.getString("id").equals(project)&&!p.getBoolean("deleted"))projectName=p.getString("name");if(projectName.isEmpty())throw new IllegalArgumentException("项目不存在或已移入回收站");
        Set<String> requested=new HashSet<>();JSONArray ids=args.optJSONArray("ids");if(ids!=null)for(int i=0;i<ids.length();i++)requested.add(ids.getString(i));List<JSONObject> selected=new ArrayList<>();
        for(JSONObject e:ReportModel.list(state.getJSONArray("expenses")))if(e.getString("state").equals("verified")&&!e.getBoolean("deleted")&&(ids==null||requested.remove(e.getString("id"))))selected.add(e);
        if(!requested.isEmpty())throw new IllegalArgumentException("选中费用已变化，请刷新");if(selected.isEmpty())throw new IllegalArgumentException("没有已核对的待提交费用");
        selected.sort(Comparator.comparing((JSONObject e)->e.optString("date")).thenComparing(e->e.optString("id")));
        JSONObject snapshot=ReportModel.snapshot(selected);snapshot.put("project_id",project).put("project",projectName).put("template_digest",template()==null?"builtin-v1":PrintExport.sha(template()));
        String id=Ledger.id();File folder=new File(context.getFilesDir(),"exports");folder.mkdirs();File pdf=new File(folder,id+".pdf"),xlsx=new File(folder,id+".xlsx");boolean saved=false;
        try(PDDocument proofs=new PDDocument();PDDocument document=new PDDocument()){
            JSONArray items=snapshot.getJSONArray("items");int summaryPages=1+(items.length()+13)/14;
            Map<String,int[]> originals=new HashMap<>();for(JSONObject expense:ReportModel.list(items)){String expenseId=expense.getString("id");if(!originals.containsKey(expenseId)){int first=proofs.getNumberOfPages();appendProofs(proofs,expense);originals.put(expenseId,new int[]{first,proofs.getNumberOfPages()});}int[] range=originals.get(expenseId);expense.put("source_first",range[0]).put("source_end",range[1]);}
            ReportWorkbook workbook=new ReportWorkbook(template());try(PDDocument packed=VectorPacking.pack(proofs,items,summaryPages)){workbook.fill(snapshot,projectName);workbook.pdf(context,document,snapshot,projectName);new PDFMergerUtility().appendDocument(document,packed);}workbook.save(xlsx);document.save(pdf);
            snapshot.put("pdf_digest",PrintExport.sha(pdf)).put("xlsx_digest",PrintExport.sha(xlsx));check(snapshot);
            synchronized(ledger){SQLiteDatabase db=ledger.getWritableDatabase();db.execSQL("INSERT INTO reports(id,project_id,path,total,count,created,status,manifest,xlsx_path,digest) VALUES(?,?,?,?,?,?,'preview',?,?,?)",new Object[]{id,project,pdf.getAbsolutePath(),snapshot.getLong("total_cents"),selected.size(),System.currentTimeMillis(),snapshot.toString(),xlsx.getAbsolutePath(),snapshot.getString("pdf_digest")});ledger.event(db,"report_preview_created",id);}saved=true;
            return new JSONObject().put("id",id).put("total",snapshot.getLong("total_cents")).put("count",selected.size());
        }finally{if(!saved){pdf.delete();xlsx.delete();}}
    }
    private void appendProofs(PDDocument document,JSONObject expense) throws Exception{
        boolean fourUp=expense.optString("basis").equals("payment_only");PDRectangle paper=fourUp?new PDRectangle((PDRectangle.A4.getWidth()-8)/2,(PDRectangle.A4.getHeight()-8)/2):PDRectangle.A4;
        JSONArray materials=expense.getJSONArray("materials");boolean invoice=false;for(JSONObject m:ReportModel.list(materials))invoice|=m.getString("role").equals("invoice");
        int before=document.getNumberOfPages();for(JSONObject m:ReportModel.list(materials)){
            if(invoice&&Set.of("高铁","打车","顺丰").contains(expense.getString("category"))&&m.getString("role").equals("payment"))continue;
            File file=ledger.materialFile(m.getString("id"));if(!PrintExport.sha(file).equals(m.getString("digest")))throw new IOException("原件内容已变化，请重新核对");
            if(m.getString("mime").equals("application/pdf")){try(PDDocument source=PDDocument.load(file)){if(source.isEncrypted()||source.getNumberOfPages()==0)throw new IOException("加密或空白PDF不能合成");if(!fourUp)new PDFMergerUtility().appendDocument(document,source);else for(int pageIndex=0;pageIndex<source.getNumberOfPages();pageIndex++){com.tom_roush.pdfbox.pdmodel.graphics.form.PDFormXObject form=new com.tom_roush.pdfbox.multipdf.LayerUtility(document).importPageAsForm(source,pageIndex);PDRectangle box=form.getBBox();com.tom_roush.pdfbox.util.Matrix matrix=form.getMatrix();float minX=Float.POSITIVE_INFINITY,minY=Float.POSITIVE_INFINITY,maxX=Float.NEGATIVE_INFINITY,maxY=Float.NEGATIVE_INFINITY;for(float x:new float[]{box.getLowerLeftX(),box.getUpperRightX()})for(float y:new float[]{box.getLowerLeftY(),box.getUpperRightY()}){float tx=x*matrix.getScaleX()+y*matrix.getShearX()+matrix.getTranslateX(),ty=x*matrix.getShearY()+y*matrix.getScaleY()+matrix.getTranslateY();minX=Math.min(minX,tx);minY=Math.min(minY,ty);maxX=Math.max(maxX,tx);maxY=Math.max(maxY,ty);}float scale=Math.min((paper.getWidth()-12)/(maxX-minX),(paper.getHeight()-12)/(maxY-minY));if(!Float.isFinite(scale)||scale<=0)throw new IOException("付款PDF尺寸无效");PDPage page=new PDPage(paper);document.addPage(page);try(PDPageContentStream stream=new PDPageContentStream(document,page)){stream.transform(new com.tom_roush.pdfbox.util.Matrix(scale,0,0,scale,(paper.getWidth()-(maxX-minX)*scale)/2-minX*scale,(paper.getHeight()-(maxY-minY)*scale)/2-minY*scale));stream.drawForm(form);}}}}
            else{Bitmap bitmap=ImageFiles.read(file,3508);try{PDPage page=new PDPage(paper);document.addPage(page);PDImageXObject image=LosslessFactory.createFromImage(document,bitmap);float margin=fourUp?12:36;float scale=Math.min((paper.getWidth()-margin)/bitmap.getWidth(),(paper.getHeight()-margin)/bitmap.getHeight()),w=bitmap.getWidth()*scale,h=bitmap.getHeight()*scale;try(PDPageContentStream stream=new PDPageContentStream(document,page)){stream.drawImage(image,(paper.getWidth()-w)/2,(paper.getHeight()-h)/2,w,h);}}finally{bitmap.recycle();}}
        }if(document.getNumberOfPages()==before)throw new IOException("本笔费用没有可打印材料");
    }
    private void check(JSONObject snapshot) throws Exception{
        boolean projectCurrent=false;for(JSONObject p:ReportModel.list(ledger.state(snapshot.getString("project_id")).getJSONArray("projects")))if(p.getString("id").equals(snapshot.getString("project_id"))&&!p.getBoolean("deleted")&&p.getString("name").equals(snapshot.getString("project")))projectCurrent=true;if(!projectCurrent)throw new IllegalArgumentException("项目名称或状态已变化，请重新生成预览");
        Map<String,JSONObject> current=new HashMap<>();for(JSONObject e:ReportModel.list(ledger.state(snapshot.getString("project_id")).getJSONArray("expenses")))current.put(e.getString("id"),e);
        if(!snapshot.getString("template_digest").equals(template()==null?"builtin-v1":PrintExport.sha(template())))throw new IllegalArgumentException("模板已更换，请重新生成预览");
        for(JSONObject e:ReportModel.list(snapshot.getJSONArray("items"))){JSONObject actual=current.get(e.getString("id"));if(actual==null||actual.getBoolean("deleted")||!actual.getString("state").equals("verified")||actual.getInt("version")!=e.getInt("version"))throw new IllegalArgumentException("费用已改变，请重新核对并生成预览");for(JSONObject m:ReportModel.list(e.getJSONArray("materials")))if(!PrintExport.sha(ledger.materialFile(m.getString("id"))).equals(m.getString("digest")))throw new IOException("原件内容已变化，提交已停止");}
    }
    public JSONObject submit(String id) throws Exception{
        synchronized(ledger){SQLiteDatabase db=ledger.getWritableDatabase();db.beginTransaction();try{JSONObject snapshot;String status,path,xlsx;try(Cursor c=db.rawQuery("SELECT status,manifest,path,xlsx_path FROM reports WHERE id=?",new String[]{id})){if(!c.moveToFirst())throw new IllegalArgumentException("预览不存在");status=c.getString(0);snapshot=new JSONObject(c.getString(1));path=c.getString(2);xlsx=c.getString(3);}if(status.equals("submitted")){db.setTransactionSuccessful();return new JSONObject().put("already_submitted",true);}if(!status.equals("preview"))throw new IllegalArgumentException("请重新生成最终预览");check(snapshot);if(!PrintExport.sha(new File(path)).equals(snapshot.getString("pdf_digest"))||!PrintExport.sha(new File(xlsx)).equals(snapshot.getString("xlsx_digest")))throw new IOException("预览文件已变化，请重新生成");
            Set<String> submitted=new HashSet<>();for(JSONObject e:ReportModel.list(snapshot.getJSONArray("items"))){if(!submitted.add(e.getString("id")))continue;ContentValues v=new ContentValues();v.put("state","submitted");if(db.update("expenses",v,"id=? AND state='verified' AND version=? AND deleted=0",new String[]{e.getString("id"),String.valueOf(e.getInt("version"))})!=1)throw new IllegalArgumentException("费用已变化，整批提交已回滚");db.execSQL("UPDATE expenses SET version=version+1 WHERE id=?",new Object[]{e.getString("id")});}
            db.execSQL("UPDATE reports SET status='submitted' WHERE id=?",new Object[]{id});ledger.event(db,"report_submitted",id);db.setTransactionSuccessful();return new JSONObject().put("submitted",true);
        }finally{db.endTransaction();}}
    }
    public File file(String id,boolean excel){try(Cursor c=ledger.getReadableDatabase().rawQuery("SELECT path,xlsx_path FROM reports WHERE id=?",new String[]{id})){if(!c.moveToFirst())throw new IllegalArgumentException("报销包不存在");File f=new File(c.getString(excel?1:0));if(!f.isFile())throw new IllegalArgumentException("文件不存在，请重新生成");return f;}}
}
