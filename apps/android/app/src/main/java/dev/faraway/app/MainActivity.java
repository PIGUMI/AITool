package dev.faraway.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

/**
 * Faraway（常駐 AI アシスタント）の Android アプリ。
 *
 * Faraway サーバーの画面（チャット・管理）を WebView で表示するだけの薄いアプリ。
 * - サーバーのアドレスは端末内（SharedPreferences）に保存する。初回だけ入力を求める。
 * - サーバーが表示する「開くだけで設定される URL（…/#token=…）」を入力すると、トークンが自動で設定される。
 *   トークンは WebView の localStorage にだけ保存され、アプリの設定には書かない。
 * - 設定したサーバー以外のページは、WebView ではなく外部ブラウザで開く。
 */
public class MainActivity extends Activity {
    private static final String PREFS = "faraway";
    private static final String KEY_URL = "url";
    private static final int MENU_RELOAD = 1;
    private static final int MENU_ADMIN = 2;
    private static final int MENU_CHAT = 3;
    private static final int MENU_SETTINGS = 4;

    private WebView web;
    private String baseUrl = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // トークンの保存（localStorage）に必要
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

        baseUrl = getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_URL, "");
        if (baseUrl.isEmpty()) {
            askServer(true);
        } else {
            web.loadUrl(baseUrl);
        }
    }

    /** 入力を「スキーム://ホスト:ポート/」に整える。# 以降はペアリング用として別に返す（保存しない）。 */
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
        String base = u.getScheme() + "://" + u.getAuthority() + "/";
        return new String[] { base, frag };
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
                .setMessage("サーバーが表示した「開くだけで設定される URL」（…/#token=…）でも入力できます。")
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
                        getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_URL, baseUrl).apply();  // アドレスだけ保存
                        // トークン付き（ペアリング）なら、初回の読み込みにだけ付ける
                        web.loadUrl(parts[1].startsWith("token=") ? baseUrl + "#" + parts[1] : baseUrl);
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

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, MENU_RELOAD, 0, "再読み込み");
        menu.add(0, MENU_CHAT, 1, "チャット");
        menu.add(0, MENU_ADMIN, 2, "管理画面");
        menu.add(0, MENU_SETTINGS, 3, "サーバー設定");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case MENU_RELOAD:
                web.loadUrl(baseUrl);
                return true;
            case MENU_CHAT:
                web.loadUrl(baseUrl);
                return true;
            case MENU_ADMIN:
                web.loadUrl(baseUrl + "admin");
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
