package cn.qiuc.invoice;

import android.app.job.*;
import android.content.*;
import java.util.concurrent.*;

public final class ReceiptUploadJob extends JobService {
    private final ExecutorService worker=Executors.newSingleThreadExecutor();
    private Future<?> task;
    static void schedule(Context context){JobScheduler scheduler=context.getSystemService(JobScheduler.class);if(scheduler.getPendingJob(2203)==null)scheduler.schedule(new JobInfo.Builder(2203,new ComponentName(context,ReceiptUploadJob.class)).setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setPeriodic(15*60_000L).build());}
    @Override public boolean onStartJob(JobParameters params){task=worker.submit(()->{synchronized(ReceiptInbox.SYNC_LOCK){try(ReceiptInbox inbox=new ReceiptInbox(this)){inbox.sync(false);}catch(Exception ignored){/* Durable records and next_try survive the next job. */}}jobFinished(params,false);});return true;}
    @Override public boolean onStopJob(JobParameters params){if(task!=null)task.cancel(true);return true;}
    @Override public void onDestroy(){worker.shutdown();super.onDestroy();}
}
