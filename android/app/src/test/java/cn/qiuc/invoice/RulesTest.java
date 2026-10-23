package cn.qiuc.invoice;

import org.junit.Test;
import static org.junit.Assert.*;
import java.util.*;

public class RulesTest {
    @Test public void centsAreExactAndNeverRound() {
        assertEquals(999,Rules.cents("9.99"));assertEquals(1387220,Rules.cents("13872.20"));
        for(String bad:List.of("0","-1","9.999","NaN","1e3","","1,000","1000000000"))assertThrows(IllegalArgumentException.class,()->Rules.cents(bad));
    }
    @Test public void datesAreRealAndInvoiceDateIsNotConsumption() {
        Rules.date("2026-10-17");assertThrows(IllegalArgumentException.class,()->Rules.date("2026-02-30"));
        assertFalse(Rules.parse("开票日期：2026-10-17").containsKey("date"));
        assertEquals("2026-10-17",Rules.parse("消费日期：2026年10月17日").get("date"));
    }
    @Test public void ambiguousAndRefundedAmountsStayUnknown() {
        assertFalse(Rules.parse("实付金额：17.00\n支付金额：18.00").containsKey("amount"));
        assertFalse(Rules.parse("支付成功\n实付金额：17.00\n已退款").containsKey("amount"));
        assertEquals("17.00",Rules.parse("付款成功\n实付金额：17.00").get("amount"));
        assertFalse(Rules.parse("商品10元\n优惠2元").containsKey("amount"));
    }
    @Test public void materialRequirementsRespectProofRoles() {
        assertTrue(Rules.missing("打车",Set.of("invoice","trip")).isEmpty());
        assertEquals(List.of("行程单"),Rules.missing("打车",Set.of("invoice","payment")));
        assertTrue(Rules.missing("顺丰",Set.of("invoice","detail")).isEmpty());
        assertTrue(Rules.missing("高铁",Set.of("invoice")).isEmpty());
        assertEquals(List.of("发票"),Rules.missing("外卖",Set.of("order")));
        assertTrue(Rules.missing("材料采购",Set.of("receipt","payment")).isEmpty());
        assertFalse(Rules.missing("其他",Set.of("invoice")).isEmpty());
    }
    @Test public void invoiceFieldsNeedExplicitLabels() {
        Map<String,String> parsed=Rules.parse("电子发票\n发票号码：12345678901234567890\n价税合计（小写）：￥1353.00\n销售方名称：样本酒店");
        assertEquals("invoice",parsed.get("role"));assertEquals("1353.00",parsed.get("amount"));assertEquals("样本酒店",parsed.get("merchant"));
        assertFalse(Rules.parse("纳税人识别号：12345678901234567890").containsKey("invoice_number"));
        assertEquals("测试商店",Rules.parse("销售⽅名称：测试商店").get("merchant"));
    }
    @Test public void transactionRolesAndNegativeSpendingAreExplicit() {
        Map<String,String> fields=Rules.parse("付款成功\n实付金额：-17.00\n购物平台：淘宝闪购\n店铺：样本店（南门店）\n支付方式：支付宝\n订单编号：10000000100000000001");
        assertEquals("17.00",fields.get("amount"));assertEquals("淘宝闪购",fields.get("shopping_platform"));assertEquals("样本店(南门店)",fields.get("shop_name"));assertEquals("支付宝",fields.get("payment_channel"));assertEquals("10000000100000000001",fields.get("order_number"));
        assertFalse(Rules.parse("资金来源：建设银行卡").containsKey("payment_channel"));
    }
    @Test public void basisCannotSilentlyRemoveTaxiTripRequirement(){assertEquals(List.of("行程单"),Rules.missing("打车",Set.of("invoice","payment"),"invoice_payment"));assertTrue(Rules.missing("其他",Set.of("payment"),"payment_only").isEmpty());assertEquals(List.of("支付凭证"),Rules.missing("高铁",Set.of("invoice"),"payment_only"));}
    @Test public void categoryNeedsGoodsAndMixedPurchasesStayForReview(){assertEquals("其他",Rules.categoryFromGoods(Rules.goods("商家：滴滴出行\n支付成功")));assertEquals("材料采购",Rules.categoryFromGoods(Rules.goods("销售方：酒店有限公司\n商品明细\n螺丝\n实付17.00")));assertEquals("其他",Rules.categoryFromGoods("螺丝、盒饭"));assertEquals("顺丰",Rules.categoryFromGoods("顺丰快递费"));}
    @Test public void deliveredShoppingScreenshotUsesPaidTotalAndActualShop(){
        Map<String,String> fields=Rules.parse("订单己送达 >\n感谢你对淘宝闪购的信任，期待再次光临\n闪购 样本便利店（南湖店） >\n有线鼠标 ¥16\n打包费 ¥1\n总价 ¥20\n总优惠 ¥3 实付款 ¥17");
        assertEquals("purchase",fields.get("role"));assertEquals("17.00",fields.get("amount"));assertEquals("样本便利店(南湖店)",fields.get("merchant"));assertEquals(fields.get("merchant"),fields.get("shop_name"));assertEquals("淘宝闪购",fields.get("shopping_platform"));
    }
    @Test public void successfulBillUsesNegativeHeadlineAndLabelledDescription(){
        Map<String,String> fields=Rules.parse("账单详情\n淘宝闪购\n-17.00\n交易成功\n支付时间 2026-10-01 16:23:47\n付款方式 农业银行储蓄卡(0000)\n商品说明 样本便利店(南湖店)\n为您推荐\n其他便利店");
        assertEquals("payment",fields.get("role"));assertEquals("17.00",fields.get("amount"));assertEquals("样本便利店(南湖店)",fields.get("merchant"));assertEquals("2026-10-01",fields.get("date"));assertFalse(fields.containsKey("payment_channel"));
        fields=Rules.parse("账单详情\n淘宝闪购\n-17.00\n交易成功\n商品说明\n样本便利店(南湖店)");assertEquals("样本便利店(南湖店)",fields.get("shop_name"));
    }
    @Test public void billHeadlineDoesNotTurnIncomeRefundOrConflictingTotalsIntoAnExpense(){
        String header="账单详情\n淘宝闪购\n";
        for(String body:List.of("+17.00\n交易成功","17.00\n交易成功","-17.00\n退款成功","-17.00\n交易失败","-17.00\n待付款","-17.00\n交易成功\n支付金额：18.00"))assertFalse(body,Rules.parse(header+body).containsKey("amount"));
    }
    @Test public void aShopBrandIsNotEvidenceForTheShoppingPlatform(){
        assertFalse(Rules.parse("店铺：京东便利店（样本店）").containsKey("shopping_platform"));
        Map<String,String> fields=Rules.parse("淘宝闪购\n商品说明：京东便利店（样本店）\n账单详情\n-17.00\n交易成功");assertEquals("淘宝闪购",fields.get("shopping_platform"));assertEquals("京东便利店(样本店)",fields.get("shop_name"));
        fields=Rules.parse("账单详情\n-17.00\n交易成功\n商品说明：甲便利店\n收款方：乙便利店");assertFalse(fields.containsKey("shop_name"));assertFalse(fields.containsKey("merchant"));
    }
}
