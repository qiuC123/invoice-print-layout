package cn.qiuc.invoice;

import com.tom_roush.pdfbox.pdmodel.*;
import com.tom_roush.pdfbox.pdmodel.common.PDRectangle;
import com.tom_roush.pdfbox.multipdf.LayerUtility;
import com.tom_roush.pdfbox.util.Matrix;
import org.json.*;
import java.util.*;

/** Pack whole short PDF pages at scale 1. No content detection, crop or rotation. */
public final class VectorPacking {
    private static final float W=PDRectangle.A4.getWidth(),H=PDRectangle.A4.getHeight(),GAP=8;
    private static final class Slot{float x,y,w,h;int page;Slot(float x,float y,float w,float h,int p){this.x=x;this.y=y;this.w=w;this.h=h;page=p;}}
    private static final class Placement{int source,page;float x,y;Placement(int source,int page,float x,float y){this.source=source;this.page=page;this.x=x;this.y=y;}}
    public static PDDocument pack(PDDocument source,JSONArray items,int summaryPages)throws Exception{
        PDDocument out=new PDDocument();try{LayerUtility layer=new LayerUtility(out);Map<Integer,Set<Integer>> pages=new HashMap<>();int offset=0;
            while(offset<items.length()){JSONObject first=items.getJSONObject(offset);int end=offset+1;while(end<items.length()&&items.getJSONObject(end).getInt("report_row")==first.getInt("report_row")&&items.getJSONObject(end).getString("project_name").equals(first.getString("project_name")))end++;
                List<Integer> groupPages=new ArrayList<>();Map<Integer,List<Integer>> owners=new HashMap<>();for(int i=offset;i<end;i++){JSONObject e=items.getJSONObject(i);for(int p=e.getInt("source_first");p<e.getInt("source_end");p++){if(!owners.containsKey(p))groupPages.add(p);owners.computeIfAbsent(p,k->new ArrayList<>()).add(i);}}
                List<Placement> layout=layout(source,groupPages);int count=layout.stream().mapToInt(p->p.page).max().orElse(-1)+1;
                if(count<groupPages.size()){int base=out.getNumberOfPages();for(int i=0;i<count;i++){Placement firstPlacement=null;for(Placement p:layout)if(p.page==i){firstPlacement=p;break;}PDPage original=source.getPage(firstPlacement.source);if(packable(original))out.addPage(new PDPage(PDRectangle.A4));else out.importPage(original);}for(Placement p:layout){PDPage target=out.getPage(base+p.page);if(packable(source.getPage(p.source)))try(PDPageContentStream stream=new PDPageContentStream(out,target,PDPageContentStream.AppendMode.APPEND,true,true)){stream.saveGraphicsState();stream.transform(Matrix.getTranslateInstance(p.x,H-p.y-source.getPage(p.source).getMediaBox().getHeight()));stream.drawForm(layer.importPageAsForm(source,p.source));stream.restoreGraphicsState();}for(int owner:owners.get(p.source))pages.computeIfAbsent(owner,k->new TreeSet<>()).add(summaryPages+base+p.page+1);}}
                else{for(int p:groupPages){out.importPage(source.getPage(p));for(int owner:owners.get(p))pages.computeIfAbsent(owner,k->new TreeSet<>()).add(summaryPages+out.getNumberOfPages());}}
                offset=end;
            }
            for(int i=0;i<items.length();i++){Set<Integer> numbers=pages.getOrDefault(i,Set.of());List<String> values=new ArrayList<>();for(int p:numbers)values.add(String.valueOf(p));items.getJSONObject(i).put("pages",String.join("、",values));}return out;
        }catch(Exception e){out.close();throw e;}
    }
    private static boolean packable(PDPage page){PDRectangle box=page.getMediaBox();return page.getRotation()==0&&box.getLowerLeftX()==0&&box.getLowerLeftY()==0&&box.getWidth()<=W&&box.getHeight()<H*.8f;}
    private static List<Placement> layout(PDDocument doc,List<Integer> group){List<Slot> free=new ArrayList<>();List<Placement> placements=new ArrayList<>();int pageCount=0;for(int number:group){PDPage page=doc.getPage(number);PDRectangle box=page.getMediaBox();float w=box.getWidth(),h=box.getHeight();
            if(!packable(page)){placements.add(new Placement(number,pageCount++,0,0));continue;}Slot chosen=null;float score=Float.MAX_VALUE;for(Slot s:free)if(w<=s.w+.01&&h<=s.h+.01){float waste=s.w*s.h-w*h;if(waste<score){score=waste;chosen=s;}}if(chosen==null){chosen=new Slot(0,0,W,H,pageCount++);free.add(chosen);}placements.add(new Placement(number,chosen.page,chosen.x,chosen.y));free.remove(chosen);float right=chosen.w-w-GAP,above=chosen.h-h-GAP;if(right>0)free.add(new Slot(chosen.x+w+GAP,chosen.y,right,h,chosen.page));if(above>0)free.add(new Slot(chosen.x,chosen.y+h+GAP,chosen.w,above,chosen.page));}return placements;}
}
