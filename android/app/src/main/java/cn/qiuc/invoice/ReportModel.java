package cn.qiuc.invoice;

import org.json.*;
import java.util.*;

/** Same public company row contract as report_types.py / report_snapshot.py. */
public final class ReportModel {
    static final String[][] FIXED={
        {"expansion","场地费用(消费)","8","其他"},{"expansion","施工押金(可退)","9","其他"},
        {"transport","货车去程运费","10","其他"},{"transport","货车回程运费","11","其他"},
        {"transport","火车、飞机","12","高铁"},{"transport","打的(出租车、滴滴等)","13","打车"},
        {"transport","公交(地铁、公交、大巴等)","14","其他"},{"transport","货拉拉(附行程单、报销凭证)","15","其他"},
        {"living","住宿费(附清单)","16","酒店"},{"living","员工统一购餐费(附清单)","17","外卖"},
        {"living","生活补助费用(附清单)","18","其他"},{"living","现场饮用水、夜宵","19","餐饮"},
        {"personnel","工厂搭建人工数","20","其他"},{"personnel","工厂撤展人工数","21","其他"},
        {"personnel","外请人员金额(附清单)","22","其他"},{"personnel","保洁费(附清单)","23","其他"}};
    static final String[][] TYPES={{"expansion","拓展","8","9"},{"transport","车费","10","15"},{"living","生活费","16","19"},{"personnel","人员","20","23"},{"materials","现场购材料","24","28"},{"other","其他","29","34"}};
    static final Map<Integer,String> ROWS=new TreeMap<>();static{for(String[] item:FIXED)ROWS.put(Integer.parseInt(item[2]),item[1]);}
    public static JSONArray types() throws JSONException {JSONArray a=new JSONArray();for(String[] type:TYPES){JSONObject o=new JSONObject().put("code",type[0]).put("label",type[1]);JSONArray items=new JSONArray();for(String[] item:FIXED)if(item[0].equals(type[0]))items.put(new JSONObject().put("name",item[1]).put("row",Integer.parseInt(item[2])).put("category",item[3]));a.put(o.put("items",items));}return a;}
    public static JSONObject choice(String type,String name,String category) throws JSONException {
        name=name.trim();if(name.isEmpty()||name.length()>200)throw new IllegalArgumentException("请填写或选择报销子项（最多200字）");
        if(type.equals("materials")||type.equals("other"))return new JSONObject().put("project_type",type).put("project_name",name).put("category",type.equals("materials")?"材料采购":category.equals("顺丰")?"顺丰":"其他");
        for(String[] item:FIXED)if(item[0].equals(type)&&item[1].equals(name))return new JSONObject().put("project_type",type).put("project_name",name).put("category",item[3]);
        throw new IllegalArgumentException("报销子项不属于当前类型");
    }
    public static JSONObject infer(JSONObject expense) throws JSONException {
        String category=expense.optString("category","其他");
        if(!expense.optString("project_type").isEmpty())return choice(expense.getString("project_type"),expense.optString("project_name"),category);
        for(String[] item:FIXED)if(!item[3].equals("其他")&&item[3].equals(category))return choice(item[0],item[1],category);
        return choice(category.equals("材料采购")?"materials":"other",expense.optString("title").isBlank()?"其他费用":expense.optString("title"),category);
    }
    public static JSONObject snapshot(List<JSONObject> input) throws JSONException {
        List<JSONObject> shares=new ArrayList<>();for(JSONObject expense:input){JSONArray allocations=expense.optJSONArray("allocations");if(allocations==null||allocations.length()==0)shares.add(expense);else{long sum=0;for(JSONObject allocation:list(allocations)){JSONObject share=new JSONObject(expense.toString());long cents=allocation.getLong("cents");sum=Math.addExact(sum,cents);share.put("source_cents",expense.getLong("cents")).put("cents",cents).put("owner",allocation.getString("name")).put("owner_id",allocation.getString("person_id"));shares.add(share);}if(sum!=expense.getLong("cents"))throw new IllegalArgumentException("分摊合计与费用金额不一致");}}
        Map<String,Integer> allocated=new LinkedHashMap<>();TreeMap<Integer,LinkedHashMap<String,List<JSONObject>>> rows=new TreeMap<>();
        for(JSONObject source:shares){JSONObject expense=new JSONObject(source.toString());JSONObject choice=infer(expense);String type=choice.getString("project_type"),name=choice.getString("project_name");expense.put("project_type",type).put("project_name",name);
            int row=0;for(String[] item:FIXED)if(item[0].equals(type)&&item[1].equals(name))row=Integer.parseInt(item[2]);
            String group=type+":"+name;
            if(row==0){int start=type.equals("materials")?24:29,end=type.equals("materials")?28:34;if(!allocated.containsKey(group)){Set<Integer> used=new HashSet<>(allocated.values());int candidate=start;while(candidate<end&&used.contains(candidate))candidate++;allocated.put(group,candidate);}row=allocated.get(group);}
            rows.computeIfAbsent(row,k->new LinkedHashMap<>()).computeIfAbsent(group,k->new ArrayList<>()).add(expense);
        }
        JSONArray ordered=new JSONArray(),groups=new JSONArray(),totals=new JSONArray();long total=0;
        for(Map.Entry<Integer,LinkedHashMap<String,List<JSONObject>>> row:rows.entrySet()){
            long subtotal=0;Set<String> names=new LinkedHashSet<>();JSONArray ids=new JSONArray();
            for(List<JSONObject> bucket:row.getValue().values()){JSONArray groupIds=new JSONArray();for(JSONObject e:bucket){e.put("report_row",row.getKey()).put("detail_row",ordered.length()+2);ordered.put(e);groupIds.put(e.getString("id"));ids.put(e.getString("id"));names.add(e.getString("project_name"));subtotal=Math.addExact(subtotal,e.getLong("cents"));}groups.put(new JSONObject().put("row",row.getKey()).put("ids",groupIds));}
            JSONArray detailRows=new JSONArray();for(int i=0;i<ordered.length();i++)if(ordered.getJSONObject(i).getInt("report_row")==row.getKey())detailRows.put(ordered.getJSONObject(i).getInt("detail_row"));
            totals.put(new JSONObject().put("row",row.getKey()).put("name",String.join("、",names)).put("cents",subtotal).put("ids",ids).put("detail_rows",detailRows));total=Math.addExact(total,subtotal);
        }
        return new JSONObject().put("items",ordered).put("groups",groups).put("rows",totals).put("total_cents",total);
    }
    public static List<JSONObject> list(JSONArray array) throws JSONException {List<JSONObject> out=new ArrayList<>();for(int i=0;i<array.length();i++)out.add(array.getJSONObject(i));return out;}
    private ReportModel(){}
}
