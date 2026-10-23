package cn.qiuc.invoice;
import org.junit.Test;
import static org.junit.Assert.*;
import java.util.*;
public class TextLayoutTest {
    @Test public void separateLabelAndValueColumnsAreRebuiltAsPhysicalRows(){
        List<TextLayout.Line> lines=List.of(
            new TextLayout.Line("账单详情",10,0,160,30),new TextLayout.Line("淘宝闪购",200,60,400,90),
            new TextLayout.Line("支付时间",10,200,160,230),new TextLayout.Line("付款方式",10,250,160,280),new TextLayout.Line("商品说明",10,300,160,330),
            new TextLayout.Line("-17.00",210,100,390,150),new TextLayout.Line("交易成功",230,160,370,190),
            new TextLayout.Line("2026-10-01 16:23:47",200,198,600,232),new TextLayout.Line("农业银行储蓄卡(0000)",200,248,600,282),new TextLayout.Line("样本便利店(南湖店)",200,298,600,332));
        String text=TextLayout.rows(lines);assertTrue(text.contains("商品说明 样本便利店(南湖店)"));Map<String,String> fields=Rules.parse(text);
        assertEquals("payment",fields.get("role"));assertEquals("17.00",fields.get("amount"));assertEquals("2026-10-01",fields.get("date"));assertEquals("样本便利店(南湖店)",fields.get("shop_name"));
    }
    @Test public void closeRowsDoNotBorrowAnAdjacentValue(){
        List<TextLayout.Line> lines=List.of(new TextLayout.Line("甲",10,10,30,40),new TextLayout.Line("乙",10,38,30,68),new TextLayout.Line("2",50,39,70,69),new TextLayout.Line("1",50,11,70,41));
        assertEquals("甲 1\n乙 2",TextLayout.rows(lines));
    }
    @Test public void paidTotalIsKeptBesideItsLabelRatherThanMixedWithDiscount(){
        String text=TextLayout.rows(List.of(new TextLayout.Line("订单已送达",10,0,150,30),new TextLayout.Line("感谢你对淘宝闪购的信任",10,50,400,80),new TextLayout.Line("闪购样本便利店(南湖店)",10,100,400,130),new TextLayout.Line("总优惠¥3",10,200,160,230),new TextLayout.Line("实付款¥17",300,199,450,232),new TextLayout.Line("总价¥20",10,250,160,280)));
        assertEquals("17.00",Rules.parse(text).get("amount"));assertEquals("purchase",Rules.parse(text).get("role"));
    }
}
