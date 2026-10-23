package cn.qiuc.invoice;

import android.content.Context;
import android.content.ContextWrapper;
import android.database.DatabaseErrorHandler;
import android.database.sqlite.SQLiteDatabase;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import org.junit.*;
import org.junit.runner.RunWith;
import org.json.*;
import java.io.*;
import java.util.*;
import static org.junit.Assert.*;
import com.tom_roush.pdfbox.android.PDFBoxResourceLoader;
import com.tom_roush.pdfbox.pdmodel.*;
import com.tom_roush.pdfbox.pdmodel.common.PDRectangle;
import com.tom_roush.pdfbox.pdmodel.font.PDType1Font;
import com.tom_roush.pdfbox.text.PDFTextStripper;

@RunWith(AndroidJUnit4.class)
public class LedgerInstrumentedTest {
    private Context context;private Ledger ledger;private String project;
    @Before public void setup() throws Exception {
        Context target=InstrumentationRegistry.getInstrumentation().getTargetContext();
        File directory=new File(target.getCacheDir(),"ledger-tests-"+Ledger.id());assertTrue(directory.mkdirs());
        context=new ContextWrapper(target){
            @Override public File getFilesDir(){return directory;}
            @Override public android.content.SharedPreferences getSharedPreferences(String name,int mode){return target.getSharedPreferences(directory.getName()+"-"+name,mode);}
            @Override public File getDatabasePath(String name){return new File(directory,name);}
            @Override public boolean deleteDatabase(String name){return SQLiteDatabase.deleteDatabase(getDatabasePath(name));}
            @Override public SQLiteDatabase openOrCreateDatabase(String name,int mode,SQLiteDatabase.CursorFactory factory){return SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name),factory);}
            @Override public SQLiteDatabase openOrCreateDatabase(String name,int mode,SQLiteDatabase.CursorFactory factory,DatabaseErrorHandler handler){return SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name).getPath(),factory,handler);}
        };
        context.deleteDatabase("invoice-assistant.sqlite3");ledger=new Ledger(context);project=ledger.project("合成样本项目").getString("id");
        PDFBoxResourceLoader.init(InstrumentationRegistry.getInstrumentation().getTargetContext());
    }
    @After public void cleanup(){if(ledger!=null)ledger.close();if(context!=null){context.deleteDatabase("invoice-assistant.sqlite3");PortableBackup.erase(context.getFilesDir());}}
    private String material(String digest,String role) throws Exception {
        String id=Ledger.id();File file=new File(context.getFilesDir(),id+".pdf");
        try(PDDocument document=new PDDocument()){PDPage page=new PDPage(PDRectangle.A4);document.addPage(page);try(PDPageContentStream stream=new PDPageContentStream(document,page)){stream.beginText();stream.setFont(PDType1Font.HELVETICA,12);stream.newLineAtOffset(40,780);stream.showText("SYNTHETIC SOURCE "+digest);stream.endText();}document.save(file);}
        ledger.addMaterial(id,project,digest,"synthetic.pdf",file,"application/pdf",Map.of("role",role),"synthetic text");return id;
    }
    private JSONObject draft(String...ids)throws Exception{return new JSONObject().put("project_id",project).put("title","测试费用").put("merchant","测试商店").put("owner","测试人员").put("date","2026-10-17").put("amount","17.00").put("category","高铁").put("material_ids",new JSONArray(Arrays.asList(ids)));}
    @Test public void originalDigestDedupAndPersistence() throws Exception {
        String id=material("duplicate-digest","invoice");assertFalse(ledger.addMaterial(Ledger.id(),project,"duplicate-digest","copy.pdf",ledger.materialFile(id),"application/pdf",Map.of("role","invoice"),"copy"));
        ledger.close();ledger=new Ledger(context);assertEquals(1,ledger.materials(project,null).length());
    }
    @Test public void materialCannotBeCountedInTwoExpensesAndRollbackIsAtomic()throws Exception {
        String used=material("used","invoice"),fresh=material("fresh","invoice");ledger.save(draft(used));
        try{ledger.save(draft(fresh,used));fail("should reject reused material");}catch(IllegalArgumentException expected){}
        assertEquals(1,ledger.state(project).getJSONArray("expenses").length());assertEquals(fresh,ledger.materials(project,null).getJSONObject(0).getString("id"));
    }
    @Test public void detachedDraftRemainsEditableButCannotBeVerified()throws Exception {
        String material=material("detached-edit","invoice"),id=ledger.save(draft(material)).getString("id");
        new Workbench(ledger).call("detach",new JSONObject().put("expense_id",id).put("material_id",material));
        JSONObject old=ledger.state(project).getJSONArray("expenses").getJSONObject(0);
        ledger.save(draft().put("id",id).put("version",old.getInt("version")).put("title","无原件草稿可修改").put("amount","23.45"));
        JSONObject edited=ledger.state(project).getJSONArray("expenses").getJSONObject(0);
        assertEquals("无原件草稿可修改",edited.getString("title"));assertEquals(2345,edited.getLong("cents"));
        assertEquals(0,edited.getJSONArray("materials").length());assertEquals("review",edited.getString("state"));
        try{ledger.verify(id,true);fail("no original must still block verification");}catch(IllegalArgumentException expected){}
    }
    @Test public void unknownAmountCannotBeVerifiedEvenWithMissingOverride()throws Exception {
        JSONObject d=draft(material("unknown","other")).put("amount","");String id=ledger.save(d).getString("id");
        try{ledger.verify(id,true);fail("unknown amount");}catch(IllegalArgumentException expected){}
        assertEquals("review",ledger.state(project).getJSONArray("expenses").getJSONObject(0).getString("state"));
    }
    @Test public void missingMaterialsNeedExplicitConfirmationAndStayRecorded()throws Exception {
        String id=ledger.save(draft(material("taxi","invoice")).put("category","打车")).getString("id");
        assertTrue(ledger.verify(id,false).getBoolean("requires_confirmation"));assertEquals("review",ledger.state(project).getJSONArray("expenses").getJSONObject(0).getString("state"));
        ledger.verify(id,true);JSONObject e=ledger.state(project).getJSONArray("expenses").getJSONObject(0);assertTrue(e.getBoolean("missing_confirmed"));assertEquals("行程单",e.getJSONArray("missing").getString(0));
        ledger.returnReview(id);assertEquals("review",ledger.state(project).getJSONArray("expenses").getJSONObject(0).getString("state"));
    }
    @Test public void reimbursementLabelNeverChangesExpenseStateOrMoney()throws Exception {
        ledger.save(draft(material("manual-label","invoice")));String before=ledger.state(project).getJSONArray("expenses").toString();ledger.reimbursement(project,true);
        assertEquals(before,ledger.state(project).getJSONArray("expenses").toString());assertTrue(ledger.state(project).getJSONArray("projects").getJSONObject(0).getBoolean("reimbursed"));
    }
    @Test public void pdfExportPreservesExtractableOriginalTextAndNeverChangesLedger()throws Exception {
        String material=material("vector-proof","invoice");File file=ledger.materialFile(material);String sha=PrintExport.sha(file);
        // Fixture records must pin actual bytes, just as real import does.
        ledger.getWritableDatabase().execSQL("UPDATE materials SET digest=? WHERE id=?",new Object[]{sha,material});
        ledger.verify(ledger.save(draft(material)).getString("id"),false);String before=ledger.state(project).getJSONArray("expenses").toString();
        Context target=InstrumentationRegistry.getInstrumentation().getTargetContext();File report=PrintExport.create(target,ledger,project,"合成样本项目");
        try(PDDocument doc=PDDocument.load(report)){
            assertEquals(2,doc.getNumberOfPages());String text=new PDFTextStripper().getText(doc);
            assertTrue("original text missing: "+text,text.contains("SYNTHETIC SOURCE vector-proof"));
            assertTrue("amount text missing: "+text,text.contains("17.00"));
            // Noto CJK shares glyphs with compatibility radicals (e.g. ⽤ / 用).
            // Match the desktop documents.py NFKC text-normalization contract.
            String normalized=java.text.Normalizer.normalize(text,java.text.Normalizer.Form.NFKC);
            assertTrue("Chinese text missing: "+text,normalized.contains("测试费用"));
        }
        assertEquals(sha,PrintExport.sha(file));assertEquals(before,ledger.state(project).getJSONArray("expenses").toString());
        report.delete();file.delete();
    }
    private String actual(String suffix,String role)throws Exception{String id=material(suffix,role);ledger.getWritableDatabase().execSQL("UPDATE materials SET digest=? WHERE id=?",new Object[]{PrintExport.sha(ledger.materialFile(id)),id});return id;}
    private JSONObject preview()throws Exception{return new ReportService(context,ledger).create(new JSONObject().put("project_id",project));}
    @Test public void previewAndExcelShareMoneyAndOriginalOrderThenSubmissionIsIdempotent()throws Exception{
        String m=actual("new-report","invoice"),id=ledger.save(draft(m).put("order_number","10000000100000000001").put("title","=NOT_A_FORMULA")).getString("id");ledger.verify(id,false);JSONObject r=preview();assertEquals(1700,r.getLong("total"));ReportService service=new ReportService(context,ledger);File excel=service.file(r.getString("id"),true);Map<String,byte[]> zip=ReportWorkbook.readZip(excel);String detail=new String(zip.get("xl/worksheets/android-details.xml"),java.nio.charset.StandardCharsets.UTF_8);assertTrue(detail.contains("10000000100000000001"));assertTrue(detail.contains("<t xml:space=\"preserve\">=NOT_A_FORMULA</t>"));assertTrue(new String(zip.get("xl/worksheets/sheet1.xml"),java.nio.charset.StandardCharsets.UTF_8).contains("SUM(H8:H34)"));try(PDDocument pdf=PDDocument.load(service.file(r.getString("id"),false))){assertEquals(3,pdf.getNumberOfPages());assertTrue(new PDFTextStripper().getText(pdf).contains("SYNTHETIC SOURCE new-report"));}service.submit(r.getString("id"));assertTrue(service.submit(r.getString("id")).getBoolean("already_submitted"));assertEquals("submitted",ledger.state(project).getJSONArray("expenses").getJSONObject(0).getString("state"));try{preview();fail("no duplicate pending package");}catch(IllegalArgumentException expected){}ledger.returnReview(id);assertEquals("submitted",ledger.state(project).getJSONArray("reports").getJSONObject(0).getString("status"));
    }
    @Test public void changedExpenseOrOriginalBlocksWholeSubmission()throws Exception{
        String m=actual("stale","invoice"),id=ledger.save(draft(m)).getString("id");ledger.verify(id,false);String report=preview().getString("id");ledger.returnReview(id);try{new ReportService(context,ledger).submit(report);fail("stale submission");}catch(IllegalArgumentException expected){}assertEquals("preview",ledger.state(project).getJSONArray("reports").getJSONObject(0).getString("status"));ledger.verify(id,false);report=preview().getString("id");try(FileOutputStream out=new FileOutputStream(ledger.materialFile(m),true)){out.write(32);}try{new ReportService(context,ledger).submit(report);fail("changed original");}catch(IOException expected){}assertEquals("verified",ledger.state(project).getJSONArray("expenses").getJSONObject(0).getString("state"));
    }
    @Test public void rowOverflowNeverDropsMoneyOrCustomNames()throws Exception{
        List<JSONObject> items=new ArrayList<>();for(int i=0;i<9;i++)items.add(new JSONObject().put("id","e"+i).put("category","材料采购").put("project_type","materials").put("project_name","物品"+i).put("cents",101));JSONObject snapshot=ReportModel.snapshot(items);assertEquals(909,snapshot.getLong("total_cents"));assertEquals(9,snapshot.getJSONArray("items").length());assertEquals(5,snapshot.getJSONArray("rows").length());assertTrue(snapshot.getJSONArray("rows").getJSONObject(4).getString("name").contains("物品8"));
    }
    @Test public void manualMaterialFieldsSurviveRecognition()throws Exception{
        String id=actual("protected","invoice");Workbench w=new Workbench(ledger);w.call("materialEdit",new JSONObject().put("id",id).put("fields",new JSONObject().put("amount","22.00").put("merchant","人工店铺")));w.recognized(id,Map.of("amount","99.00","merchant","机器店铺","date","2026-10-16"),"new");JSONObject f=ledger.materials(project,null).getJSONObject(0).getJSONObject("fields");assertEquals("22.00",f.getString("amount"));assertEquals("人工店铺",f.getString("merchant"));assertEquals("2026-10-16",f.getString("date"));
    }
    @Test public void allocationsMustBalanceAndRenameKeepsStableIdentity()throws Exception{
        Workbench w=new Workbench(ledger);String one=((JSONObject)w.call("person",new JSONObject().put("project_id",project).put("name","甲"))).getString("id"),two=((JSONObject)w.call("person",new JSONObject().put("project_id",project).put("name","乙"))).getString("id");JSONArray allocations=new JSONArray().put(new JSONObject().put("person_id",one).put("cents",800)).put(new JSONObject().put("person_id",two).put("cents",900));String m=actual("shares","invoice");JSONObject input=draft(m).put("owner","").put("allocations",allocations);ledger.save(input);w.call("person",new JSONObject().put("project_id",project).put("id",one).put("name","甲改名"));JSONObject e=ledger.state(project).getJSONArray("expenses").getJSONObject(0);assertEquals("甲改名",e.getJSONArray("allocations").getJSONObject(0).getString("name"));assertEquals(one,e.getJSONArray("allocations").getJSONObject(0).getString("person_id"));assertEquals(1700,e.getLong("cents"));w.call("mergePeople",new JSONObject().put("project_id",project).put("from",one).put("to",two));e=ledger.state(project).getJSONArray("expenses").getJSONObject(0);assertEquals(1,e.getJSONArray("allocations").length());assertEquals(1700,e.getJSONArray("allocations").getJSONObject(0).getLong("cents"));
        try{ledger.save(draft(actual("badshares","invoice")).put("allocations",new JSONArray().put(new JSONObject().put("person_id",two).put("cents",1600))));fail("unbalanced");}catch(IllegalArgumentException expected){}assertEquals(1,ledger.state(project).getJSONArray("expenses").length());
    }
    @Test public void batchVerificationRollsBackWhenAnyExpenseHasUnknownMoney()throws Exception{
        String known=ledger.save(draft(actual("known","invoice"))).getString("id"),unknown=ledger.save(draft(actual("missing-money","invoice")).put("amount","")).getString("id");try{new Workbench(ledger).call("expenseBatch",new JSONObject().put("action","verify").put("ids",new JSONArray().put(known).put(unknown)));fail("batch unknown");}catch(IllegalArgumentException expected){}for(JSONObject e:ReportModel.list(ledger.state(project).getJSONArray("expenses")))assertEquals("review",e.getString("state"));
    }
    @Test public void archiveRestoreAndSoftDeletionPreserveOriginals()throws Exception{
        String m=actual("archive","invoice");String hash=PrintExport.sha(ledger.materialFile(m));Workbench w=new Workbench(ledger);w.call("materialStatus",new JSONObject().put("id",m).put("status","archived"));assertEquals(0,ledger.materials(project,null).length());w.call("materialStatus",new JSONObject().put("id",m).put("status","active"));String id=ledger.save(draft(m)).getString("id");w.call("expenseBatch",new JSONObject().put("action","delete").put("ids",new JSONArray().put(id)));assertTrue(ledger.state(project).getJSONArray("expenses").getJSONObject(0).getBoolean("deleted"));w.call("expenseBatch",new JSONObject().put("action","restore").put("ids",new JSONArray().put(id)));assertEquals(hash,PrintExport.sha(ledger.materialFile(m)));assertFalse(ledger.state(project).getJSONArray("expenses").getJSONObject(0).getBoolean("deleted"));
    }
    @Test public void duplicateOrDifferentOrderNamespaceNeverCreatesUnsafeAutoMatch()throws Exception{
        String a=actual("pair-a","invoice"),b=actual("pair-b","payment");for(String id:List.of(a,b))ledger.getWritableDatabase().execSQL("UPDATE materials SET fields=? WHERE id=?",new Object[]{new JSONObject().put("merchant","唯一店铺").put("amount","17.00").put("order_number",id).put("date",id.equals(a)?"2026-10-01":"2026-10-17").toString(),id});Workbench w=new Workbench(ledger);JSONObject args=new JSONObject().put("project_id",project);assertEquals(1,((JSONObject)w.call("autoMatch",args)).getJSONArray("proposals").length());String duplicate=actual("pair-c","invoice");ledger.getWritableDatabase().execSQL("UPDATE materials SET fields=(SELECT fields FROM materials WHERE id=?) WHERE id=?",new Object[]{a,duplicate});assertEquals(0,((JSONObject)w.call("autoMatch",args.put("apply",true))).getJSONArray("proposals").length());assertEquals(0,ledger.state(project).getJSONArray("expenses").length());w.call("materialStatus",new JSONObject().put("id",duplicate).put("status","archived"));w.call("autoMatch",args);JSONObject e=ledger.state(project).getJSONArray("expenses").getJSONObject(0);assertEquals("review",e.getString("state"));assertEquals(1700,e.getLong("cents"));assertEquals("",e.getString("owner"));
    }
    @Test public void parsedShoppingBillPairCreatesOnlyOneUnverifiedDraft()throws Exception{
        String order=actual("shopping-view","purchase"),payment=actual("bill-view","payment");
        String purchase="订单己送达\n感谢你对淘宝闪购的信任\n闪购样本便利店(南湖店)>\n鼠标¥16\n打包费¥1\n总价¥20\n总优惠¥3实付款¥17";
        String bill="账单详情\n淘宝闪购\n-17.00\n交易成功\n支付时间2026-10-01 16:23:47\n付款方式农业银行储蓄卡(0000)\n商品说明样本便利店(南湖店)";
        Workbench w=new Workbench(ledger);w.recognized(order,Rules.parse(purchase),purchase);w.recognized(payment,Rules.parse(bill),bill);
        JSONObject args=new JSONObject().put("project_id",project);JSONObject preview=(JSONObject)w.call("autoMatch",args);assertEquals(1,preview.getJSONArray("proposals").length());assertEquals(0,ledger.state(project).getJSONArray("expenses").length());
        w.call("autoMatch",args.put("apply",true));JSONObject expense=ledger.state(project).getJSONArray("expenses").getJSONObject(0);assertEquals(1700,expense.getLong("cents"));assertEquals("review",expense.getString("state"));assertEquals("",expense.getString("owner"));assertEquals("2026-10-01",expense.getString("date"));assertEquals(2,expense.getJSONArray("materials").length());
        assertEquals(0,((JSONObject)w.call("autoMatch",args)).getJSONArray("proposals").length());assertEquals(1,ledger.state(project).getJSONArray("expenses").length());
    }
    @Test public void shopConflictsStaySeparateAndBlankShopUsesExplicitMerchant()throws Exception{
        String a=actual("shop-a","purchase"),b=actual("shop-b","payment");Workbench w=new Workbench(ledger);
        for(String id:List.of(a,b))ledger.getWritableDatabase().execSQL("UPDATE materials SET fields=? WHERE id=?",new Object[]{new JSONObject().put("merchant",id.equals(a)?"甲便利店":"乙便利店").put("shop_name","").put("amount","17.00").toString(),id});
        JSONObject args=new JSONObject().put("project_id",project);assertEquals(0,((JSONObject)w.call("autoMatch",args)).getJSONArray("proposals").length());
        ledger.getWritableDatabase().execSQL("UPDATE materials SET fields=(SELECT fields FROM materials WHERE id=?) WHERE id=?",new Object[]{a,b});assertEquals(1,((JSONObject)w.call("autoMatch",args)).getJSONArray("proposals").length());
    }
    @Test public void backupRoundTripAndConflictRestoreAreAtomic()throws Exception{
        ledger.save(draft(actual("backup-source","invoice")));PortableBackup service=new PortableBackup(context,ledger);File zip=service.create();String digest=PrintExport.sha(zip);assertEquals(1,service.inspect(zip).getInt("expenses"));assertEquals(0,service.restore(zip,digest).getInt("inserted"));ledger.getWritableDatabase().execSQL("UPDATE expenses SET version=version+1");try{service.restore(zip,digest);fail("conflict");}catch(IOException expected){}assertEquals(1,ledger.state(project).getJSONArray("expenses").length());ledger.close();context.deleteDatabase("invoice-assistant.sqlite3");ledger=new Ledger(context);service=new PortableBackup(context,ledger);assertTrue(service.restore(zip,digest).getInt("inserted")>=3);assertEquals(1,ledger.state(project).getJSONArray("expenses").length());assertEquals(0,service.restore(zip,digest).getInt("inserted"));assertEquals(1,android.database.DatabaseUtils.longForQuery(ledger.getReadableDatabase(),"SELECT COUNT(*) FROM events WHERE kind='expense_draft_saved'",null));zip.delete();
    }
    @Test public void zipTraversalIsRejectedBeforeRestore()throws Exception{
        File f=new File(context.getFilesDir(),"malformed.zip");try(java.util.zip.ZipOutputStream zip=new java.util.zip.ZipOutputStream(new FileOutputStream(f))){zip.putNextEntry(new java.util.zip.ZipEntry("../escape"));zip.write(1);zip.closeEntry();}try{new PortableBackup(context,ledger).inspect(f);fail("zip traversal");}catch(IOException expected){}assertFalse(new File(context.getFilesDir().getParentFile(),"escape").exists());f.delete();
    }
    @Test public void cloudRequiresOptInAndRejectsInventedFields()throws Exception{
        OptionalServices services=new OptionalServices(context);assertFalse(services.enabled("glm"));JSONObject good=new JSONObject().put("fields",new JSONObject().put("amount","17.00").put("merchant","测试商店")).put("evidence",new JSONObject().put("amount","实付17.00").put("merchant","测试商店"));assertEquals("17.00",OptionalServices.validateExtraction(good,"测试商店 实付17.00").get("amount"));good.getJSONObject("fields").put("amount","27.00");try{OptionalServices.validateExtraction(good,"测试商店 实付17.00");fail("invented money");}catch(IllegalArgumentException expected){}assertFalse(OptionalServices.cleanGoods("商品：螺丝\n税号：100000000000000\n手机号：12345678901").contains("12345678901"));
    }
    @Test public void v1DatabaseUpgradePreservesAmountOwnerStateAndProjectLabel()throws Exception{
        ledger.close();context.deleteDatabase("invoice-assistant.sqlite3");try(SQLiteDatabase db=context.openOrCreateDatabase("invoice-assistant.sqlite3",0,null)){
            db.execSQL("CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,reimbursed INTEGER NOT NULL DEFAULT 0)");db.execSQL("CREATE TABLE materials(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),digest TEXT NOT NULL,name TEXT NOT NULL,path TEXT NOT NULL,mime TEXT NOT NULL,role TEXT NOT NULL,fields TEXT NOT NULL,text TEXT NOT NULL,expense_id TEXT,UNIQUE(project_id,digest))");db.execSQL("CREATE TABLE expenses(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),data TEXT NOT NULL,state TEXT NOT NULL)");db.execSQL("CREATE TABLE reports(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),path TEXT NOT NULL,total INTEGER NOT NULL,count INTEGER NOT NULL,created INTEGER NOT NULL)");db.execSQL("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,subject TEXT NOT NULL,created INTEGER NOT NULL)");db.execSQL("INSERT INTO projects VALUES(?,'原项目',1)",new Object[]{project});db.execSQL("INSERT INTO expenses VALUES('old',?,?,'verified')",new Object[]{project,new JSONObject().put("owner","原人员").put("category","高铁").put("cents",2345).toString()});db.setVersion(1);}
        ledger=new Ledger(context);JSONObject state=ledger.state(project),e=state.getJSONArray("expenses").getJSONObject(0);assertEquals(2345,e.getLong("cents"));assertEquals("verified",e.getString("state"));assertEquals("原人员",e.getString("owner"));assertFalse(e.getString("owner_id").isEmpty());assertTrue(state.getJSONArray("projects").getJSONObject(0).getBoolean("reimbursed"));assertEquals(2,ledger.getReadableDatabase().getVersion());
    }
    @Test public void packingKeepsVectorTextScaleAndCategoryBoundaries()throws Exception{
        try(PDDocument source=new PDDocument()){for(int i=0;i<3;i++){PDPage page=new PDPage(new PDRectangle(280,210));source.addPage(page);try(PDPageContentStream out=new PDPageContentStream(source,page)){out.beginText();out.setFont(PDType1Font.HELVETICA,12);out.newLineAtOffset(20,180);out.showText("VECTOR PACK "+i);out.endText();}}JSONArray items=new JSONArray();items.put(new JSONObject().put("report_row",12).put("project_name","rail").put("source_first",0).put("source_end",2));items.put(new JSONObject().put("report_row",16).put("project_name","hotel").put("source_first",2).put("source_end",3));try(PDDocument packed=VectorPacking.pack(source,items,2)){assertEquals(2,packed.getNumberOfPages());String text=new PDFTextStripper().getText(packed);assertTrue(text.contains("VECTOR PACK 0"));assertTrue(text.contains("VECTOR PACK 1"));assertTrue(text.contains("VECTOR PACK 2"));assertEquals("3",items.getJSONObject(0).getString("pages"));assertEquals("4",items.getJSONObject(1).getString("pages"));}}
    }
    @Test public void xmlEntitiesAndWrongTemplateRowsAreRejected()throws Exception{
        try{ReportWorkbook.parse("<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///private'>]><x>&e;</x>".getBytes(java.nio.charset.StandardCharsets.UTF_8));fail("DTD");}catch(IOException expected){}File f=new File(context.getFilesDir(),"wrong.xlsx");try(java.util.zip.ZipOutputStream zip=new java.util.zip.ZipOutputStream(new FileOutputStream(f))){for(Map.Entry<String,byte[]> entry:ReportWorkbook.readZip(defaultWorkbook()).entrySet()){zip.putNextEntry(new java.util.zip.ZipEntry(entry.getKey()));byte[] data=entry.getValue();if(entry.getKey().equals("xl/worksheets/sheet1.xml"))data=new String(data,java.nio.charset.StandardCharsets.UTF_8).replace("火车、飞机","错误项目").getBytes(java.nio.charset.StandardCharsets.UTF_8);zip.write(data);zip.closeEntry();}}try{new ReportWorkbook(f);fail("wrong row");}catch(IOException expected){}f.delete();
    }
    private File defaultWorkbook()throws Exception{File f=new File(context.getFilesDir(),"default.xlsx");new ReportWorkbook(null).save(f);return f;}
    @Test public void workbookNumericColumnNeverStoresHeaderTextAsANumber()throws Exception{
        Map<String,byte[]> entries=ReportWorkbook.readZip(defaultWorkbook());org.w3c.dom.Document xml=ReportWorkbook.parse(entries.get("xl/worksheets/sheet1.xml"));org.w3c.dom.NodeList cells=xml.getElementsByTagNameNS("http://schemas.openxmlformats.org/spreadsheetml/2006/main","c");boolean found=false;for(int i=0;i<cells.getLength();i++){org.w3c.dom.Element c=(org.w3c.dom.Element)cells.item(i);if(c.getAttribute("r").equals("H6")){found=true;assertEquals("inlineStr",c.getAttribute("t"));assertEquals("需报销",c.getTextContent());}}assertTrue(found);
    }
    @Test public void enabledCloudUsesTypedPayloadAndEncryptedKeyWithoutOverwritingManualFields()throws Exception{
        List<JSONObject> requests=new ArrayList<>();OptionalServices services=new OptionalServices(context,(service,payload)->{requests.add(payload);if(service.equals("glm"))return new JSONObject().put("choices",new JSONArray().put(new JSONObject().put("message",new JSONObject().put("content",new JSONObject().put("fields",new JSONObject().put("amount","17.00")).put("evidence",new JSONObject().put("amount","实付17.00")).toString()))));return new JSONObject().put("answers",new JSONObject().put("category",new JSONObject().put("type","choice").put("choice","materials").put("confidence",.95)));});
        try{services.extract("实付17.00");fail("default offline");}catch(IllegalArgumentException expected){}assertTrue(requests.isEmpty());services.configure(new JSONObject().put("service","glm").put("secret","SYNTHETIC_TEST_KEY").put("enabled",true));assertEquals("17.00",services.extract("实付17.00").get("amount"));assertFalse(context.getSharedPreferences("optional-services",0).getString("glm.secret","").contains("SYNTHETIC_TEST_KEY"));services.configure(new JSONObject().put("service","jev").put("secret","SYNTHETIC_TEST_KEY").put("enabled",true));assertEquals("材料采购",services.classify("商品 螺丝\n税号：123456789012345","搭建耗材").getString("category"));assertFalse(requests.get(1).getJSONObject("state").toString().contains("123456789012345"));services.configure(new JSONObject().put("service","glm").put("clear",true));assertFalse(services.enabled("glm"));
    }
    @Test public void mailMimeAttachmentsAreBoundedAndQrFallbackDoesNotPretendToBeInvoice()throws Exception{
        javax.mail.internet.MimeBodyPart body=new javax.mail.internet.MimeBodyPart();body.setText("ordinary message body");javax.mail.internet.MimeBodyPart attachment=new javax.mail.internet.MimeBodyPart();attachment.setFileName("synthetic.pdf");attachment.setDataHandler(new javax.activation.DataHandler(new javax.mail.util.ByteArrayDataSource("%PDF-SYNTHETIC".getBytes(),"application/pdf")));javax.mail.internet.MimeMultipart multipart=new javax.mail.internet.MimeMultipart();multipart.addBodyPart(body);multipart.addBodyPart(attachment);javax.mail.internet.MimeMessage message=new javax.mail.internet.MimeMessage(javax.mail.Session.getInstance(new Properties()));message.setContent(multipart);message.saveChanges();JSONArray received=new JSONArray();PhoneIntake.attachments(message,context.getCacheDir(),(file,name)->new JSONObject().put("name",name).put("bytes",file.length()),received,new int[]{0},0);assertEquals(1,received.length());assertEquals("synthetic.pdf",received.getJSONObject(0).getString("name"));JSONObject fallback=PhoneIntake.claim("https://example.org/invoice",context.getCacheDir(),(file,name)->{throw new AssertionError("unsupported host must not download");});assertEquals("manual",fallback.getString("status"));
    }
    @Test public void uniqueWholeOneToManyMatchCountsPaymentOnce()throws Exception{
        String a=actual("bill-ten","invoice"),b=actual("bill-seven","invoice"),p=actual("payment-seventeen","payment");int i=0;for(String id:List.of(a,b,p)){String amount=new String[]{"10.00","7.00","17.00"}[i++];ledger.getWritableDatabase().execSQL("UPDATE materials SET fields=? WHERE id=?",new Object[]{new JSONObject().put("merchant","同一明确店铺").put("shopping_platform","同一平台").put("amount",amount).put("date","2026-10-17").toString(),id});}JSONObject result=(JSONObject)new Workbench(ledger).call("autoMatch",new JSONObject().put("project_id",project).put("apply",true));assertEquals(1,result.getJSONArray("proposals").length());JSONObject expense=ledger.state(project).getJSONArray("expenses").getJSONObject(0);assertEquals(1700,expense.getLong("cents"));assertEquals(3,expense.getJSONArray("materials").length());assertEquals("review",expense.getString("state"));
    }
    @Test public void emptyMaterialsCannotPassVerificationAndConflictingAiDoesNotOverwriteLocalMoney()throws Exception{
        String m=actual("clear-proof","invoice"),id=ledger.save(draft(m)).getString("id");Workbench w=new Workbench(ledger);w.call("detach",new JSONObject().put("expense_id",id).put("material_id",m));try{ledger.verify(id,true);fail("no original");}catch(IllegalArgumentException expected){}w.recognized(m,Map.of("amount","17.00"),"实付17.00");w.recognized(m,Map.of("amount","99.00"),"实付17.00");JSONObject fields=ledger.materials(project,null).getJSONObject(0).getJSONObject("fields");assertEquals("17.00",fields.getString("amount"));assertTrue(fields.getString("warning").contains("手动确认"));
    }
    @Test public void bundledQrScannerReadsSyntheticCodeOffline()throws Exception{
        String value="https://example.org/synthetic-invoice-only";com.google.zxing.common.BitMatrix matrix=new com.google.zxing.qrcode.QRCodeWriter().encode(value,com.google.zxing.BarcodeFormat.QR_CODE,700,700);android.graphics.Bitmap bitmap=android.graphics.Bitmap.createBitmap(700,700,android.graphics.Bitmap.Config.ARGB_8888);for(int y=0;y<700;y++)for(int x=0;x<700;x++)bitmap.setPixel(x,y,matrix.get(x,y)?android.graphics.Color.BLACK:android.graphics.Color.WHITE);File f=new File(context.getFilesDir(),"synthetic-qr.png");try(FileOutputStream out=new FileOutputStream(f)){bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG,100,out);}bitmap.recycle();JSONArray result=PhoneIntake.qr(f);assertEquals(1,result.length());assertEquals(value,result.getString(0));
    }
    @Test public void cameraExifOrientationDoesNotRotateTheOriginalFile()throws Exception{
        File file=new File(context.getFilesDir(),"oriented.jpg");android.graphics.Bitmap bitmap=android.graphics.Bitmap.createBitmap(80,40,android.graphics.Bitmap.Config.ARGB_8888);bitmap.eraseColor(android.graphics.Color.BLUE);try(FileOutputStream out=new FileOutputStream(file)){bitmap.compress(android.graphics.Bitmap.CompressFormat.JPEG,95,out);}bitmap.recycle();androidx.exifinterface.media.ExifInterface exif=new androidx.exifinterface.media.ExifInterface(file.getAbsolutePath());exif.setAttribute(androidx.exifinterface.media.ExifInterface.TAG_ORIENTATION,"6");exif.saveAttributes();String hash=PrintExport.sha(file);bitmap=ImageFiles.read(file,2000);assertEquals(40,bitmap.getWidth());assertEquals(80,bitmap.getHeight());bitmap.recycle();assertEquals(hash,PrintExport.sha(file));
    }
    @Test public void allocatedReportHasTwoOwnerRowsButOneExpenseAndOneOriginal()throws Exception{
        Workbench w=new Workbench(ledger);String one=((JSONObject)w.call("person",new JSONObject().put("project_id",project).put("name","分摊甲"))).getString("id"),two=((JSONObject)w.call("person",new JSONObject().put("project_id",project).put("name","分摊乙"))).getString("id");String id=ledger.save(draft(actual("allocated-report","invoice")).put("owner","").put("allocations",new JSONArray().put(new JSONObject().put("person_id",one).put("cents",800)).put(new JSONObject().put("person_id",two).put("cents",900)))).getString("id");ledger.verify(id,false);JSONObject report=preview();assertEquals(1,report.getInt("count"));assertEquals(1700,report.getLong("total"));JSONObject manifest=ledger.state(project).getJSONArray("reports").getJSONObject(0).getJSONObject("manifest");assertEquals(2,manifest.getJSONArray("items").length());assertEquals("3",manifest.getJSONArray("items").getJSONObject(0).getString("pages"));assertEquals("3",manifest.getJSONArray("items").getJSONObject(1).getString("pages"));try(PDDocument document=PDDocument.load(new ReportService(context,ledger).file(report.getString("id"),false))){assertEquals(3,document.getNumberOfPages());}new ReportService(context,ledger).submit(report.getString("id"));assertEquals("submitted",ledger.state(project).getJSONArray("expenses").getJSONObject(0).getString("state"));
    }
    @Test public void retiredOwnerDoesNotSilentlyBecomeANewIdentity()throws Exception{
        Workbench w=new Workbench(ledger);String person=((JSONObject)w.call("person",new JSONObject().put("project_id",project).put("name","稳定人员"))).getString("id");w.call("person",new JSONObject().put("project_id",project).put("id",person).put("active",false));String m=actual("retired-person","invoice");try{ledger.save(draft(m).put("owner","稳定人员"));fail("retired owner duplicated");}catch(IllegalArgumentException expected){}assertEquals(1,ledger.state(project).getJSONArray("people").length());w.call("person",new JSONObject().put("project_id",project).put("id",person).put("active",true));ledger.save(draft(m).put("owner","稳定人员"));assertEquals(person,ledger.state(project).getJSONArray("expenses").getJSONObject(0).getString("owner_id"));
    }
    @Test public void importingAnAlreadyFilledTemplateCanReplaceItsDetailSheet()throws Exception{
        ledger.verify(ledger.save(draft(actual("template-roundtrip","invoice"))).getString("id"),false);ReportService service=new ReportService(context,ledger);String first=preview().getString("id");java.nio.file.Files.copy(service.file(first,true).toPath(),new File(context.getFilesDir(),"report-template.xlsx").toPath());JSONObject second=preview();assertEquals(1700,second.getLong("total"));ReportWorkbook workbook=new ReportWorkbook(service.file(second.getString("id"),true));assertEquals("17.00",workbook.text("H35"));Map<String,byte[]> entries=ReportWorkbook.readZip(service.file(second.getString("id"),true));org.w3c.dom.Document document=ReportWorkbook.parse(entries.get("xl/workbook.xml"));org.w3c.dom.NodeList sheets=document.getElementsByTagNameNS("http://schemas.openxmlformats.org/spreadsheetml/2006/main","sheet");int details=0;for(int i=0;i<sheets.getLength();i++)if(((org.w3c.dom.Element)sheets.item(i)).getAttribute("name").equals("报销明细"))details++;assertEquals(1,details);
    }
    @Test public void visionUploadNeedsItsOwnSwitchAndPreservesManualRole()throws Exception{
        List<JSONObject> requests=new ArrayList<>();OptionalServices services=new OptionalServices(context,(service,payload)->{requests.add(payload);JSONObject value=new JSONObject().put("text","支付成功\n实付17.00").put("fields",new JSONObject().put("amount","17.00")).put("evidence",new JSONObject().put("amount","实付17.00"));return new JSONObject().put("choices",new JSONArray().put(new JSONObject().put("message",new JSONObject().put("content",value.toString()))));});services.configure(new JSONObject().put("service","glm").put("enabled",true).put("secret","SYNTHETIC_TEST_KEY"));File file=new File(context.getFilesDir(),"vision-synthetic.png");android.graphics.Bitmap image=android.graphics.Bitmap.createBitmap(80,40,android.graphics.Bitmap.Config.ARGB_8888);image.eraseColor(android.graphics.Color.WHITE);try(FileOutputStream out=new FileOutputStream(file)){image.compress(android.graphics.Bitmap.CompressFormat.PNG,100,out);}image.recycle();String hash=PrintExport.sha(file);try{services.extract(file,false,"");fail("vision is separate opt-in");}catch(IllegalArgumentException expected){}assertTrue(requests.isEmpty());services.configure(new JSONObject().put("service","glm").put("enabled",true).put("images",true));Map<String,String> fields=services.extract(file,false,"");assertEquals("17.00",fields.get("amount"));assertEquals("payment",fields.get("role"));JSONArray content=requests.get(0).getJSONArray("messages").getJSONObject(0).getJSONArray("content");assertTrue(content.getJSONObject(1).getJSONObject("image_url").getString("url").startsWith("data:image/png;base64,"));assertEquals(hash,PrintExport.sha(file));String m=actual("manual-role","other");ledger.materialRole(m,"other");ledger.getWritableDatabase().execSQL("UPDATE materials SET fields=? WHERE id=?",new Object[]{new JSONObject().put("role","other").toString(),m});new Workbench(ledger).recognized(m,Map.of("role","invoice"),"电子发票");assertEquals("other",ledger.materials(project,null).getJSONObject(0).getString("role"));services.configure(new JSONObject().put("service","glm").put("clear",true));assertFalse(services.status().getJSONObject("glm").getBoolean("images"));
    }
    @Test public void paymentOnlyProfilePrintsFourVectorProofsOnOneA4()throws Exception{
        String[] proofs=new String[4];for(int i=0;i<4;i++)proofs[i]=actual("four-up-"+i,"payment");String id=ledger.save(draft(proofs).put("category","其他").put("basis","payment_only")).getString("id");ledger.verify(id,false);JSONObject report=preview();try(PDDocument document=PDDocument.load(new ReportService(context,ledger).file(report.getString("id"),false))){assertEquals(3,document.getNumberOfPages());PDFTextStripper reader=new PDFTextStripper();reader.setStartPage(3);reader.setEndPage(3);String text=reader.getText(document);for(int i=0;i<4;i++)assertTrue(text.contains("SYNTHETIC SOURCE four-up-"+i));assertEquals(PDRectangle.A4.getWidth(),document.getPage(2).getMediaBox().getWidth(),.01f);}assertEquals(1700,report.getLong("total"));
    }
}
