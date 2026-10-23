package cn.qiuc.invoice;

import java.util.*;

/** Reconstruct physical OCR rows instead of concatenating separate text columns. */
final class TextLayout {
    static final class Line {
        final String text;final int left,top,right,bottom;
        Line(String text,int left,int top,int right,int bottom){this.text=text;this.left=left;this.top=top;this.right=right;this.bottom=bottom;}
    }
    static String rows(List<Line> input){
        List<Line> lines=new ArrayList<>(input);lines.sort(Comparator.comparingInt(line->line.top));List<List<Line>> rows=new ArrayList<>();
        for(Line line:lines){List<Line> target=null;for(List<Line> row:rows){Line anchor=row.get(0);int overlap=Math.min(anchor.bottom,line.bottom)-Math.max(anchor.top,line.top);int height=Math.min(anchor.bottom-anchor.top,line.bottom-line.top);if(height>0&&overlap>height*.5){target=row;break;}}if(target==null){target=new ArrayList<>();rows.add(target);}target.add(line);}
        List<String> output=new ArrayList<>();for(List<Line> row:rows){row.sort(Comparator.comparingInt(line->line.left));List<String> words=new ArrayList<>();for(Line line:row)words.add(line.text);output.add(String.join(" ",words));}return String.join("\n",output);
    }
    private TextLayout(){}
}
