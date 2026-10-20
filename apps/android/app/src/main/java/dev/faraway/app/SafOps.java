package dev.faraway.app;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.DocumentsContract;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * 端末のファイル操作（list / read / write）。利用者が許可したフォルダ（SAF のツリー URI）の中だけ。
 * アプリが最終的な権威: サーバーが何を要求しても、許可フォルダの外・秘匿ファイル・実行ファイルへの書き込みは拒否する。
 * Windows 版（apps/windows/Faraway.cs の FileOps）と同じ規則。
 */
final class SafOps {
    static final class OpError extends Exception {
        OpError(String message) {
            super(message);
        }
    }

    private static final int MAX_READ = 200000;
    private static final int MAX_WRITE = 500000;
    private static final int MAX_ENTRIES = 200;
    private static final Pattern SECRET_NAME = Pattern.compile(
            "(^\\.env|\\.pem$|\\.key$|\\.pfx$|\\.p12$|\\.kdbx$|\\.ppk$|^id_(rsa|ed25519|ecdsa)|^known_hosts$|^credentials?$"
                    + "|^api_tokens\\.json$|^google_(token|client)\\.json$|^login data$|^cookies$)", Pattern.CASE_INSENSITIVE);
    private static final String[] SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", "secrets", ".git"};
    private static final String[] BLOCKED_WRITE_EXT = {".exe", ".dll", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".js", ".lnk", ".msi", ".scr",
            ".com", ".reg", ".hta", ".jar", ".apk", ".dex", ".so", ".sh", ".url"};

    private SafOps() { }

    private static boolean isSecret(String name, boolean last) {
        for (String d : SECRET_DIRS) {
            if (d.equalsIgnoreCase(name)) {
                return true;
            }
        }
        return last && SECRET_NAME.matcher(name).find();
    }

    /** 許可フォルダにしてはいけない場所（ストレージ全体・認証情報のフォルダ）。理由を返す。問題なければ null。 */
    static String forbiddenRoot(Uri tree) {
        String id = DocumentsContract.getTreeDocumentId(tree);
        int colon = id.indexOf(':');
        String path = colon >= 0 ? id.substring(colon + 1) : "";
        if (path.isEmpty()) {
            return "ストレージ全体は許可できません。フォルダを絞ってください";
        }
        for (String part : path.split("/")) {
            if (isSecret(part, false)) {
                return "認証情報などのフォルダは許可できません（" + part + "）";
            }
            if (part.equalsIgnoreCase("data") && path.toLowerCase(Locale.ROOT).startsWith("android/")) {
                return "他のアプリのデータ領域は許可できません";
            }
        }
        return null;
    }

    private static String[] splitPath(String rel) throws OpError {
        String r = rel == null ? "" : rel.replace('\\', '/').trim();
        if (r.startsWith("/") || r.indexOf(':') >= 0 || r.indexOf('\0') >= 0) {
            throw new OpError("ルートからの相対パスで指定してください");
        }
        List<String> parts = new ArrayList<>();
        for (String p : r.split("/")) {
            if (p.isEmpty() || p.equals(".")) {
                continue;
            }
            if (p.equals("..")) {
                throw new OpError("親フォルダへは移動できません");
            }
            parts.add(p);
        }
        for (int i = 0; i < parts.size(); i++) {
            if (isSecret(parts.get(i), i == parts.size() - 1)) {
                throw new OpError("認証情報・秘匿ファイルのパスは扱えません");
            }
        }
        return parts.toArray(new String[0]);
    }

    private static final class Child {
        final String docId;
        final String name;
        final boolean dir;
        final long size;
        final long modified;

        Child(String docId, String name, boolean dir, long size, long modified) {
            this.docId = docId;
            this.name = name;
            this.dir = dir;
            this.size = size;
            this.modified = modified;
        }
    }

    private static List<Child> children(ContentResolver cr, Uri tree, String parentDocId) {
        List<Child> out = new ArrayList<>();
        Uri uri = DocumentsContract.buildChildDocumentsUriUsingTree(tree, parentDocId);
        String[] cols = {DocumentsContract.Document.COLUMN_DOCUMENT_ID, DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE, DocumentsContract.Document.COLUMN_SIZE, DocumentsContract.Document.COLUMN_LAST_MODIFIED};
        try (Cursor c = cr.query(uri, cols, null, null, null)) {
            while (c != null && c.moveToNext()) {
                boolean dir = DocumentsContract.Document.MIME_TYPE_DIR.equals(c.getString(2));
                out.add(new Child(c.getString(0), c.getString(1), dir, c.isNull(3) ? 0 : c.getLong(3), c.isNull(4) ? 0 : c.getLong(4)));
            }
        }
        return out;
    }

    private static Child find(ContentResolver cr, Uri tree, String parentDocId, String name) {
        for (Child ch : children(cr, tree, parentDocId)) {
            if (ch.name.equals(name)) {
                return ch;
            }
        }
        return null;
    }

    /** 実行。成功なら JSON を返し、失敗は OpError（メッセージはそのまま見せてよい）。 */
    static JSONObject execute(Context ctx, String op, JSONObject args, List<Store.Root> roots) throws OpError, JSONException, IOException {
        String rootName = args.optString("root", "");
        Store.Root root = null;
        for (Store.Root r : roots) {
            if (r.name.equalsIgnoreCase(rootName)) {
                root = r;
            }
        }
        if (root == null) {
            throw new OpError("許可されていないフォルダです: " + rootName);
        }
        Uri tree = Uri.parse(root.uri);
        ContentResolver cr = ctx.getContentResolver();
        String rel = args.optString("path", "");
        switch (op) {
            case "list":
                return list(cr, tree, root, rel);
            case "read":
                return read(cr, tree, root, rel, args.optInt("max_bytes", MAX_READ));
            case "write":
                return write(cr, tree, root, rel, args.optString("content", ""), args.optString("mode", ""));
            default:
                throw new OpError("未対応の操作です");
        }
    }

    private static Child walk(ContentResolver cr, Uri tree, String[] parts) throws OpError {
        String docId = DocumentsContract.getTreeDocumentId(tree);
        Child cur = new Child(docId, "", true, 0, 0);
        for (String p : parts) {
            Child next = find(cr, tree, cur.docId, p);
            if (next == null) {
                throw new OpError("見つかりません: " + p);
            }
            cur = next;
        }
        return cur;
    }

    private static JSONObject list(ContentResolver cr, Uri tree, Store.Root root, String rel) throws OpError, JSONException {
        Child dir = walk(cr, tree, splitPath(rel));
        if (!dir.dir) {
            throw new OpError("フォルダではありません");
        }
        JSONArray items = new JSONArray();
        boolean truncated = false;
        for (Child ch : children(cr, tree, dir.docId)) {
            if (isSecret(ch.name, true)) {
                continue;
            }
            if (items.length() >= MAX_ENTRIES) {
                truncated = true;
                break;
            }
            JSONObject o = new JSONObject().put("name", ch.name).put("type", ch.dir ? "dir" : "file");
            if (!ch.dir) {
                o.put("size", ch.size);
            }
            o.put("modified", ch.modified);
            items.put(o);
        }
        return new JSONObject().put("root", root.name).put("path", rel).put("entries", items).put("truncated", truncated);
    }

    private static JSONObject read(ContentResolver cr, Uri tree, Store.Root root, String rel, int max) throws OpError, JSONException, IOException {
        Child f = walk(cr, tree, splitPath(rel));
        if (f.dir) {
            throw new OpError("ファイルではありません");
        }
        int limit = (max <= 0 || max > MAX_READ) ? MAX_READ : max;
        Uri doc = DocumentsContract.buildDocumentUriUsingTree(tree, f.docId);
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        boolean truncated = false;
        try (InputStream in = cr.openInputStream(doc)) {
            if (in == null) {
                throw new OpError("開けませんでした");
            }
            byte[] buf = new byte[8192];
            int k;
            while ((k = in.read(buf)) > 0) {
                if (bos.size() + k > limit) {
                    bos.write(buf, 0, limit - bos.size());
                    truncated = true;
                    break;
                }
                bos.write(buf, 0, k);
            }
        }
        byte[] data = bos.toByteArray();
        for (int i = 0; i < Math.min(data.length, 8192); i++) {
            if (data[i] == 0) {
                throw new OpError("バイナリファイルは読めません");
            }
        }
        int off = (data.length >= 3 && (data[0] & 0xFF) == 0xEF && (data[1] & 0xFF) == 0xBB && (data[2] & 0xFF) == 0xBF) ? 3 : 0;
        return new JSONObject().put("root", root.name).put("path", rel).put("size", f.size).put("truncated", truncated)
                .put("text", new String(data, off, data.length - off, StandardCharsets.UTF_8));
    }

    private static JSONObject write(ContentResolver cr, Uri tree, Store.Root root, String rel, String content, String mode)
            throws OpError, JSONException, IOException {
        if (!root.write) {
            throw new OpError("このフォルダは読み取り専用です（アプリの設定で「読み書き」を許可すると書き込めます）");
        }
        if (!mode.equals("create") && !mode.equals("overwrite") && !mode.equals("append")) {
            throw new OpError("mode は create / overwrite / append のいずれかです");
        }
        byte[] data = content.getBytes(StandardCharsets.UTF_8);
        if (data.length > MAX_WRITE) {
            throw new OpError("書き込めるのは約 " + MAX_WRITE + " バイトまでです");
        }
        String[] parts = splitPath(rel);
        if (parts.length == 0) {
            throw new OpError("ファイル名を指定してください");
        }
        String fileName = parts[parts.length - 1].toLowerCase(Locale.ROOT);
        for (String ext : BLOCKED_WRITE_EXT) {
            if (fileName.endsWith(ext)) {
                throw new OpError("実行ファイル・スクリプト・ショートカットは書き込めません（" + ext + "）");
            }
        }
        // 親フォルダまでたどる（無ければ、許可フォルダの中だけで作る）
        String parentId = DocumentsContract.getTreeDocumentId(tree);
        for (int i = 0; i < parts.length - 1; i++) {
            Child next = find(cr, tree, parentId, parts[i]);
            if (next == null) {
                Uri created = DocumentsContract.createDocument(cr, DocumentsContract.buildDocumentUriUsingTree(tree, parentId),
                        DocumentsContract.Document.MIME_TYPE_DIR, parts[i]);
                if (created == null) {
                    throw new OpError("フォルダを作れませんでした: " + parts[i]);
                }
                parentId = DocumentsContract.getDocumentId(created);
            } else if (!next.dir) {
                throw new OpError("途中にファイルがあります: " + parts[i]);
            } else {
                parentId = next.docId;
            }
        }
        String name = parts[parts.length - 1];
        Child existing = find(cr, tree, parentId, name);
        Uri target;
        if (existing != null && existing.dir) {
            throw new OpError("同じ名前のフォルダがあります");
        }
        if (mode.equals("create")) {
            if (existing != null) {
                throw new OpError("既に存在します（上書きするなら mode=overwrite）");
            }
            target = DocumentsContract.createDocument(cr, DocumentsContract.buildDocumentUriUsingTree(tree, parentId),
                    "application/octet-stream", name);  // text/plain だと拡張子が付くことがあるため
        } else if (existing == null) {
            target = DocumentsContract.createDocument(cr, DocumentsContract.buildDocumentUriUsingTree(tree, parentId), "application/octet-stream", name);
        } else {
            target = DocumentsContract.buildDocumentUriUsingTree(tree, existing.docId);
        }
        if (target == null) {
            throw new OpError("ファイルを作れませんでした");
        }
        try (OutputStream out = cr.openOutputStream(target, mode.equals("append") ? "wa" : "wt")) {
            if (out == null) {
                throw new OpError("書き込みを開けませんでした");
            }
            out.write(data);
        }
        return new JSONObject().put("root", root.name).put("path", rel).put("bytes", data.length).put("mode", mode);
    }
}
