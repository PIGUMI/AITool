// Faraway.exe — Faraway（常駐 AI アシスタント）の Windows 用アプリ。
//
// Edge（無ければ Chrome）のアプリ表示で、Faraway の画面を専用ウィンドウとして開く起動アプリ。
// - 接続先（Faraway サーバーのアドレス）は %APPDATA%\Faraway\url.txt に保存する。初回だけ入力を求める。
// - ログイン情報（APIトークン）は、専用プロファイル（%APPDATA%\Faraway\profile）の中にだけ保存される。
//   サーバーが表示する「開くだけで設定される URL（…/#token=…）」を入力すると、初回に自動で設定される。
//   トークンは設定ファイルには書かない。
// - 画面そのもの（チャット・管理）はサーバーが配信するので、サーバーを更新すればアプリも新しくなる。
//
// 使い方:  Faraway.exe            通常起動（チャット画面）
//          Faraway.exe --admin    管理画面を開く
//          Faraway.exe --reset    保存した接続先とログイン情報を消す
//          Faraway.exe <URL>      接続先を指定して起動（ペアリング URL も可）
//          Faraway.exe --check <URL>   接続確認のみ（終了コード 0 = 到達できる）
//
// ビルド: apps\windows\build.ps1（Windows 標準の C# コンパイラ csc.exe を使う。追加のインストールは不要）
// C# 5 で書く（csc.exe v4.0.30319 のため。文字列補間や ?. は使えない）。

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Windows.Forms;

static class Program
{
    static readonly string Dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Faraway");
    static readonly string UrlFile = Path.Combine(Dir, "url.txt");
    static readonly string Profile = Path.Combine(Dir, "profile");
    const string Title = "Faraway";

    [STAThread]
    static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        bool admin = false, reset = false, check = false;
        string given = null;
        foreach (string a in args)
        {
            if (a == "--admin") admin = true;
            else if (a == "--reset") reset = true;
            else if (a == "--check") check = true;
            else if (!a.StartsWith("--")) given = a;
        }

        if (check)
        {
            return given != null && Reachable(Normalize(given, null)) ? 0 : 1;
        }

        if (reset)
        {
            try { if (File.Exists(UrlFile)) File.Delete(UrlFile); } catch (IOException) { }
            try { if (Directory.Exists(Profile)) Directory.Delete(Profile, true); } catch (IOException) { }
            MessageBox.Show("保存した接続先とログイン情報を消しました。", Title);
            return 0;
        }

        string baseUrl = ReadSaved();
        string pairing = null; // "token=..." （初回のペアリングだけ）
        string input = given;
        if (input == null && string.IsNullOrEmpty(baseUrl))
            input = Ask("Faraway サーバーのアドレスを入力してください。\r\n例: http://192.168.0.13:8765/\r\nサーバーが表示した「開くだけで設定される URL」（…/#token=…）でも構いません。", "");
        if (input == null && string.IsNullOrEmpty(baseUrl)) return 1;

        while (true)
        {
            if (!string.IsNullOrEmpty(input))
            {
                string frag;
                baseUrl = Normalize(input, out frag);
                if (frag != null && frag.StartsWith("token=")) pairing = frag;
                Save(baseUrl);
                input = null;
            }
            if (Reachable(baseUrl)) break;
            DialogResult r = MessageBox.Show(
                baseUrl + "\r\nに接続できません。\r\n\r\nサーバー（python -m api.server）が起動しているか、LAN / Tailscale に繋がっているかを確認してください。\r\n\r\n[はい] 再試行   [いいえ] アドレスを変更   [キャンセル] 終了",
                Title, MessageBoxButtons.YesNoCancel, MessageBoxIcon.Warning);
            if (r == DialogResult.Cancel) return 1;
            if (r == DialogResult.No)
            {
                input = Ask("Faraway サーバーのアドレスを入力してください。", baseUrl);
                if (input == null) return 1;
            }
        }

        string browser = FindBrowser();
        if (browser == null)
        {
            MessageBox.Show("Microsoft Edge または Google Chrome が見つかりません。", Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        string url = baseUrl + (admin ? "admin" : "") + (pairing != null ? "#" + pairing : "");
        Directory.CreateDirectory(Profile);
        string cmd = "--app=" + url + " \"--user-data-dir=" + Profile + "\" --window-size=520,880 --no-first-run --no-default-browser-check";
        Process.Start(browser, cmd);
        return 0;
    }

    // 入力を「末尾が / の http(s) アドレス」に整える。# 以降（ペアリング用）は frag に分ける。
    static string Normalize(string input, out string frag)
    {
        frag = null;
        string s = input.Trim();
        int h = s.IndexOf('#');
        if (h >= 0) { frag = s.Substring(h + 1); s = s.Substring(0, h); }
        if (!s.StartsWith("http://") && !s.StartsWith("https://")) s = "http://" + s;
        int q = s.IndexOf('/', s.IndexOf("://") + 3);
        s = q >= 0 ? s.Substring(0, q + 1) : s + "/";   // パスは捨て、ホスト:ポートだけ残す
        return s;
    }

    static string Normalize(string input, string unused)
    {
        string frag;
        return Normalize(input, out frag);
    }

    // サーバーに到達できるか。認証なしの /api/health が 401 を返せば「生きている」。
    static bool Reachable(string baseUrl)
    {
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(baseUrl + "api/health");
            req.Timeout = 4000;
            req.Proxy = null;
            using (req.GetResponse()) { return true; }
        }
        catch (WebException e)
        {
            return e.Response != null;   // 401 など、応答があれば到達できている
        }
        catch (Exception)
        {
            return false;
        }
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

    static string ReadSaved()
    {
        try { return File.Exists(UrlFile) ? File.ReadAllText(UrlFile).Trim() : ""; }
        catch (IOException) { return ""; }
    }

    static void Save(string baseUrl)
    {
        Directory.CreateDirectory(Dir);
        File.WriteAllText(UrlFile, baseUrl);   // アドレスだけ。トークンは保存しない
    }

    // 入力ダイアログ。キャンセルなら null。
    static string Ask(string prompt, string initial)
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
            Button ok = new Button(); ok.Text = "接続"; ok.DialogResult = DialogResult.OK; ok.SetBounds(292, 130, 84, 28);
            Button ng = new Button(); ng.Text = "キャンセル"; ng.DialogResult = DialogResult.Cancel; ng.SetBounds(384, 130, 84, 28);
            f.Controls.AddRange(new Control[] { l, t, ok, ng });
            f.AcceptButton = ok; f.CancelButton = ng;
            return f.ShowDialog() == DialogResult.OK && t.Text.Trim().Length > 0 ? t.Text : null;
        }
    }
}
