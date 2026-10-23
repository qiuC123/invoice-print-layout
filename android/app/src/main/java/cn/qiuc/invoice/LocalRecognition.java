package cn.qiuc.invoice;

import android.content.Context;
import android.graphics.*;
import android.graphics.pdf.PdfRenderer;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import com.google.android.gms.tasks.Tasks;
import com.google.mlkit.vision.common.InputImage;
import com.google.mlkit.vision.text.TextRecognition;
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions;
import com.google.mlkit.vision.text.TextRecognizer;
import com.google.mlkit.vision.text.Text;
import com.tom_roush.pdfbox.pdmodel.PDDocument;
import com.tom_roush.pdfbox.text.PDFTextStripper;
import java.io.*;
import java.util.*;
import java.util.concurrent.TimeUnit;

/** Bundled Chinese OCR; network and Google Play model downloads are unnecessary. */
public final class LocalRecognition implements AutoCloseable {
    private final Context context;
    private final TextRecognizer recognizer=TextRecognition.getClient(new ChineseTextRecognizerOptions.Builder().build());
    public LocalRecognition(Context context) { this.context=context; }
    public String read(File file,boolean pdf) throws Exception {
        if(!pdf){Bitmap bitmap=ImageFiles.read(file,4096);try{return readImage(bitmap);}finally{bitmap.recycle();}}
        try(PDDocument doc=PDDocument.load(file)) {
            if(doc.isEncrypted())throw new IOException("加密PDF请手动处理");
            if(doc.getNumberOfPages()>10)throw new IOException("PDF超过10页，请拆分后识别，原件已保留");
            String text=new PDFTextStripper().getText(doc);
            if(text.strip().length()>20)return text.substring(0,Math.min(text.length(),24000));
        }
        StringBuilder text=new StringBuilder();
        try(ParcelFileDescriptor fd=ParcelFileDescriptor.open(file,ParcelFileDescriptor.MODE_READ_ONLY);PdfRenderer renderer=new PdfRenderer(fd)) {
            if(renderer.getPageCount()>4)throw new IOException("扫描PDF超过4页，请拆分后识别，原件已保留");
            for(int i=0;i<renderer.getPageCount();i++)try(PdfRenderer.Page page=renderer.openPage(i)) {
                float scale=Math.min(3f,1600f/page.getWidth());Bitmap bitmap=Bitmap.createBitmap(Math.max(1,Math.round(page.getWidth()*scale)),Math.max(1,Math.round(page.getHeight()*scale)),Bitmap.Config.ARGB_8888);
                try { bitmap.eraseColor(Color.WHITE);page.render(bitmap,null,null,PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY);
                    text.append(readImage(bitmap)).append('\n');
                } finally { bitmap.recycle(); }
            }
        }return text.toString();
    }
    private String readImage(Bitmap bitmap)throws Exception{
        Text result=Tasks.await(recognizer.process(InputImage.fromBitmap(bitmap,0)),60,TimeUnit.SECONDS);String raw=result.getText();
        boolean screen=raw.contains("账单详")||raw.contains("订单已")||raw.contains("订单己");int shops=0,statuses=0;List<TextLayout.Line> lines=new ArrayList<>();
        for(Text.TextBlock block:result.getTextBlocks())for(Text.Line line:block.getLines()){
            Rect bounds=line.getBoundingBox();if(bounds==null)continue;String value=line.getText(),compact=java.text.Normalizer.normalize(value,java.text.Normalizer.Form.NFKC).replaceAll("\\s","");
            if(screen&&shops<2&&Rules.shopShaped(Rules.shopValue(compact))){String refined=refine(bitmap,bounds,false);if(!refined.isEmpty()){String check=java.text.Normalizer.normalize(refined,java.text.Normalizer.Form.NFKC).replaceAll("\\s","");if(Rules.shopShaped(Rules.shopValue(check)))value=refined;}shops++;}
            else if(screen&&statuses<1&&compact.startsWith("交易")&&compact.endsWith("功")&&!compact.equals("交易成功")){String refined=refine(bitmap,bounds,true);if(!refined.isEmpty())value=refined;statuses++;}
            lines.add(new TextLayout.Line(value,bounds.left,bounds.top,bounds.right,bounds.bottom));
        }return TextLayout.rows(lines);
    }
    private String refine(Bitmap original,Rect bounds,boolean contrast)throws Exception{
        int left=Math.max(0,bounds.left-8),top=Math.max(0,bounds.top-8),right=Math.min(original.getWidth(),bounds.right+8),bottom=Math.min(original.getHeight(),bounds.bottom+8);if(right<=left||bottom<=top)return "";
        float scale=Math.min(2f,1600f/(right-left));int width=Math.max(1,Math.round((right-left)*scale)),height=Math.max(1,Math.round((bottom-top)*scale));Bitmap crop=Bitmap.createBitmap(width+128,height+128,Bitmap.Config.ARGB_8888);
        try{Canvas canvas=new Canvas(crop);canvas.drawColor(Color.WHITE);canvas.drawBitmap(original,new Rect(left,top,right,bottom),new Rect(64,64,64+width,64+height),new Paint(Paint.FILTER_BITMAP_FLAG));
            if(contrast){int[] pixels=new int[crop.getWidth()*crop.getHeight()];crop.getPixels(pixels,0,crop.getWidth(),0,0,crop.getWidth(),crop.getHeight());for(int i=0;i<pixels.length;i++){int color=pixels[i];pixels[i]=(Color.red(color)*299+Color.green(color)*587+Color.blue(color)*114)/1000<210?Color.BLACK:Color.WHITE;}crop.setPixels(pixels,0,crop.getWidth(),0,0,crop.getWidth(),crop.getHeight());}
            return Tasks.await(recognizer.process(InputImage.fromBitmap(crop,0)),60,TimeUnit.SECONDS).getText().replace('\n',' ').trim();
        }catch(Exception error){return "";}finally{crop.recycle();}
    }
    @Override public void close() { recognizer.close(); }
}
