package cn.qiuc.invoice;

import java.net.URI;
import java.net.URLDecoder;
import java.time.LocalDate;
import java.util.*;

/** Rules shared by the inbox and its tests; no Android or network dependencies. */
public final class ReceiptRules {
    public record Pairing(String code,List<String> urls) {}
    public record Project(String id,String start,String end,String main,List<String> people) {}

    public static String address(String input) {
        try {
            URI u=URI.create(input.trim());String host=u.getHost();
            if(!"http".equals(u.getScheme())||host==null||u.getPort()!=8769||u.getUserInfo()!=null||u.getQuery()!=null||u.getFragment()!=null||!Set.of("","/").contains(u.getPath()))throw new IllegalArgumentException();
            String[] parts=host.split("\\.");if(parts.length!=4)throw new IllegalArgumentException();
            int[] n=new int[4];for(int i=0;i<4;i++){if(!parts[i].matches("0|[1-9][0-9]{0,2}"))throw new IllegalArgumentException();n[i]=Integer.parseInt(parts[i]);if(n[i]>255)throw new IllegalArgumentException();}
            if(!(n[0]==10||n[0]==127||n[0]==192&&n[1]==168||n[0]==172&&n[1]>=16&&n[1]<=31))throw new IllegalArgumentException();
            return "http://"+host+":8769";
        } catch(RuntimeException e){throw new IllegalArgumentException("电脑地址须为局域网 IPv4 的 http://地址:8769");}
    }
    public static Pairing pairing(String text) {
        if(!text.startsWith("invoicepair:1?"))throw new IllegalArgumentException("不是收票 App 的配对二维码");
        String code="";List<String> urls=new ArrayList<>();
        for(String field:text.substring(14).split("&")){
            String[] kv=field.split("=",2);if(kv.length!=2)throw new IllegalArgumentException("配对二维码格式不完整");
            String value;try{value=URLDecoder.decode(kv[1],"UTF-8");}catch(java.io.UnsupportedEncodingException e){throw new IllegalStateException(e);}
            if(kv[0].equals("code")){if(!code.isEmpty())throw new IllegalArgumentException("配对码重复");code=value;}
            else if(kv[0].equals("url")){String url=address(value);if(!urls.contains(url))urls.add(url);}
        }
        if(!code.matches("[0-9]{8}")||urls.isEmpty()||urls.size()>8)throw new IllegalArgumentException("需要8位配对码和电脑地址");
        return new Pairing(code,List.copyOf(urls));
    }
    public static String project(List<Project> projects,String day,String last) {
        List<Project> matches=new ArrayList<>();LocalDate date=LocalDate.parse(day);
        for(Project p:projects)try{if(!p.start.isEmpty()&&!date.isBefore(LocalDate.parse(p.start))&&(p.end.isEmpty()||!date.isAfter(LocalDate.parse(p.end))))matches.add(p);}catch(RuntimeException ignored){}
        if(matches.size()==1)return matches.get(0).id;
        for(Project p:projects)if(p.id.equals(last))return last;
        return projects.isEmpty()?"":projects.get(0).id;
    }
    public static String category(String recognized,String last,List<String> categories) {
        if(!recognized.equals("其他")&&categories.contains(recognized))return recognized;
        return categories.contains(last)?last:"其他";
    }
    public static String person(Project project,String last) {
        if(project==null)return "";
        return project.people.contains(project.main)?project.main:project.people.contains(last)?last:"";
    }
    public static String outcome(int status) {
        if(status==200)return "received";
        if(status==409)return "reselect";
        if(status==401)return "unpaired";
        return status==0?"pending":"failed";
    }
    public static long delay(int attempts){return Math.min(6*60*60_000L,30_000L*(1L<<Math.min(10,Math.max(0,attempts-1))));}
    private ReceiptRules(){}
}
