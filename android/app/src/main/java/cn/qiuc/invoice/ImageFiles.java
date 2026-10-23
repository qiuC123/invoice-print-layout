package cn.qiuc.invoice;

import android.graphics.*;
import androidx.exifinterface.media.ExifInterface;
import java.io.*;

/** Respect phone-camera orientation and bound decoded bitmap memory; source bytes never change. */
public final class ImageFiles {
    public static Bitmap read(File file,int maximum)throws IOException{
        BitmapFactory.Options options=new BitmapFactory.Options();options.inJustDecodeBounds=true;BitmapFactory.decodeFile(file.getAbsolutePath(),options);if(options.outWidth<=0||options.outHeight<=0)throw new IOException("图片不可读取");options.inSampleSize=1;while(Math.max(options.outWidth,options.outHeight)/options.inSampleSize>maximum)options.inSampleSize*=2;options.inJustDecodeBounds=false;Bitmap source=BitmapFactory.decodeFile(file.getAbsolutePath(),options);if(source==null)throw new IOException("图片不可读取");int orientation=1;try{orientation=new ExifInterface(file.getAbsolutePath()).getAttributeInt(ExifInterface.TAG_ORIENTATION,1);}catch(IOException ignored){}Matrix transform=new Matrix();switch(orientation){case 2:transform.setScale(-1,1);break;case 3:transform.setRotate(180);break;case 4:transform.setScale(1,-1);break;case 5:transform.setRotate(90);transform.postScale(-1,1);break;case 6:transform.setRotate(90);break;case 7:transform.setRotate(270);transform.postScale(-1,1);break;case 8:transform.setRotate(270);break;default:return source;}Bitmap rotated=Bitmap.createBitmap(source,0,0,source.getWidth(),source.getHeight(),transform,true);if(rotated!=source)source.recycle();return rotated;
    }
    private ImageFiles(){}
}
