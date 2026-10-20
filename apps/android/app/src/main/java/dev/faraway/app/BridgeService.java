package dev.faraway.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 端末ブリッジ（常駐サービス）。サーバーへ「ロングポーリング」で接続し（端末→サーバーの発信のみ。待ち受けポートは無い）、
 * サーバーが出したファイル操作（list / read / write）を、許可フォルダの中だけで実行して結果を返す。
 * プロトコルは Windows 版（apps/windows/Faraway.cs の Bridge）と同じ。実パスは送らない。内容は記録しない。
 */
public class BridgeService extends Service {
    /** 許可フォルダが変わったので、サーバーへ申告し直す。 */
    static volatile boolean rootsChanged = true;
    static volatile String status = "停止中";

    private static final String CHANNEL = "faraway_bridge";
    private static final int NOTIF_ID = 1;
    private static final int NOTIF_WRITE = 2;

    private volatile boolean stop;
    private Thread thread;

    /** HTTP の失敗（ステータスコードつき）。 */
    private static final class HttpFailure extends IOException {
        final int code;

        HttpFailure(int code) {
            super("HTTP " + code);
            this.code = code;
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        createChannel();
        Notification n = notification("起動中");
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIF_ID, n);
        }
        if (thread == null || !thread.isAlive()) {
            stop = false;
            thread = new Thread(new Runnable() {
                @Override
                public void run() {
                    loop();
                }
            }, "faraway-bridge");
            thread.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stop = true;
        if (thread != null) {
            thread.interrupt();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ---- 通知 ----
    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Faraway ファイルアクセス", NotificationManager.IMPORTANCE_LOW);
            getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }
    }

    private Notification notification(String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        return b.setContentTitle("Faraway").setContentText(text).setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true).setContentIntent(open).build();
    }

    private void setStatus(String s) {
        if (s.equals(status)) {
            return;
        }
        status = s;
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) {
            nm.notify(NOTIF_ID, notification(s));
        }
    }

    // ---- ブリッジ本体 ----
    private void loop() {
        long lastHello = 0;
        while (!stop) {
            try {
                String base = Store.url(this);
                String token = Store.token(this);
                if (base.isEmpty()) {
                    setStatus("接続先が未設定");
                    Thread.sleep(3000);
                    continue;
                }
                if (token == null) {
                    setStatus("ログイン情報がありません（ペアリングが必要）");
                    Thread.sleep(3000);
                    continue;
                }
                List<Store.Root> roots = Store.roots(this);
                long now = System.currentTimeMillis();
                if (rootsChanged || now - lastHello > 5 * 60 * 1000L) {
                    hello(base, token, roots);
                    rootsChanged = false;
                    lastHello = now;
                }
                String text = http("GET", base + "api/bridge/next?wait=25", token, null, 40000);
                setStatus("接続中（許可フォルダ " + roots.size() + " 件）");
                JSONObject request = new JSONObject(text).optJSONObject("request");
                if (request != null) {
                    handle(base, token, request, roots);
                }
            } catch (SocketTimeoutException e) {
                // ロングポーリングの時間切れは正常。そのまま次へ
            } catch (InterruptedException e) {
                return;
            } catch (HttpFailure e) {
                setStatus(e.code == 401 ? "ログイン情報が無効です（ペアリングし直してください）" : "サーバーに接続できません");
                rootsChanged = true;
                sleepQuietly(5000);
            } catch (Exception e) {
                setStatus("サーバーに接続できません");
                rootsChanged = true;
                sleepQuietly(5000);
            }
        }
    }

    private static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }

    private void hello(String base, String token, List<Store.Root> roots) throws Exception {
        JSONArray rs = new JSONArray();
        for (Store.Root r : roots) {
            rs.put(new JSONObject().put("name", r.name).put("mode", r.write ? "readwrite" : "read"));  // URI（実パス）は送らない
        }
        JSONObject body = new JSONObject().put("roots", rs).put("platform", "android").put("version", "0.1.0");
        http("POST", base + "api/bridge/hello", token, body.toString(), 10000);
    }

    private void handle(String base, String token, JSONObject request, List<Store.Root> roots) throws Exception {
        String id = request.getString("id");
        String op = request.getString("op");
        JSONObject args = request.optJSONObject("args") == null ? new JSONObject() : request.getJSONObject("args");
        String rootName = args.optString("root", "");
        String path = args.optString("path", "");
        JSONObject reply = new JSONObject().put("id", id);
        try {
            JSONObject data = SafOps.execute(this, op, args, roots);
            reply.put("ok", true).put("data", data);
            log(op, rootName, path, "ok");
            if (op.equals("write")) {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (nm != null) {
                    nm.notify(NOTIF_WRITE, notification("ファイルに書き込みました: " + rootName + ":" + path));
                }
            }
        } catch (SafOps.OpError e) {
            reply.put("ok", false).put("error", e.getMessage());
            log(op, rootName, path, "拒否: " + e.getMessage());
        } catch (Exception e) {
            reply.put("ok", false).put("error", "端末側でエラーが発生しました（" + e.getClass().getSimpleName() + "）");
            log(op, rootName, path, "エラー: " + e.getClass().getSimpleName());
        }
        http("POST", base + "api/bridge/result", token, reply.toString(), 15000);
    }

    /** 端末側の記録（内容は書かない）。 */
    private void log(String op, String root, String path, String result) {
        try {
            File f = new File(getFilesDir(), "access.log");
            if (f.length() > 200000) {
                f.delete();  // 簡易ローテーション
            }
            String line = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date()) + "\t" + op + "\t" + root + "\t" + path + "\t" + result + "\n";
            try (FileOutputStream out = new FileOutputStream(f, true)) {
                out.write(line.getBytes(StandardCharsets.UTF_8));
            }
        } catch (IOException ignored) {
            // 記録に失敗しても操作は続ける
        }
    }

    private static String http(String method, String url, String token, String body, int timeoutMs) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        try {
            c.setRequestMethod(method);
            c.setConnectTimeout(10000);
            c.setReadTimeout(timeoutMs);
            c.setRequestProperty("Authorization", "Bearer " + token);
            if (body != null) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                byte[] b = body.getBytes(StandardCharsets.UTF_8);
                c.setFixedLengthStreamingMode(b.length);
                try (OutputStream out = c.getOutputStream()) {
                    out.write(b);
                }
            }
            int code = c.getResponseCode();
            if (code < 200 || code >= 300) {
                throw new HttpFailure(code);
            }
            StringBuilder sb = new StringBuilder();
            try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = r.readLine()) != null) {
                    sb.append(line);
                }
            }
            return sb.toString();
        } finally {
            c.disconnect();
        }
    }
}
