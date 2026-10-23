package cn.qiuc.invoice;

import android.content.Context;
import android.graphics.*;
import com.tom_roush.pdfbox.pdmodel.*;
import com.tom_roush.pdfbox.pdmodel.common.PDRectangle;
import com.tom_roush.pdfbox.pdmodel.font.PDType0Font;
import com.tom_roush.pdfbox.pdmodel.graphics.image.*;
import com.tom_roush.pdfbox.multipdf.PDFMergerUtility;
import org.json.*;
import java.io.*;
import java.security.MessageDigest;
import java.util.*;

/** Original PDF pages are merged as PDF objects; only original image proofs become images. */
public final class PrintExport {
    public static String sha(File file) throws Exception {
        MessageDigest md=MessageDigest.getInstance("SHA-256");try(InputStream in=new FileInputStream(file)) { byte[] block=new byte[65536];int n;while((n=in.read(block))!=-1)md.update(block,0,n); }
        StringBuilder out=new StringBuilder();for(byte b:md.digest())out.append(String.format(Locale.ROOT,"%02x",b));return out.toString();
    }
    public static File create(Context context,Ledger ledger,String project,String name) throws Exception {
        JSONArray all=ledger.state(project).getJSONArray("expenses");List<JSONObject> expenses=new ArrayList<>();
        for(int i=0;i<all.length();i++) { JSONObject item=all.getJSONObject(i);if(item.getString("state").equals("verified"))expenses.add(item); }
        if(expenses.isEmpty())throw new IllegalArgumentException("请先核对费用，再生成打印包");
        expenses.sort(Comparator.comparingInt((JSONObject e)->Rules.CATEGORIES.indexOf(e.optString("category"))).thenComparing(e->e.optString("date")).thenComparing(e->e.optString("id")));
        String id=Ledger.id();File folder=new File(context.getFilesDir(),"exports");folder.mkdirs();File target=new File(folder,id+".pdf");File partial=new File(folder,id+".part");long total=0;
        try(PDDocument document=new PDDocument();InputStream fontStream=context.getAssets().open("fonts/NotoSansSC.ttf")) {
            PDType0Font font=PDType0Font.load(document,fontStream,true);
            // Summary and proof pages follow precisely the same expense order.
            for(int offset=0;offset<expenses.size();offset+=18) {
                PDPage page=new PDPage(PDRectangle.A4);document.addPage(page);
                try(PDPageContentStream stream=new PDPageContentStream(document,page)) {
                    line(stream,font,18,48,790,"报销明细 · "+name);line(stream,font,10,48,764,"打印包不代表财务送达或已报销。原件完整保留。");
                    int y=733;
                    for(int i=offset;i<Math.min(offset+18,expenses.size());i++) { JSONObject e=expenses.get(i);String row=(i+1)+". "+e.optString("date")+"  "+e.optString("owner")+"  "+e.optString("category")+"  ¥"+Rules.money(e.getLong("cents"));line(stream,font,11,48,y,row);line(stream,font,10,64,y-15,e.optString("title"));y-=36;total+=e.getLong("cents"); }
                    if(offset+18>=expenses.size())line(stream,font,12,48,60,"共 "+expenses.size()+" 笔，合计 ¥"+Rules.money(total));
                }
            }
            PDFMergerUtility merger=new PDFMergerUtility();
            for(JSONObject expense:expenses) {
                JSONArray materials=expense.getJSONArray("materials");boolean hasInvoice=false;
                for(int i=0;i<materials.length();i++)hasInvoice|=materials.getJSONObject(i).getString("role").equals("invoice");
                for(int i=0;i<materials.length();i++) {
                    JSONObject material=materials.getJSONObject(i);String role=material.getString("role");
                    if(hasInvoice && Set.of("高铁","打车","顺丰").contains(expense.getString("category")) && role.equals("payment"))continue;
                    File file=ledger.materialFile(material.getString("id"));if(!sha(file).equals(material.getString("digest")))throw new IOException("原件内容已变化，请检查材料");
                    if(material.getString("mime").equals("application/pdf")) {
                        try(PDDocument source=PDDocument.load(file)) { if(source.isEncrypted())throw new IOException("加密PDF暂不能合成，原件已保留");merger.appendDocument(document,source); }
                    } else {
                        Bitmap bitmap=BitmapFactory.decodeFile(file.getAbsolutePath());if(bitmap==null)throw new IOException("图片读取失败");
                        try {
                            PDPage page=new PDPage(PDRectangle.A4);document.addPage(page);PDImageXObject image=LosslessFactory.createFromImage(document,bitmap);
                            float scale=Math.min((page.getMediaBox().getWidth()-36)/bitmap.getWidth(),(page.getMediaBox().getHeight()-36)/bitmap.getHeight());float width=bitmap.getWidth()*scale,height=bitmap.getHeight()*scale;
                            try(PDPageContentStream stream=new PDPageContentStream(document,page)) { stream.drawImage(image,(page.getMediaBox().getWidth()-width)/2,(page.getMediaBox().getHeight()-height)/2,width,height); }
                        } finally { bitmap.recycle(); }
                    }
                }
            }
            // Refuse export if an expense changed while reading originals.
            JSONArray latest=ledger.state(project).getJSONArray("expenses");Map<String,String> versions=new HashMap<>();for(int i=0;i<latest.length();i++){JSONObject e=latest.getJSONObject(i);versions.put(e.getString("id"),e.toString());}
            for(JSONObject e:expenses)if(!e.toString().equals(versions.get(e.getString("id"))))throw new IOException("费用在生成期间已变化，请重新生成");
            document.save(partial);
        } catch(Exception error) { partial.delete();throw error; }
        if(!partial.renameTo(target))throw new IOException("打印包保存失败");
        ledger.report(project,id,target,total,expenses.size());return target;
    }
    private static void line(PDPageContentStream stream,PDType0Font font,int size,float x,float y,String text) throws IOException {
        // Wrap long input over existing summary rows rather than silently crop original documents.
        String safe=text.replace('\n',' ').replace('\r',' ');if(safe.length()>62)safe=safe.substring(0,59)+"…";
        stream.beginText();stream.setFont(font,size);stream.newLineAtOffset(x,y);stream.showText(safe);stream.endText();
    }
    private PrintExport() {}
}
