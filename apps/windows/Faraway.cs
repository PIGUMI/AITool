// Faraway.exe — Faraway（常駐 AI アシスタント）の Windows 用アプリ。
//
// 1) 画面: Edge（無ければ Chrome）のアプリ表示で、Faraway の画面を専用ウィンドウとして開く。
// 2) 端末ブリッジ（ブラウザ版には無い機能）: タスクトレイに常駐し、利用者が許可したフォルダ（ルート）の中のファイルを、
//    サーバーの Faraway から list / read / write できるようにする。
//    - 端末→サーバーへの発信（ロングポーリング）だけで動く。この PC で待ち受けるポートは無い。
//    - 許可フォルダは利用者がこのアプリで決める（既定は何も許可しない。読み取りのみ／読み書きをフォルダごとに選ぶ）。
//    - 許可フォルダの外・リンク（ジャンクション等）経由・認証情報のファイル・実行ファイルへの書き込みは、サーバーが何を要求しても拒否する。
//    - すべての操作を %APPDATA%\Faraway\access.log に記録する（内容は記録しない）。
// ログイン情報（APIトークン）は Windows のユーザーごとの暗号化（DPAPI）で保存する。設定ファイルには平文で書かない。
//
// 使い方:  Faraway.exe                 画面を開き、トレイに常駐してブリッジを動かす
//          Faraway.exe --admin         管理画面を開く
//          Faraway.exe --no-window     画面を開かずトレイにだけ常駐（自動起動用）
//          Faraway.exe --reset         保存した接続先・ログイン情報・許可フォルダを消す
//          Faraway.exe --check <URL>   接続確認のみ（終了コード 0 = 到達できる）
//          Faraway.exe --add-root <名前> <フォルダ> [rw]   許可フォルダを追加（rw で読み書き）
//          Faraway.exe --headless      トレイも画面も出さず、ブリッジだけを動かす（検証・サービス用）
//          Faraway.exe --self-update [--yes] [--restart]   アプリ自身の更新（サーバーが配布する最新版を、SHA-256 を照合して入れ替える）
//          Faraway.exe --pair <URL>    接続先とログイン情報だけを保存する（画面なし）
//          Faraway.exe <URL>           接続先を指定して起動（ペアリング URL …/#token=… も可）
//
// ビルド: apps\windows\build.ps1（Windows 標準の C# コンパイラ csc.exe を使う。追加のインストールは不要）
// C# 5 で書く（csc.exe v4.0.30319 のため。文字列補間・?.・=> の本体式・out var は使えない）。

using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

class OpError : Exception
{
    public OpError(string message) : base(message) { }
}

class Root
{
    public string Name;
    public string Path;
    public bool Write;
}

static class Cfg
{
    public const string Version = "0.3.0";
    public static readonly string Dir = Environment.GetEnvironmentVariable("FARAWAY_HOME") ??
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Faraway");
    public static readonly string UrlFile = Path.Combine(Dir, "url.txt");
    public static readonly string TokenFile = Path.Combine(Dir, "bridge.token");   // DPAPI で暗号化
    public static readonly string RootsFile = Path.Combine(Dir, "roots.json");
    public static readonly string LogFile = Path.Combine(Dir, "access.log");
    public static readonly string Profile = Path.Combine(Dir, "profile");
    static readonly byte[] Entropy = Encoding.UTF8.GetBytes("Faraway.bridge.v1");
    static readonly object LogLock = new object();

    public static string ReadUrl()
    {
        try { return File.Exists(UrlFile) ? File.ReadAllText(UrlFile).Trim() : ""; }
        catch (IOException) { return ""; }
    }

    public static void SaveUrl(string baseUrl)
    {
        Directory.CreateDirectory(Dir);
        File.WriteAllText(UrlFile, baseUrl);   // アドレスだけ。トークンは別ファイル（暗号化）
    }

    public static void SaveToken(string token)
    {
        Directory.CreateDirectory(Dir);
        byte[] enc = ProtectedData.Protect(Encoding.UTF8.GetBytes(token), Entropy, DataProtectionScope.CurrentUser);
        File.WriteAllBytes(TokenFile, enc);
    }

    public static string LoadToken()
    {
        try
        {
            if (!File.Exists(TokenFile)) return null;
            byte[] dec = ProtectedData.Unprotect(File.ReadAllBytes(TokenFile), Entropy, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(dec);
        }
        catch (Exception) { return null; }
    }

    public static List<Root> LoadRoots()
    {
        List<Root> list = new List<Root>();
        try
        {
            if (!File.Exists(RootsFile)) return list;
            JavaScriptSerializer js = new JavaScriptSerializer();
            IEnumerable arr = js.DeserializeObject(File.ReadAllText(RootsFile)) as IEnumerable;
            if (arr == null) return list;
            foreach (object o in arr)
            {
                Dictionary<string, object> d = o as Dictionary<string, object>;
                if (d == null || !d.ContainsKey("name") || !d.ContainsKey("path")) continue;
                Root r = new Root();
                r.Name = Convert.ToString(d["name"]);
                r.Path = Convert.ToString(d["path"]);
                r.Write = d.ContainsKey("mode") && Convert.ToString(d["mode"]) == "readwrite";
                if (r.Name.Length > 0 && Directory.Exists(r.Path)) list.Add(r);
            }
        }
        catch (Exception) { }
        return list;
    }

    public static void SaveRoots(List<Root> roots)
    {
        Directory.CreateDirectory(Dir);
        List<object> arr = new List<object>();
        foreach (Root r in roots)
        {
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["name"] = r.Name; d["path"] = r.Path; d["mode"] = r.Write ? "readwrite" : "read";
            arr.Add(d);
        }
        File.WriteAllText(RootsFile, new JavaScriptSerializer().Serialize(arr));
    }

    // 端末側の記録（内容は書かない。いつ・何を・どのフォルダの・どのパスに・結果）
    public static void Log(string op, string root, string path, string result)
    {
        try
        {
            lock (LogLock)
            {
                Directory.CreateDirectory(Dir);
                File.AppendAllText(LogFile, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "\t" + op + "\t" + root + "\t" + path + "\t" + result + "\r\n");
            }
        }
        catch (Exception) { }
    }

    public static string Normalize(string input, out string frag)
    {
        frag = null;
        string s = input.Trim();
        int h = s.IndexOf('#');
        if (h >= 0) { frag = s.Substring(h + 1); s = s.Substring(0, h); }
        if (!s.StartsWith("http://") && !s.StartsWith("https://")) s = "http://" + s;
        int q = s.IndexOf('/', s.IndexOf("://") + 3);
        return q >= 0 ? s.Substring(0, q + 1) : s + "/";   // パスは捨て、ホスト:ポートだけ残す
    }

    public static string TokenFromFragment(string frag)
    {
        if (frag != null && frag.StartsWith("token=")) return Uri.UnescapeDataString(frag.Substring(6));
        return null;
    }
}

// ---- 端末のファイル操作。許可フォルダの外・リンク経由・秘匿ファイル・実行ファイルへの書き込みを拒否する。端末側が最終的な権威。 ----
static class FileOps
{
    const int MaxRead = 200000;
    const int MaxWrite = 500000;
    const int MaxEntries = 200;
    static readonly Regex SecretName = new Regex(
        @"(^\.env|\.pem$|\.key$|\.pfx$|\.p12$|\.kdbx$|\.ppk$|^id_(rsa|ed25519|ecdsa)|^known_hosts$|^credentials?$|^api_tokens\.json$|^google_(token|client)\.json$|^login data$|^cookies$|^ntuser\.dat)",
        RegexOptions.IgnoreCase);
    static readonly string[] SecretDirs = { ".ssh", ".gnupg", ".aws", ".azure", "secrets", ".git" };
    static readonly string[] BlockedWriteExt = { ".exe", ".dll", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".lnk", ".msi", ".scr", ".com",
                                                 ".reg", ".hta", ".jar", ".cpl", ".sys", ".pif", ".url", ".settingcontent-ms" };
    static readonly string[] Reserved = { "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                                          "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9" };

    static string Trim(string p) { return p.TrimEnd('\\', '/'); }

    // 許可フォルダにしてはいけない場所（広すぎる・システム・認証情報）。理由を返す。問題なければ null。
    public static string ForbiddenRoot(string path)
    {
        string full;
        try { full = Trim(Path.GetFullPath(path)); }
        catch (Exception) { return "パスが不正です"; }
        string drive = Trim(Path.GetPathRoot(full) ?? "");
        if (string.Equals(full, drive, StringComparison.OrdinalIgnoreCase)) return "ドライブ全体は許可できません";
        string profile = Trim(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        if (string.Equals(full, profile, StringComparison.OrdinalIgnoreCase)) return "ユーザーフォルダ全体は広すぎます。Documents など、フォルダを絞ってください";
        string[] systemDirs = {
            Environment.GetEnvironmentVariable("WINDIR"), Environment.GetEnvironmentVariable("ProgramFiles"), Environment.GetEnvironmentVariable("ProgramFiles(x86)"),
            Environment.GetEnvironmentVariable("ProgramData"), Environment.GetEnvironmentVariable("APPDATA"), Environment.GetEnvironmentVariable("LOCALAPPDATA"),
            Path.Combine(profile, "AppData") };
        foreach (string d in systemDirs)
        {
            if (string.IsNullOrEmpty(d)) continue;
            string t = Trim(d);
            if (string.Equals(full, t, StringComparison.OrdinalIgnoreCase) || full.StartsWith(t + "\\", StringComparison.OrdinalIgnoreCase))
                return "システム・アプリのデータ領域は許可できません（" + t + "）";
        }
        foreach (string part in full.Split('\\'))
            foreach (string sd in SecretDirs)
                if (string.Equals(part, sd, StringComparison.OrdinalIgnoreCase)) return "認証情報・秘匿ファイルのフォルダは許可できません（" + sd + "）";
        string farawayDir = Trim(Cfg.Dir);
        if (string.Equals(full, farawayDir, StringComparison.OrdinalIgnoreCase) || full.StartsWith(farawayDir + "\\", StringComparison.OrdinalIgnoreCase))
            return "Faraway 自身の設定フォルダは許可できません";
        return null;
    }

    static bool IsSecretDirOrFile(string name, bool last)
    {
        foreach (string sd in SecretDirs) if (string.Equals(name, sd, StringComparison.OrdinalIgnoreCase)) return true;
        return last && SecretName.IsMatch(name);
    }

    // ルート内の相対パスを実パスにする。.. ・絶対パス・ドライブ・代替ストリーム・予約名・リンク・秘匿パスは OpError。
    static string Resolve(Root root, string rel)
    {
        string r = (rel ?? "").Replace('/', '\\').Trim();
        if (r.StartsWith("\\") || r.IndexOf(':') >= 0 || r.IndexOf('\0') >= 0) throw new OpError("ルートからの相対パスで指定してください");
        string rootFull = Trim(Path.GetFullPath(root.Path));
        string cur = rootFull;
        string[] parts = r.Split(new char[] { '\\' }, StringSplitOptions.RemoveEmptyEntries);
        for (int i = 0; i < parts.Length; i++)
        {
            string p = parts[i];
            if (p == ".") continue;
            if (p == "..") throw new OpError("親フォルダへは移動できません");
            string stem = p.TrimEnd(' ', '.');
            if (stem.Length == 0 || Array.IndexOf(Reserved, stem.Split('.')[0].ToUpperInvariant()) >= 0) throw new OpError("使えない名前です: " + p);
            if (IsSecretDirOrFile(p, i == parts.Length - 1)) throw new OpError("認証情報・秘匿ファイルのパスは扱えません");
            cur = Path.Combine(cur, p);
            if (File.Exists(cur) || Directory.Exists(cur))
            {
                if ((File.GetAttributes(cur) & FileAttributes.ReparsePoint) != 0) throw new OpError("リンク（ジャンクション・シンボリックリンク）は辿れません");
            }
        }
        string full = Path.GetFullPath(cur);
        if (!(string.Equals(Trim(full), rootFull, StringComparison.OrdinalIgnoreCase) || full.StartsWith(rootFull + "\\", StringComparison.OrdinalIgnoreCase)))
            throw new OpError("許可フォルダの外には出られません");
        return full;
    }

    static Root FindRoot(List<Root> roots, string name)
    {
        foreach (Root r in roots) if (string.Equals(r.Name, name, StringComparison.OrdinalIgnoreCase)) return r;
        throw new OpError("許可されていないフォルダです: " + name);
    }

    static string Str(Dictionary<string, object> a, string key)
    {
        return a.ContainsKey(key) && a[key] != null ? Convert.ToString(a[key]) : "";
    }

    // 戻り値: サーバーへ返すデータ。失敗は OpError（メッセージはそのまま利用者・モデルに見せてよい内容）。
    public static object Execute(string op, Dictionary<string, object> a, List<Root> roots)
    {
        Root root = FindRoot(roots, Str(a, "root"));
        string rel = Str(a, "path");
        if (op == "list") return List(root, rel);
        if (op == "read") return Read(root, rel, a.ContainsKey("max_bytes") ? Convert.ToInt32(a["max_bytes"]) : MaxRead);
        if (op == "write") return Write(root, rel, Str(a, "content"), Str(a, "mode"));
        throw new OpError("未対応の操作です");
    }

    static object List(Root root, string rel)
    {
        string dir = Resolve(root, rel);
        if (!Directory.Exists(dir)) throw new OpError("フォルダがありません");
        List<object> items = new List<object>();
        List<string> entries = new List<string>(Directory.GetFileSystemEntries(dir));
        entries.Sort(StringComparer.OrdinalIgnoreCase);
        bool truncated = false;
        foreach (string e in entries)
        {
            string name = Path.GetFileName(e);
            if (IsSecretDirOrFile(name, true)) continue;
            FileAttributes at = File.GetAttributes(e);
            if ((at & FileAttributes.ReparsePoint) != 0) continue;   // リンクは見せない
            if (items.Count >= MaxEntries) { truncated = true; break; }
            bool isDir = (at & FileAttributes.Directory) != 0;
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["name"] = name;
            d["type"] = isDir ? "dir" : "file";
            if (!isDir) d["size"] = new FileInfo(e).Length;
            d["modified"] = File.GetLastWriteTime(e).ToString("yyyy-MM-ddTHH:mm:ss");
            items.Add(d);
        }
        Dictionary<string, object> res = new Dictionary<string, object>();
        res["root"] = root.Name; res["path"] = rel; res["entries"] = items; res["truncated"] = truncated;
        return res;
    }

    static object Read(Root root, string rel, int max)
    {
        string file = Resolve(root, rel);
        if (!File.Exists(file)) throw new OpError("ファイルがありません");
        if (max <= 0 || max > MaxRead) max = MaxRead;
        using (FileStream fs = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            long size = fs.Length;
            int n = (int)Math.Min((long)max, size);
            byte[] buf = new byte[n];
            int got = 0;
            while (got < n) { int k = fs.Read(buf, got, n - got); if (k <= 0) break; got += k; }
            for (int i = 0; i < Math.Min(got, 8192); i++) if (buf[i] == 0) throw new OpError("バイナリファイルは読めません");
            int off = (got >= 3 && buf[0] == 0xEF && buf[1] == 0xBB && buf[2] == 0xBF) ? 3 : 0;
            Dictionary<string, object> res = new Dictionary<string, object>();
            res["root"] = root.Name; res["path"] = rel; res["size"] = size; res["truncated"] = size > got;
            res["text"] = new UTF8Encoding(false).GetString(buf, off, got - off);
            return res;
        }
    }

    static object Write(Root root, string rel, string content, string mode)
    {
        if (!root.Write) throw new OpError("このフォルダは読み取り専用です（アプリの設定で「読み書き」を許可すると書き込めます）");
        if (mode != "create" && mode != "overwrite" && mode != "append") throw new OpError("mode は create / overwrite / append のいずれかです");
        byte[] data = new UTF8Encoding(false).GetBytes(content);
        if (data.Length > MaxWrite) throw new OpError("書き込めるのは約 " + MaxWrite + " バイトまでです");
        if (rel.Trim().Length == 0) throw new OpError("ファイル名を指定してください");
        string file = Resolve(root, rel);
        string ext = Path.GetExtension(file).ToLowerInvariant();
        if (Array.IndexOf(BlockedWriteExt, ext) >= 0) throw new OpError("実行ファイル・スクリプト・ショートカットは書き込めません（" + ext + "）");
        if (Directory.Exists(file)) throw new OpError("同じ名前のフォルダがあります");
        string parent = Path.GetDirectoryName(file);
        if (!Directory.Exists(parent)) Directory.CreateDirectory(parent);   // 検証済みのルート内だけ
        if (mode == "create")
        {
            if (File.Exists(file)) throw new OpError("既に存在します（上書きするなら mode=overwrite）");
            using (FileStream fs = new FileStream(file, FileMode.CreateNew, FileAccess.Write, FileShare.None)) fs.Write(data, 0, data.Length);
        }
        else if (mode == "overwrite")
        {
            using (FileStream fs = new FileStream(file, FileMode.Create, FileAccess.Write, FileShare.None)) fs.Write(data, 0, data.Length);
        }
        else
        {
            if (File.Exists(file) && new FileInfo(file).Length + data.Length > 5 * MaxWrite) throw new OpError("追記後のサイズが大きすぎます");
            using (FileStream fs = new FileStream(file, FileMode.Append, FileAccess.Write, FileShare.None)) fs.Write(data, 0, data.Length);
        }
        Dictionary<string, object> res = new Dictionary<string, object>();
        res["root"] = root.Name; res["path"] = rel; res["bytes"] = data.Length; res["mode"] = mode;
        return res;
    }
}

// ---- アプリ自身の更新（自己アップデート）----
// サーバー（Faraway）が配布する最新の Faraway.exe を、確認 → ダウンロード → SHA-256 の照合 → 入れ替え、の順で更新する。
// 勝手には入れ替えない（トレイのメニューで利用者が選んだとき、または --self-update のとき）。古い版は Faraway.exe.bak に残す。
class ClientUpdateInfo
{
    public string Version;
    public string Sha256;
    public long Size;
}

static class ClientUpdate
{
    const long MaxSize = 8 * 1024 * 1024;

    static HttpWebRequest Req(string url, string token, int timeoutMs)
    {
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
        req.Timeout = timeoutMs;
        req.ReadWriteTimeout = timeoutMs;
        req.Proxy = null;
        req.Headers["Authorization"] = "Bearer " + token;
        return req;
    }

    // 新しい版があれば情報を返す。無ければ（または確認できなければ）null。
    public static ClientUpdateInfo Check()
    {
        string baseUrl = Cfg.ReadUrl();
        string token = Cfg.LoadToken();
        if (string.IsNullOrEmpty(baseUrl) || string.IsNullOrEmpty(token)) return null;
        ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072;
        string text;
        using (HttpWebResponse r = (HttpWebResponse)Req(baseUrl + "api/client/windows", token, 10000).GetResponse())
        using (StreamReader rd = new StreamReader(r.GetResponseStream(), Encoding.UTF8))
            text = rd.ReadToEnd();
        Dictionary<string, object> d = new JavaScriptSerializer().DeserializeObject(text) as Dictionary<string, object>;
        if (d == null || !d.ContainsKey("available") || !(d["available"] is bool) || !(bool)d["available"]) return null;
        ClientUpdateInfo info = new ClientUpdateInfo();
        info.Version = Convert.ToString(d["version"]);
        info.Sha256 = Convert.ToString(d["sha256"]).ToLowerInvariant();
        info.Size = Convert.ToInt64(d["size"]);
        try { if (new Version(info.Version) <= new Version(Cfg.Version)) return null; }
        catch (Exception) { return null; }
        return info;
    }

    // ダウンロードして検証する（サイズ・実行ファイルの先頭 "MZ"・SHA-256）。保存先のパスを返す。
    public static string Download(ClientUpdateInfo info)
    {
        if (info.Size <= 0 || info.Size > MaxSize) throw new OpError("配布されたファイルのサイズが不正です");
        string baseUrl = Cfg.ReadUrl();
        string token = Cfg.LoadToken();
        byte[] data;
        using (HttpWebResponse r = (HttpWebResponse)Req(baseUrl + "api/client/windows/download", token, 60000).GetResponse())
        using (Stream s = r.GetResponseStream())
        using (MemoryStream ms = new MemoryStream())
        {
            byte[] buf = new byte[16384];
            int k;
            while ((k = s.Read(buf, 0, buf.Length)) > 0)
            {
                ms.Write(buf, 0, k);
                if (ms.Length > MaxSize) throw new OpError("ダウンロードが大きすぎます");
            }
            data = ms.ToArray();
        }
        if (data.Length != info.Size) throw new OpError("ダウンロードしたサイズが一致しません");
        if (data.Length < 2 || data[0] != (byte)'M' || data[1] != (byte)'Z') throw new OpError("実行ファイルではありません");
        string hex;
        using (SHA256 sha = SHA256.Create())
        {
            StringBuilder sb = new StringBuilder();
            foreach (byte b in sha.ComputeHash(data)) sb.Append(b.ToString("x2"));
            hex = sb.ToString();
        }
        if (hex != info.Sha256) throw new OpError("SHA-256 が一致しません。更新を中止しました");
        string dir = Path.Combine(Cfg.Dir, "update");
        Directory.CreateDirectory(dir);
        string path = Path.Combine(dir, "Faraway-" + info.Version + ".exe");
        File.WriteAllBytes(path, data);
        return path;
    }

    // 入れ替え。実行中の exe は上書きできないので、終了を待ってから入れ替える小さなバッチを起動する。
    // 古い版は <exe>.bak に残す。restart=true なら、入れ替え後にトレイ常駐で起動し直す。
    public static void Apply(string newExe, bool restart)
    {
        string target = Application.ExecutablePath;
        string batch = Path.Combine(Path.GetDirectoryName(newExe), "apply-update.cmd");
        StringBuilder b = new StringBuilder();
        b.AppendLine("@echo off");
        b.AppendLine("setlocal");
        b.AppendLine("set \"TARGET=" + target.Replace("%", "%%") + "\"");
        b.AppendLine("set \"NEW=" + newExe.Replace("%", "%%") + "\"");
        b.AppendLine("set /a n=0");
        b.AppendLine(":retry");
        b.AppendLine("copy /Y \"%TARGET%\" \"%TARGET%.bak\" >nul 2>&1");
        b.AppendLine("move /Y \"%NEW%\" \"%TARGET%\" >nul 2>&1");
        b.AppendLine("if errorlevel 1 (");
        b.AppendLine("  set /a n+=1");
        b.AppendLine("  if %n% GEQ 60 exit /b 1");
        b.AppendLine("  ping -n 2 127.0.0.1 >nul");
        b.AppendLine("  goto retry");
        b.AppendLine(")");
        if (restart) b.AppendLine("start \"\" \"%TARGET%\" --no-window");
        b.AppendLine("exit /b 0");
        File.WriteAllText(batch, b.ToString(), Encoding.Default);
        ProcessStartInfo psi = new ProcessStartInfo("cmd.exe", "/c \"\"" + batch + "\"\"");
        psi.CreateNoWindow = true;
        psi.UseShellExecute = false;
        Process.Start(psi);
    }
}

// ---- サーバーとのブリッジ（ロングポーリング）----
class Bridge
{
    public volatile string Status = "起動中";
    public Action<string> OnWrite;          // 書き込みがあったときの通知（トレイ用）
    public Action<string> OnStatus;
    volatile bool stop;
    volatile bool needHello = true;
    DateTime lastHello = DateTime.MinValue;
    readonly JavaScriptSerializer js = new JavaScriptSerializer();

    public Bridge() { js.MaxJsonLength = 8 * 1024 * 1024; }

    public void Stop() { stop = true; }
    public void RootsChanged() { needHello = true; }

    void SetStatus(string s)
    {
        if (s == Status) return;
        Status = s;
        if (OnStatus != null) OnStatus(s);
    }

    string Call(string method, string url, string token, string body, int timeoutMs)
    {
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
        req.Method = method;
        req.Timeout = timeoutMs;
        req.ReadWriteTimeout = timeoutMs;
        req.Proxy = null;
        req.Headers["Authorization"] = "Bearer " + token;
        if (body != null)
        {
            byte[] b = Encoding.UTF8.GetBytes(body);
            req.ContentType = "application/json";
            req.ContentLength = b.Length;
            using (Stream s = req.GetRequestStream()) s.Write(b, 0, b.Length);
        }
        using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
        using (StreamReader rd = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
            return rd.ReadToEnd();
    }

    string Hello(string baseUrl, string token, List<Root> roots)
    {
        List<object> rs = new List<object>();
        foreach (Root r in roots)
        {
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["name"] = r.Name; d["mode"] = r.Write ? "readwrite" : "read";   // 実パスはサーバーに送らない
            rs.Add(d);
        }
        Dictionary<string, object> body = new Dictionary<string, object>();
        body["roots"] = rs; body["platform"] = "windows"; body["version"] = Cfg.Version;
        return Call("POST", baseUrl + "api/bridge/hello", token, js.Serialize(body), 10000);
    }

    public void Run()
    {
        ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072;   // TLS 1.2（HTTPS のサーバー用）
        while (!stop)
        {
            try
            {
                string baseUrl = Cfg.ReadUrl();
                string token = Cfg.LoadToken();
                if (string.IsNullOrEmpty(baseUrl)) { SetStatus("接続先が未設定"); Thread.Sleep(3000); continue; }
                if (string.IsNullOrEmpty(token)) { SetStatus("ログイン情報がありません（ペアリングが必要）"); Thread.Sleep(3000); continue; }
                List<Root> roots = Cfg.LoadRoots();
                if (needHello || (DateTime.UtcNow - lastHello).TotalMinutes > 5)
                {
                    Hello(baseUrl, token, roots);
                    needHello = false; lastHello = DateTime.UtcNow;
                }
                string text = Call("GET", baseUrl + "api/bridge/next?wait=25", token, null, 40000);
                SetStatus("接続中（許可フォルダ " + roots.Count + " 件）");
                Dictionary<string, object> msg = js.DeserializeObject(text) as Dictionary<string, object>;
                Dictionary<string, object> request = msg != null && msg.ContainsKey("request") ? msg["request"] as Dictionary<string, object> : null;
                if (request != null) Handle(baseUrl, token, request, roots);
            }
            catch (WebException ex)
            {
                HttpWebResponse r = ex.Response as HttpWebResponse;
                if (ex.Status == WebExceptionStatus.Timeout) continue;   // ロングポーリングの時間切れは正常
                SetStatus(r != null && r.StatusCode == HttpStatusCode.Unauthorized ? "ログイン情報が無効です（ペアリングし直してください）" : "サーバーに接続できません");
                needHello = true;
                Thread.Sleep(5000);
            }
            catch (Exception)
            {
                needHello = true;
                Thread.Sleep(5000);
            }
        }
    }

    void Handle(string baseUrl, string token, Dictionary<string, object> request, List<Root> roots)
    {
        string id = Convert.ToString(request["id"]);
        string op = Convert.ToString(request["op"]);
        Dictionary<string, object> args = request["args"] as Dictionary<string, object> ?? new Dictionary<string, object>();
        string rootName = args.ContainsKey("root") ? Convert.ToString(args["root"]) : "";
        string path = args.ContainsKey("path") ? Convert.ToString(args["path"]) : "";
        Dictionary<string, object> reply = new Dictionary<string, object>();
        reply["id"] = id;
        try
        {
            object data = FileOps.Execute(op, args, roots);
            reply["ok"] = true; reply["data"] = data;
            Cfg.Log(op, rootName, path, "ok");
            if (op == "write" && OnWrite != null) OnWrite(rootName + ":" + path);
        }
        catch (OpError e)
        {
            reply["ok"] = false; reply["error"] = e.Message;
            Cfg.Log(op, rootName, path, "拒否: " + e.Message);
        }
        catch (Exception e)
        {
            reply["ok"] = false; reply["error"] = "端末側でエラーが発生しました（" + e.GetType().Name + "）";
            Cfg.Log(op, rootName, path, "エラー: " + e.GetType().Name);
        }
        Call("POST", baseUrl + "api/bridge/result", token, js.Serialize(reply), 15000);
    }
}

// ---- 許可フォルダの管理画面 ----
class RootsForm : Form
{
    readonly List<Root> roots;
    readonly ListView list = new ListView();
    readonly Action changed;

    public RootsForm(List<Root> roots, Action changed)
    {
        this.roots = roots;
        this.changed = changed;
        Text = "Faraway — 許可フォルダ";
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(640, 360);
        MinimizeBox = false; MaximizeBox = false;
        FormBorderStyle = FormBorderStyle.FixedDialog;

        Label note = new Label();
        note.Text = "Faraway がこの PC で読み書きできるフォルダを決めます。ここに無い場所には、サーバーが何を要求しても触れません。\r\n書き込みは「読み書き」のフォルダだけで、さらに画面での承認が必要です。操作の記録は access.log に残ります。";
        note.SetBounds(12, 8, 616, 48);
        list.View = View.Details; list.FullRowSelect = true; list.MultiSelect = false;
        list.Columns.Add("名前", 120); list.Columns.Add("モード", 90); list.Columns.Add("フォルダ", 390);
        list.SetBounds(12, 62, 616, 240);
        Button add = new Button(); add.Text = "追加…"; add.SetBounds(12, 314, 90, 30);
        Button toggle = new Button(); toggle.Text = "モード切替"; toggle.SetBounds(110, 314, 100, 30);
        Button del = new Button(); del.Text = "削除"; del.SetBounds(218, 314, 90, 30);
        Button log = new Button(); log.Text = "操作の記録"; log.SetBounds(316, 314, 100, 30);
        Button close = new Button(); close.Text = "閉じる"; close.SetBounds(538, 314, 90, 30); close.DialogResult = DialogResult.OK;
        add.Click += delegate { AddRoot(); };
        toggle.Click += delegate { ToggleMode(); };
        del.Click += delegate { DeleteRoot(); };
        log.Click += delegate { if (File.Exists(Cfg.LogFile)) Process.Start("notepad.exe", Cfg.LogFile); else MessageBox.Show("まだ記録がありません。", "Faraway"); };
        Controls.AddRange(new Control[] { note, list, add, toggle, del, log, close });
        AcceptButton = close;
        Refill();
    }

    void Refill()
    {
        list.Items.Clear();
        foreach (Root r in roots)
        {
            ListViewItem it = new ListViewItem(r.Name);
            it.SubItems.Add(r.Write ? "読み書き" : "読み取りのみ");
            it.SubItems.Add(r.Path);
            list.Items.Add(it);
        }
    }

    void Commit() { Cfg.SaveRoots(roots); Refill(); if (changed != null) changed(); }

    void AddRoot()
    {
        using (FolderBrowserDialog fb = new FolderBrowserDialog())
        {
            fb.Description = "Faraway に許可するフォルダを選んでください";
            if (fb.ShowDialog(this) != DialogResult.OK) return;
            string why = FileOps.ForbiddenRoot(fb.SelectedPath);
            if (why != null) { MessageBox.Show(why, "Faraway", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
            string name = Path.GetFileName(fb.SelectedPath.TrimEnd('\\'));
            if (string.IsNullOrEmpty(name)) name = "folder";
            foreach (Root x in roots) if (string.Equals(x.Name, name, StringComparison.OrdinalIgnoreCase)) name = name + (roots.Count + 1);
            Root r = new Root();
            r.Name = name; r.Path = fb.SelectedPath;
            r.Write = MessageBox.Show("「" + r.Name + "」への書き込みも許可しますか？\r\n\r\n[はい] 読み書き（書き込みは都度の承認が必要）\r\n[いいえ] 読み取りのみ（安全）",
                "Faraway", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) == DialogResult.Yes;
            roots.Add(r);
            Commit();
        }
    }

    void ToggleMode()
    {
        if (list.SelectedIndices.Count == 0) return;
        Root r = roots[list.SelectedIndices[0]];
        r.Write = !r.Write;
        Commit();
    }

    void DeleteRoot()
    {
        if (list.SelectedIndices.Count == 0) return;
        roots.RemoveAt(list.SelectedIndices[0]);
        Commit();
    }
}

// ---- トレイ常駐 ----
class TrayApp : ApplicationContext
{
    readonly NotifyIcon icon = new NotifyIcon();
    readonly Bridge bridge;
    readonly ToolStripMenuItem statusItem = new ToolStripMenuItem("状態: 起動中");
    readonly ToolStripMenuItem updateItem = new ToolStripMenuItem("Faraway.exe を更新…");
    System.Threading.Timer updateTimer;
    readonly ToolStripMenuItem autoStart = new ToolStripMenuItem("Windows 起動時に自動で起動");
    readonly SynchronizationContext ui;
    const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";

    public TrayApp(Bridge bridge)
    {
        ui = SynchronizationContext.Current;
        this.bridge = bridge;
        try { icon.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (Exception) { icon.Icon = SystemIcons.Application; }
        icon.Text = "Faraway";
        ContextMenuStrip menu = new ContextMenuStrip();
        statusItem.Enabled = false;
        ToolStripMenuItem open = new ToolStripMenuItem("Faraway を開く");
        ToolStripMenuItem admin = new ToolStripMenuItem("管理画面を開く");
        ToolStripMenuItem roots = new ToolStripMenuItem("ファイルアクセスの設定（許可フォルダ）…");
        ToolStripMenuItem pair = new ToolStripMenuItem("ペアリング（ログイン情報の設定）…");
        ToolStripMenuItem quit = new ToolStripMenuItem("終了");
        ToolStripMenuItem about = new ToolStripMenuItem("バージョン " + Cfg.Version + "（更新を確認…）");
        updateItem.Visible = false;
        updateItem.Click += delegate { if (Program.RunSelfUpdate(false, true) == 0) { bridge.Stop(); icon.Visible = false; Application.Exit(); } };
        about.Click += delegate { if (Program.RunSelfUpdate(false, true) == 0) { bridge.Stop(); icon.Visible = false; Application.Exit(); } };
        open.Click += delegate { Program.OpenWindow(false); };
        admin.Click += delegate { Program.OpenWindow(true); };
        roots.Click += delegate { using (RootsForm f = new RootsForm(Cfg.LoadRoots(), delegate { bridge.RootsChanged(); })) f.ShowDialog(); };
        pair.Click += delegate { Program.Pair(); bridge.RootsChanged(); };
        autoStart.Checked = IsAutoStart();
        autoStart.Click += delegate { SetAutoStart(!autoStart.Checked); autoStart.Checked = IsAutoStart(); };
        quit.Click += delegate { bridge.Stop(); icon.Visible = false; Application.Exit(); };
        menu.Items.AddRange(new ToolStripItem[] { statusItem, updateItem, new ToolStripSeparator(), open, admin, new ToolStripSeparator(), roots, pair, autoStart, about, new ToolStripSeparator(), quit });
        // 起動の少し後と、6時間ごとに、新しい版が無いか確認する（確認だけ。入れ替えは利用者がメニューから選んだとき）
        updateTimer = new System.Threading.Timer(delegate
        {
            try
            {
                ClientUpdateInfo info = ClientUpdate.Check();
                if (info != null && !updateItem.Visible)
                    ui.Post(delegate { updateItem.Text = "Faraway.exe を更新（" + info.Version + "）…"; updateItem.Visible = true; icon.ShowBalloonTip(5000, "Faraway", "新しい版（" + info.Version + "）があります。トレイのメニューから更新できます。", ToolTipIcon.Info); }, null);
            }
            catch (Exception) { }
        }, null, 45000, 6 * 3600 * 1000);
        icon.ContextMenuStrip = menu;
        icon.DoubleClick += delegate { Program.OpenWindow(false); };
        icon.Visible = true;
        bridge.OnStatus = delegate (string s) { ui.Post(delegate { statusItem.Text = "状態: " + s; icon.Text = "Faraway — " + (s.Length > 40 ? s.Substring(0, 40) : s); }, null); };
        bridge.OnWrite = delegate (string what) { ui.Post(delegate { icon.ShowBalloonTip(4000, "Faraway", "ファイルに書き込みました: " + what, ToolTipIcon.Info); }, null); };
    }

    static bool IsAutoStart()
    {
        using (Microsoft.Win32.RegistryKey k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RunKey))
            return k != null && k.GetValue("Faraway") != null;
    }

    static void SetAutoStart(bool on)
    {
        using (Microsoft.Win32.RegistryKey k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RunKey, true))
        {
            if (k == null) return;
            if (on) k.SetValue("Faraway", "\"" + Application.ExecutablePath + "\" --no-window");
            else k.DeleteValue("Faraway", false);
        }
    }
}

static class Program
{
    const string Title = "Faraway";

    [STAThread]
    static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        bool admin = false, reset = false, check = false, headless = false, noWindow = false, addRoot = false;
        bool selfUpdate = false, yes = false, restartAfter = false, pairOnly = false;
        string given = null;
        List<string> rest = new List<string>();
        foreach (string a in args)
        {
            if (a == "--self-update") selfUpdate = true;
            else if (a == "--yes") yes = true;
            else if (a == "--restart") restartAfter = true;
            else if (a == "--pair") pairOnly = true;
            else if (a == "--admin") admin = true;
            else if (a == "--reset") reset = true;
            else if (a == "--check") check = true;
            else if (a == "--headless") headless = true;
            else if (a == "--no-window") noWindow = true;
            else if (a == "--add-root") addRoot = true;
            else if (!a.StartsWith("--")) { rest.Add(a); if (given == null) given = a; }
        }

        if (check)
        {
            string f;
            return given != null && Reachable(Cfg.Normalize(given, out f)) ? 0 : 1;
        }

        if (pairOnly)   // --pair <URL#token=…>: 接続先とログイン情報だけを保存する（画面なし）
        {
            if (given == null) return 2;
            string pf;
            string pb = Cfg.Normalize(given, out pf);
            string ptok = Cfg.TokenFromFragment(pf);
            if (ptok == null) return 3;
            Cfg.SaveToken(ptok);
            Cfg.SaveUrl(pb);
            return 0;
        }

        if (selfUpdate)   // --self-update [--yes] [--restart]: アプリ自身の更新（終了コード 0=更新を開始 / 10=最新 / 11=失敗 / 12=中止）
            return RunSelfUpdate(yes, restartAfter || !yes);

        if (addRoot)   // --add-root <名前> <フォルダ> [rw]
        {
            if (rest.Count < 2) return 2;
            string why = FileOps.ForbiddenRoot(rest[1]);
            if (why != null || !Directory.Exists(rest[1])) return 3;
            List<Root> roots = Cfg.LoadRoots();
            Root r = new Root(); r.Name = rest[0]; r.Path = Path.GetFullPath(rest[1]); r.Write = rest.Count > 2 && rest[2] == "rw";
            roots.RemoveAll(delegate (Root x) { return string.Equals(x.Name, r.Name, StringComparison.OrdinalIgnoreCase); });
            roots.Add(r);
            Cfg.SaveRoots(roots);
            return 0;
        }

        if (reset)
        {
            foreach (string f in new string[] { Cfg.UrlFile, Cfg.TokenFile, Cfg.RootsFile })
                try { if (File.Exists(f)) File.Delete(f); } catch (IOException) { }
            try { if (Directory.Exists(Cfg.Profile)) Directory.Delete(Cfg.Profile, true); } catch (IOException) { }
            MessageBox.Show("保存した接続先・ログイン情報・許可フォルダを消しました。", Title);
            return 0;
        }

        // 接続先とペアリング
        string baseUrl = Cfg.ReadUrl();
        string input = given != null && given.StartsWith("http") || (given != null && given.Contains(":")) ? given : null;
        if (input == null && string.IsNullOrEmpty(baseUrl) && !headless)
            input = Ask("Faraway サーバーのアドレスを入力してください。\r\n例: http://192.168.0.13:8765/\r\nサーバーが表示した「開くだけで設定される URL」（…/#token=…）でも構いません。", "");
        if (input == null && string.IsNullOrEmpty(baseUrl)) return 1;
        string pairing = null;
        while (true)
        {
            if (!string.IsNullOrEmpty(input))
            {
                string frag;
                baseUrl = Cfg.Normalize(input, out frag);
                string tok = Cfg.TokenFromFragment(frag);
                if (tok != null) { Cfg.SaveToken(tok); pairing = frag; }   // ブリッジ用に暗号化して保存（画面用はブラウザ側に保存される）
                Cfg.SaveUrl(baseUrl);
                input = null;
            }
            if (headless || Reachable(baseUrl)) break;
            DialogResult dr = MessageBox.Show(
                baseUrl + "\r\nに接続できません。\r\n\r\nサーバー（python -m api.server）が起動しているか、LAN / Tailscale に繋がっているかを確認してください。\r\n\r\n[はい] 再試行   [いいえ] アドレスを変更   [キャンセル] 終了",
                Title, MessageBoxButtons.YesNoCancel, MessageBoxIcon.Warning);
            if (dr == DialogResult.Cancel) return 1;
            if (dr == DialogResult.No)
            {
                input = Ask("Faraway サーバーのアドレスを入力してください。", baseUrl);
                if (input == null) return 1;
            }
        }

        if (headless)
        {
            Bridge hb = new Bridge();
            hb.Run();
            return 0;
        }

        // 二重起動の防止: すでに動いていれば、画面だけ開いて終了する
        bool created;
        using (Mutex m = new Mutex(true, "Faraway.Windows.App", out created))
        {
            if (!created)
            {
                if (!noWindow) OpenWindow(admin, pairing);
                return 0;
            }
            if (!noWindow) OpenWindow(admin, pairing);
            Bridge bridge = new Bridge();
            Thread t = new Thread(bridge.Run);
            t.IsBackground = true;
            t.Start();
            Application.Run(new TrayApp(bridge));
            bridge.Stop();
            GC.KeepAlive(m);
        }
        return 0;
    }

    // アプリ自身の更新。yes=true は確認なし（検証用・自動化用）。戻り値は終了コード（0 のとき、呼び出し側は終了すること）。
    public static int RunSelfUpdate(bool yes, bool restart)
    {
        ClientUpdateInfo info;
        try { info = ClientUpdate.Check(); }
        catch (Exception) { if (!yes) MessageBox.Show("更新を確認できませんでした（サーバーに接続できません）。", Title, MessageBoxButtons.OK, MessageBoxIcon.Warning); return 11; }
        if (info == null)
        {
            if (!yes) MessageBox.Show("Faraway.exe は最新です（" + Cfg.Version + "）。", Title);
            return 10;
        }
        if (!yes && MessageBox.Show("Faraway.exe の新しい版があります。\r\n\r\n現在: " + Cfg.Version + "\r\n新しい版: " + info.Version + "（" + info.Size / 1024 + " KB）\r\n配布元: " + Cfg.ReadUrl()
            + "\r\n\r\nダウンロードして入れ替え、再起動しますか？（古い版は Faraway.exe.bak に残ります）", Title, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
            return 12;
        try
        {
            string path = ClientUpdate.Download(info);
            ClientUpdate.Apply(path, restart);
            return 0;
        }
        catch (OpError e) { if (!yes) MessageBox.Show(e.Message, Title, MessageBoxButtons.OK, MessageBoxIcon.Warning); return 11; }
        catch (Exception) { if (!yes) MessageBox.Show("更新に失敗しました。", Title, MessageBoxButtons.OK, MessageBoxIcon.Warning); return 11; }
    }

    public static void OpenWindow(bool admin) { OpenWindow(admin, null); }

    public static void OpenWindow(bool admin, string pairing)
    {
        string baseUrl = Cfg.ReadUrl();
        string browser = FindBrowser();
        if (browser == null)
        {
            MessageBox.Show("Microsoft Edge または Google Chrome が見つかりません。", Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        string url = baseUrl + (admin ? "admin" : "") + (pairing != null ? "#" + pairing : "");
        Directory.CreateDirectory(Cfg.Profile);
        Process.Start(browser, "--app=" + url + " \"--user-data-dir=" + Cfg.Profile + "\" --window-size=520,880 --no-first-run --no-default-browser-check");
    }

    // ペアリング（ログイン情報）の設定
    public static void Pair()
    {
        string s = Ask("サーバーが表示した「開くだけで設定される URL」（…/#token=…）を入力してください。\r\n（python -m api.tokens add <端末名> でも発行できます）", "");
        if (s == null) return;
        string frag;
        string baseUrl = Cfg.Normalize(s, out frag);
        string tok = Cfg.TokenFromFragment(frag);
        if (tok == null) { MessageBox.Show("URL に #token=… が含まれていません。", Title, MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
        Cfg.SaveToken(tok);
        Cfg.SaveUrl(baseUrl);
        MessageBox.Show("ログイン情報を保存しました（この PC のこのユーザーだけが読める形で暗号化されます）。", Title);
    }

    // サーバーに到達できるか。認証なしの /api/health が 401 を返せば「生きている」。
    static bool Reachable(string baseUrl)
    {
        try
        {
            ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072;
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(baseUrl + "api/health");
            req.Timeout = 4000;
            req.Proxy = null;
            using (req.GetResponse()) { return true; }
        }
        catch (WebException e) { return e.Response != null; }
        catch (Exception) { return false; }
    }

    static string FindBrowser()
    {
        string pf = Environment.GetEnvironmentVariable("ProgramFiles");
        string pf86 = Environment.GetEnvironmentVariable("ProgramFiles(x86)");
        string[] candidates = new string[] {
            Path.Combine(pf86 ?? "", @"Microsoft\Edge\Application\msedge.exe"),
            Path.Combine(pf ?? "", @"Microsoft\Edge\Application\msedge.exe"),
            Path.Combine(pf ?? "", @"Google\Chrome\Application\chrome.exe"),
            Path.Combine(pf86 ?? "", @"Google\Chrome\Application\chrome.exe")
        };
        foreach (string c in candidates) if (File.Exists(c)) return c;
        return null;
    }

    // 入力ダイアログ。キャンセルなら null。
    public static string Ask(string prompt, string initial)
    {
        using (Form f = new Form())
        {
            f.Text = Title;
            f.FormBorderStyle = FormBorderStyle.FixedDialog;
            f.StartPosition = FormStartPosition.CenterScreen;
            f.MaximizeBox = false; f.MinimizeBox = false;
            f.ClientSize = new Size(480, 170);
            Label l = new Label(); l.Text = prompt; l.SetBounds(12, 12, 456, 74);
            TextBox t = new TextBox(); t.Text = initial; t.SetBounds(12, 94, 456, 24);
            Button ok = new Button(); ok.Text = "OK"; ok.DialogResult = DialogResult.OK; ok.SetBounds(292, 130, 84, 28);
            Button ng = new Button(); ng.Text = "キャンセル"; ng.DialogResult = DialogResult.Cancel; ng.SetBounds(384, 130, 84, 28);
            f.Controls.AddRange(new Control[] { l, t, ok, ng });
            f.AcceptButton = ok; f.CancelButton = ng;
            return f.ShowDialog() == DialogResult.OK && t.Text.Trim().Length > 0 ? t.Text : null;
        }
    }
}
