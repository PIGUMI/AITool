package dev.faraway.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.Toast;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

/**
 * Faraway（常駐 AI アシスタント）の Android アプリ。
 *
 * 1) 画面: Faraway サーバーの画面（チャット・管理）を WebView で表示する。設定したサーバー以外のページは外部ブラウザで開く。
 * 2) 端末ブリッジ（ブラウザ版には無い機能）: 利用者が許可したフォルダの中のファイルを、サーバーの Faraway から list / read / write できる。
 *    - 許可フォルダは利用者が選ぶ（Storage Access Framework）。初期状態では何も許可しない。フォルダごとに「読み取りのみ」か「読み書き」。
 *    - 常駐サービス（BridgeService）が、サーバーへ発信する形で動く。この端末で待ち受けるポートは無い。
 * サーバーのアドレスは端末内に保存する。APIトークンは Android Keystore の鍵で暗号化して保存する（WebView 側にも画面用に保存される）。
 */
public class MainActivity extends Activity {
    private static final int REQ_PICK_TREE = 100;
    private static final int REQ_NOTIFICATIONS = 101;
    private static final int MENU_RELOAD = 1;
    private static final int MENU_ADMIN = 2;
    private static final int MENU_CHAT = 3;
    private static final int MENU_SETTINGS = 4;
    private static final int MENU_FILES = 5;
    private static final int MENU_LOG = 6;

    private WebView web;
    private String baseUrl = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // 画面用のトークン保存（localStorage）に必要
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                if (isSameServer(u)) {
                    return false;               // サーバー内の移動は WebView で開く
                }
                startActivity(new Intent(Intent.ACTION_VIEW, u));  // それ以外は外部ブラウザ
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    String html = "<html><body style='font-family:sans-serif;background:#0f1419;color:#e6edf3;padding:24px'>"
                            + "<h3>Faraway に接続できません</h3>"
                            + "<p>サーバーが起動しているか、同じ Wi-Fi または Tailscale に繋がっているかを確認してください。</p>"
                            + "<p>右上のメニューから「再読み込み」または「サーバー設定」を選べます。</p></body></html>";
                    view.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
                }
            }
        });

        baseUrl = Store.url(this);
        if (baseUrl.isEmpty()) {
            askServer(true);
        } else {
            web.loadUrl(baseUrl);
            startBridge();
        }
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFICATIONS);  // 常駐の通知を表示するため
        }
    }

    private void startBridge() {
        if (Store.token(this) == null) {
            return;  // ペアリング（トークン付き URL の入力）がまだ。サービスは動かさない
        }
        Intent i = new Intent(this, BridgeService.class);
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(i);
        } else {
            startService(i);
        }
    }

    /** 入力を「スキーム://ホスト:ポート/」に整える。# 以降（ペアリング用 token=…）は別に返す（アドレスには保存しない）。 */
    private static String[] normalize(String input) {
        String s = input.trim();
        String frag = "";
        int h = s.indexOf('#');
        if (h >= 0) {
            frag = s.substring(h + 1);
            s = s.substring(0, h);
        }
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            s = "http://" + s;
        }
        Uri u = Uri.parse(s);
        return new String[] {u.getScheme() + "://" + u.getAuthority() + "/", frag};
    }

    private boolean isSameServer(Uri u) {
        if (baseUrl.isEmpty()) {
            return false;
        }
        Uri b = Uri.parse(baseUrl);
        return b.getScheme() != null && b.getScheme().equals(u.getScheme())
                && b.getAuthority() != null && b.getAuthority().equals(u.getAuthority());
    }

    private void askServer(final boolean first) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setSingleLine(true);
        input.setText(baseUrl);
        input.setHint("http://192.168.0.13:8765/");
        new AlertDialog.Builder(this)
                .setTitle("Faraway サーバーのアドレス")
                .setMessage("サーバーが表示した「開くだけで設定される URL」（…/#token=…）を入力すると、ログインも自動で設定されます。")
                .setView(input)
                .setCancelable(!first)
                .setPositiveButton("接続", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        String text = input.getText().toString().trim();
                        if (text.isEmpty()) {
                            if (first) {
                                finish();
                            }
                            return;
                        }
                        String[] parts = normalize(text);
                        baseUrl = parts[0];
                        Store.setUrl(MainActivity.this, baseUrl);
                        boolean paired = parts[1].startsWith("token=");
                        if (paired) {
                            Store.setToken(MainActivity.this, Uri.decode(parts[1].substring(6)));  // 端末ブリッジ用（暗号化して保存）
                            BridgeService.rootsChanged = true;
                        }
                        web.loadUrl(paired ? baseUrl + "#" + parts[1] : baseUrl);
                        startBridge();
                    }
                })
                .setNegativeButton("キャンセル", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        if (first) {
                            finish();
                        }
                    }
                })
                .show();
    }

    // ---- 許可フォルダの管理 ----
    private void showRoots() {
        final List<Store.Root> roots = Store.roots(this);
        List<String> labels = new ArrayList<>();
        for (Store.Root r : roots) {
            labels.add(r.name + "（" + (r.write ? "読み書き" : "読み取りのみ") + "）");
        }
        AlertDialog.Builder b = new AlertDialog.Builder(this).setTitle("ファイルアクセス（許可フォルダ）")
                .setMessage(roots.isEmpty() ? "Faraway が読み書きできるフォルダは、まだありません。「追加」で選んでください。\n許可していない場所には触れません。" : null);
        if (!roots.isEmpty()) {
            b.setItems(labels.toArray(new String[0]), new DialogInterface.OnClickListener() {
                @Override
                public void onClick(DialogInterface d, int which) {
                    editRoot(roots, which);
                }
            });
        }
        b.setPositiveButton("追加…", new DialogInterface.OnClickListener() {
            @Override
            public void onClick(DialogInterface d, int which) {
                Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
                i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                        | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
                startActivityForResult(i, REQ_PICK_TREE);
            }
        }).setNegativeButton("閉じる", null).show();
    }

    private void editRoot(final List<Store.Root> roots, final int index) {
        final Store.Root r = roots.get(index);
        new AlertDialog.Builder(this).setTitle(r.name)
                .setItems(new String[] {r.write ? "読み取りのみに変更" : "読み書きを許可（書き込みは都度の承認が必要）", "このフォルダの許可を取り消す"},
                        new DialogInterface.OnClickListener() {
                            @Override
                            public void onClick(DialogInterface d, int which) {
                                List<Store.Root> next = new ArrayList<>(roots);
                                if (which == 0) {
                                    next.set(index, new Store.Root(r.name, r.uri, !r.write));
                                } else {
                                    next.remove(index);
                                    try {
                                        getContentResolver().releasePersistableUriPermission(Uri.parse(r.uri),
                                                Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                                    } catch (SecurityException ignored) {
                                        // すでに取り消されている
                                    }
                                }
                                Store.saveRoots(MainActivity.this, next);
                                showRoots();
                            }
                        }).show();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK_TREE || resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        final Uri tree = data.getData();
        String why = SafOps.forbiddenRoot(tree);
        if (why != null) {
            Toast.makeText(this, why, Toast.LENGTH_LONG).show();
            return;
        }
        String id = DocumentsContract.getTreeDocumentId(tree);
        String last = id.substring(id.indexOf(':') + 1);
        final String name = last.contains("/") ? last.substring(last.lastIndexOf('/') + 1) : last;
        new AlertDialog.Builder(this).setTitle("「" + name + "」への書き込みも許可しますか？")
                .setMessage("読み書きを許可しても、書き込みは都度、サーバー側の承認画面での承認が必要です。\n「読み取りのみ」なら、書き込みは一切できません。")
                .setPositiveButton("読み書き", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        addRoot(tree, name, true);
                    }
                })
                .setNegativeButton("読み取りのみ", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        addRoot(tree, name, false);
                    }
                }).show();
    }

    private void addRoot(Uri tree, String name, boolean write) {
        int flags = Intent.FLAG_GRANT_READ_URI_PERMISSION | (write ? Intent.FLAG_GRANT_WRITE_URI_PERMISSION : 0);
        getContentResolver().takePersistableUriPermission(tree, flags);   // 読み取りのみを選んだなら、書き込み権限は保持しない
        List<Store.Root> next = new ArrayList<>(Store.roots(this));
        String unique = name;
        for (Store.Root r : next) {
            if (r.name.equalsIgnoreCase(unique)) {
                unique = name + (next.size() + 1);
            }
        }
        next.add(new Store.Root(unique, tree.toString(), write));
        Store.saveRoots(this, next);
        startBridge();
        showRoots();
    }

    private void showLog() {
        File f = new File(getFilesDir(), "access.log");
        StringBuilder sb = new StringBuilder();
        if (f.isFile()) {
            List<String> lines = new ArrayList<>();
            try (BufferedReader r = new BufferedReader(new FileReader(f))) {
                String line;
                while ((line = r.readLine()) != null) {
                    lines.add(line);
                }
            } catch (IOException ignored) {
                // 読めなければ空
            }
            for (int i = Math.max(0, lines.size() - 40); i < lines.size(); i++) {
                sb.append(lines.get(i).replace('\t', ' ')).append('\n');
            }
        }
        new AlertDialog.Builder(this).setTitle("操作の記録（直近40件・内容は記録しません）")
                .setMessage(sb.length() == 0 ? "まだ記録がありません。" : sb.toString()).setPositiveButton("閉じる", null).show();
    }

    // ---- メニュー ----
    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, MENU_RELOAD, 0, "再読み込み");
        menu.add(0, MENU_CHAT, 1, "チャット");
        menu.add(0, MENU_ADMIN, 2, "管理画面");
        menu.add(0, MENU_FILES, 3, "ファイルアクセス（許可フォルダ）");
        menu.add(0, MENU_LOG, 4, "操作の記録");
        menu.add(0, MENU_SETTINGS, 5, "サーバー設定");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case MENU_RELOAD:
            case MENU_CHAT:
                web.loadUrl(baseUrl);
                return true;
            case MENU_ADMIN:
                web.loadUrl(baseUrl + "admin");
                return true;
            case MENU_FILES:
                showRoots();
                return true;
            case MENU_LOG:
                showLog();
                return true;
            case MENU_SETTINGS:
                askServer(false);
                return true;
            default:
                return super.onOptionsItemSelected(item);
        }
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
