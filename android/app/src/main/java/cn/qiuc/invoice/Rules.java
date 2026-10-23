package cn.qiuc.invoice;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.*;
import java.util.regex.*;

/** Pure rules: cents, explicit text evidence, material requirements; no network. */
public final class Rules {
    public static final List<String> CATEGORIES = List.of("材料采购","酒店","高铁","外卖","餐饮","打车","顺丰","其他");
    public static long cents(String input) {
        if (input == null || !input.matches("[0-9]{1,9}(\\.[0-9]{1,2})?")) throw new IllegalArgumentException("金额须为正数，最多两位小数");
        long value = new BigDecimal(input).movePointRight(2).longValueExact();
        if (value <= 0) throw new IllegalArgumentException("请填写大于零的实际费用金额");
        return value;
    }
    public static String money(long cents) { return BigDecimal.valueOf(cents,2).toPlainString(); }
    public static void date(String value) {
        if (value == null || !value.matches("\\d{4}-\\d{2}-\\d{2}")) throw new IllegalArgumentException("日期请填写 YYYY-MM-DD");
        try { LocalDate.parse(value); } catch (RuntimeException e) { throw new IllegalArgumentException("消费日期无效"); }
    }
    public static List<String> missing(String category, Set<String> roles) {
        List<String> out = new ArrayList<>();
        switch(category) {
            case "打车": need(out,roles,"invoice","发票"); need(out,roles,"trip","行程单"); break;
            case "顺丰": need(out,roles,"invoice","发票"); need(out,roles,"detail","运单明细"); break;
            case "酒店": case "高铁": need(out,roles,"invoice","发票"); break;
            case "材料采购":
                if (!roles.contains("purchase") && !roles.contains("receipt")) out.add("购买明细／收据");
                if (!roles.contains("invoice") && !roles.contains("payment")) out.add("发票或付款凭证");
                break;
            case "餐饮":
                need(out,roles,"invoice","发票");
                if (!roles.contains("order") && !roles.contains("purchase") && !roles.contains("receipt")) out.add("订单记录或购买明细／收据");
                break;
            case "外卖":
                need(out,roles,"invoice","发票");need(out,roles,"order","订单记录");
                break;
            default: out.add("此类别的材料要求待人工确认");
        }
        return out;
    }
    private static void need(List<String> out, Set<String> roles, String role, String title) { if (!roles.contains(role)) out.add(title); }
    public static List<String> missing(String category,Set<String> roles,String basis){
        if(basis.equals("payment_only"))return roles.contains("payment")?List.of():List.of("支付凭证");
        if(category.equals("打车")||category.equals("顺丰"))return missing(category,roles);
        if(basis.equals("invoice_payment")){List<String> out=new ArrayList<>();if(!roles.contains("invoice")&&!roles.contains("receipt"))out.add("发票或收据");if(!roles.contains("payment"))out.add("付款凭证");return out;}
        return missing(category,roles);
    }
    public static String categoryFromGoods(String text){Set<String> matched=new HashSet<>();String[][] groups={{"材料采购","口罩","胶带","螺丝","电线","电缆","插座","垃圾袋","手套","文具","纸巾"},{"酒店","住宿费","客房费","房费"},{"高铁","铁路客运","高铁车票","动车车票","铁路电子客票"},{"外卖","奶茶","咖啡饮品","盒饭","外卖餐费"},{"餐饮","店内餐饮","途中餐饮","堂食餐费"},{"打车","网约车客运","出租汽车客运","出租车费"},{"顺丰","顺丰快递费","顺丰速运费"}};for(String[] group:groups)for(int i=1;i<group.length;i++)if(text.contains(group[i]))matched.add(group[0]);return matched.size()==1?matched.iterator().next():"其他";}
    public static String goods(String text){List<String> lines=new ArrayList<>();boolean section=false;for(String raw:text.split("\\n")){String line=raw.trim();if(line.matches("^[*＊].+")){lines.add(line);continue;}if(line.matches("^(商品明细|购买明细|商品[:：]|服务[:：]).*")){section=true;lines.add(line);continue;}if(line.matches("^(实付|合计|收货|订单号|支付|地址|电话|联系|开票|销售方|购买方).*"))section=false;if(section)lines.add(line);}return String.join("\n",lines);}
    public static Map<String,String> parse(String text) {
        Map<String,String> r=new HashMap<>(); String clean=java.text.Normalizer.normalize(text,java.text.Normalizer.Form.NFKC).replaceAll("[ \\t]", "");
        boolean shopping=Pattern.compile("订单[已己](?:送达|完成)|订单详情").matcher(clean).find();
        boolean bill=clean.contains("账单详")||(clean.contains("支付时间")&&clean.contains("付款方式"));
        boolean success=clean.contains("支付成功")||clean.contains("付款成功")||clean.contains("转账成功")||clean.contains("交易成功");
        String role="other";
        if (clean.contains("发票号码") && (clean.contains("价税合计") || clean.contains("电子发票"))) role="invoice";
        else if (clean.contains("行程单")) role="trip";
        else if (clean.contains("运单明细") || clean.contains("运单号")) role="detail";
        else if (shopping&&Pattern.compile("实付款|实付金额|实际付款|实付").matcher(clean).find()) role="purchase";
        else if (success) role="payment";
        else if (clean.contains("商品明细") || clean.contains("购买明细") || clean.contains("收据")) role="receipt";
        r.put("role", role);
        String prefix=role.equals("invoice") ? "(?:价税合计(?:[（(]小写[）)])?|[（(]小写[）)])" : "(?:实付款|实付金额|实际付款|实付|付款金额|支付金额|合计金额|收据合计)";
        Matcher m=Pattern.compile(prefix+"[:：]?\\s*[¥￥]?\\s*[-−]?\\s*([0-9]+(?:\\.[0-9]{1,2})?)(?![0-9.])").matcher(clean);
        Set<String> amounts=new HashSet<>(); while(m.find()) { try { amounts.add(money(cents(m.group(1)))); } catch(IllegalArgumentException ignored) {} }
        if(role.equals("payment")&&success&&bill){m=Pattern.compile("(?m)^[¥￥]?[-−]([0-9]+(?:\\.[0-9]{1,2})?)$").matcher(clean);while(m.find())try{amounts.add(money(cents(m.group(1))));}catch(IllegalArgumentException ignored){}}
        if (amounts.size()==1) r.put("amount",amounts.iterator().next());
        m=Pattern.compile("(?:销售方名称|销方名称|收款方|商家|店铺)[:：]\\s*([^\\n]{2,120})").matcher(clean);
        if(m.find()) r.put("merchant",m.group(1).trim());
        m=Pattern.compile("发票号码[:：]?\\s*(\\d{8,30})(?!\\d)").matcher(clean);
        if(m.find())r.put("invoice_number",m.group(1));
        for(String[] field:new String[][]{{"order_number","(?:订单号|订单编号|商户订单号|交易单号)"},{"payment_channel","(?:支付渠道|支付方式)"}}){m=Pattern.compile(field[1]+"[:：]\\s*([^\\n]{1,120})").matcher(clean);if(m.find())r.put(field[0],m.group(1));}
        Set<String> platforms=new LinkedHashSet<>(labelled(clean,List.of("购物平台","下单平台","购买平台","订单平台")));
        for(String platform:PLATFORMS){for(String line:clean.split("\\n"))if(line.equals(platform))platforms.add(platform);if(clean.replaceAll("\\s","").matches("(?s).*感谢[你您]?对"+Pattern.quote(platform)+"的信任.*"))platforms.add(platform);}
        if(platforms.size()==1)r.put("shopping_platform",platforms.iterator().next());
        if(!role.equals("invoice")){
            Set<String> shops=new LinkedHashSet<>();List<String> labels=List.of("销售方名称","销方名称","收款方","实际店铺","店铺名称","商家名称","商户名称","商家","店铺");
            for(String value:labelled(clean,labels)){String shop=shopValue(value);if(!shop.isEmpty())shops.add(shop);}
            for(String value:labelled(clean,List.of("商品说明"))){String shop=shopValue(value);if(shopShaped(shop))shops.add(shop);}
            // A payment recommendation is not its counterparty. Unlabelled shop
            // headings are accepted only in the purchase/order view.
            if(shopping&&r.containsKey("shopping_platform"))for(String line:clean.split("\\n")){boolean labelled=false;for(String label:labels)labelled|=line.startsWith(label);String shop=shopValue(line);if(!labelled&&shopShaped(shop))shops.add(shop);}
            r.remove("merchant");if(shops.size()==1){String shop=shops.iterator().next();r.put("merchant",shop);r.put("shop_name",shop);}else if(shops.size()>1)r.put("warning","识别到多个店铺，请对照原件确认");
        }
        if(!r.containsKey("payment_channel")){if(clean.contains("支付宝支付")||clean.contains("支付宝账单"))r.put("payment_channel","支付宝");else if(clean.contains("微信支付"))r.put("payment_channel","微信支付");}
        m=Pattern.compile("(?:消费日期|付款日期|支付日期|订单日期|消费时间|付款时间|支付时间|下单时间)[:：]?\\s*(\\d{4})[-年/.](\\d{1,2})[-月/.](\\d{1,2})").matcher(clean);
        Set<String> dates=new LinkedHashSet<>();while(m.find()){String day=String.format(Locale.ROOT,"%s-%02d-%02d",m.group(1),Integer.parseInt(m.group(2)),Integer.parseInt(m.group(3)));try{date(day);dates.add(day);}catch(IllegalArgumentException ignored){}}if(dates.size()==1)r.put("date",dates.iterator().next());
        if (clean.matches("(?s).*(已退款|退款成功|未付款|待付款|支付失败|交易失败|交易关闭|订单已取消).*")) { r.remove("amount"); r.put("role","other"); r.put("warning","付款状态待确认"); }
        return r;
    }
    private static final List<String> PLATFORMS=List.of("淘宝闪购","淘宝","京东秒送","京东","美团外卖","美团","饿了么","拼多多","天猫");
    private static List<String> labelled(String text,List<String> labels){
        List<String> values=new ArrayList<>();String[] lines=text.split("\\n");List<String> ordered=new ArrayList<>(labels);ordered.sort(Comparator.comparingInt(String::length).reversed());
        for(int i=0;i<lines.length;i++)for(String label:ordered)if(lines[i].startsWith(label)){String value=lines[i].substring(label.length()).replaceFirst("^[:：]","").trim();if(value.isEmpty()&&i+1<lines.length)value=lines[i+1].trim();if(!value.isEmpty())values.add(value);break;}return values;
    }
    static String shopValue(String text){String value=text.replaceFirst("^(?:淘宝闪购|闪购)[:：·|]*","").replaceAll("[>›〉]+$","").trim();if(value.length()<2||value.length()>100||PLATFORMS.contains(value)||value.contains("银行")||Pattern.compile("地址|送至|收货|电话|联系|客服|订单号|发票号|自配送|[¥￥]|\\d{8,}").matcher(value).find())return "";return value;}
    static boolean shopShaped(String value){return !value.isEmpty()&&Pattern.compile("(?:店|超市|商行|餐厅|饭馆|酒楼|小吃|公司)(?:\\([^()]{1,60}\\))?$").matcher(value).find();}
    private Rules() {}
}
