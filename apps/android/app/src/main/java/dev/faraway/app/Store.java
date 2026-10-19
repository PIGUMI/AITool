package dev.faraway.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.ArrayList;
import java.util.List;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * アプリ内の保存。サーバーのアドレス、APIトークン（Android Keystore の鍵で暗号化）、許可フォルダ。
 * トークンは平文で保存しない。許可フォルダは、利用者がアプリで選んだものだけ（初期状態は空）。
 */
final class Store {
    private static final String PREFS = "faraway";
    private static final String KEY_URL = "url";
    private static final String KEY_TOKEN = "bridge_token";
    private static final String KEY_ROOTS = "roots";
    private static final String ALIAS = "faraway_bridge_token";

    /** 許可フォルダ（SAF のツリー URI）。 */
    static final class Root {
        final String name;
        final String uri;
        final boolean write;

        Root(String name, String uri, boolean write) {
            this.name = name;
            this.uri = uri;
            this.write = write;
        }
    }

    private Store() { }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static String url(Context c) {
        return prefs(c).getString(KEY_URL, "");
    }

    static void setUrl(Context c, String url) {
        prefs(c).edit().putString(KEY_URL, url).apply();
    }

    // ---- トークン（暗号化）----
    private static SecretKey key() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (!ks.containsAlias(ALIAS)) {
            KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            kg.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build());
            return kg.generateKey();
        }
        return (SecretKey) ks.getKey(ALIAS, null);
    }

    static void setToken(Context c, String token) {
        try {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, key());
            byte[] ct = cipher.doFinal(token.getBytes(StandardCharsets.UTF_8));
            byte[] iv = cipher.getIV();
            byte[] all = new byte[iv.length + ct.length];
            System.arraycopy(iv, 0, all, 0, iv.length);
            System.arraycopy(ct, 0, all, iv.length, ct.length);
            prefs(c).edit().putString(KEY_TOKEN, Base64.encodeToString(all, Base64.NO_WRAP)).apply();
        } catch (Exception e) {
            // 暗号化できない端末では保存しない（平文では保存しない）
            prefs(c).edit().remove(KEY_TOKEN).apply();
        }
    }

    /** 保存済みのトークン。無い・復号できないときは null。 */
    static String token(Context c) {
        try {
            String b64 = prefs(c).getString(KEY_TOKEN, null);
            if (b64 == null) {
                return null;
            }
            byte[] all = Base64.decode(b64, Base64.NO_WRAP);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, all, 0, 12));
            return new String(cipher.doFinal(all, 12, all.length - 12), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return null;
        }
    }

    // ---- 許可フォルダ ----
    static List<Root> roots(Context c) {
        List<Root> out = new ArrayList<>();
        try {
            JSONArray arr = new JSONArray(prefs(c).getString(KEY_ROOTS, "[]"));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                out.add(new Root(o.getString("name"), o.getString("uri"), o.optBoolean("write", false)));
            }
        } catch (JSONException e) {
            // 壊れていたら空（何も許可しない側に倒す）
        }
        return out;
    }

    static void saveRoots(Context c, List<Root> roots) {
        JSONArray arr = new JSONArray();
        try {
            for (Root r : roots) {
                arr.put(new JSONObject().put("name", r.name).put("uri", r.uri).put("write", r.write));
            }
        } catch (JSONException e) {
            return;
        }
        prefs(c).edit().putString(KEY_ROOTS, arr.toString()).apply();
        BridgeService.rootsChanged = true;
    }
}
