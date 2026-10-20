"""device_*：アプリがインストールされた端末のファイルを使う（ブラウザ版には無い機能）。

- device_info / device_list / device_read（R0）… 端末が許可したフォルダ（ルート）の中だけ。読むと、その依頼では外部通信ツールが止まる
- device_write（R2）… 承認が必要。ルートが「読み書き」で許可されているときだけ端末側が受け付ける
- 削除・移動・実行は実装しない（R3）
パスの検証は、サーバーと端末（アプリ）の両方で行う。端末側が最終的な権威。
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from core.bridge import DeviceBridge
from policy.levels import Level
from policy.registry import ToolSpec

MAX_READ = 200_000
MAX_WRITE = 500_000
SECRET_NAME = re.compile(r"(^\.env|\.pem$|\.key$|\.pfx$|\.p12$|\.kdbx$|^id_(rsa|ed25519|ecdsa)|^known_hosts$|^credentials?$|"
                         r"^api_tokens\.json$|^google_(token|client)\.json$|\.keychain$|^login data$|^cookies$|\.ppk$)", re.I)
SECRET_DIR = {".ssh", ".gnupg", ".aws", ".azure", "secrets", ".git"}


class PathRefused(Exception):
    """端末に渡す前に拒否するパス。"""


def clean_path(path: str) -> str:
    """ルート内の相対パスを '/' 区切りに整える。.. ・絶対パス・ドライブ指定・秘匿ファイル/フォルダは PathRefused。"""
    p = path.replace("\\", "/").strip()
    if p in ("", ".", "/"):
        return ""
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or "\0" in p:
        raise PathRefused("ルートからの相対パスで指定してください")
    parts = [x for x in PurePosixPath(p).parts if x not in (".", "")]
    if any(x == ".." for x in parts):
        raise PathRefused("親フォルダへは移動できません")
    if any(x.lower() in SECRET_DIR for x in parts[:-1]) or (parts and (parts[-1].lower() in SECRET_DIR or SECRET_NAME.search(parts[-1]))):
        raise PathRefused("認証情報・秘匿ファイルのパスは扱えません")
    return "/".join(parts)


def build_tools(bridge: DeviceBridge) -> list[ToolSpec]:
    def obj(props: dict[str, Any], req: list[str]) -> dict[str, Any]:
        return {"type": "object", "properties": props, "required": req, "additionalProperties": False}

    dev = {"type": "string", "description": "端末名（device_info で確認。アプリを入れた端末のトークン名）"}
    root = {"type": "string", "description": "許可フォルダの名前（device_info の roots）"}

    def call(op: str, a: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        r = bridge.call(a["device"], op, args)
        return {"error": r["error"], "reason": r.get("reason", "")} if not r["ok"] else r["data"]

    def info(a: dict[str, Any]) -> dict[str, Any]:
        return {"devices": {d: {"online": i["online"], "platform": i["platform"], "roots": i["roots"]} for d, i in bridge.devices().items()},
                "note": "roots の mode が read の場所には書き込めません。"}

    def lst(a: dict[str, Any]) -> dict[str, Any]:
        try:
            return call("list", a, {"root": a["root"], "path": clean_path(a.get("path", ""))})
        except PathRefused as e:
            return {"error": "refused", "reason": str(e)}

    def read(a: dict[str, Any]) -> dict[str, Any]:
        try:
            return call("read", a, {"root": a["root"], "path": clean_path(a["path"]), "max_bytes": min(int(a.get("max_bytes", MAX_READ)), MAX_READ)})
        except PathRefused as e:
            return {"error": "refused", "reason": str(e)}

    def write(a: dict[str, Any]) -> dict[str, Any]:
        try:
            if len(a["content"].encode("utf-8")) > MAX_WRITE:
                return {"error": "too_large", "reason": f"{MAX_WRITE} バイトまでです"}
            path = clean_path(a["path"])
            if not path:
                return {"error": "refused", "reason": "ファイル名を指定してください"}
            return call("write", a, {"root": a["root"], "path": path, "content": a["content"], "mode": a.get("mode", "create")})
        except PathRefused as e:
            return {"error": "refused", "reason": str(e)}

    return [
        ToolSpec("device_info", "アプリが接続している端末と、許可フォルダ（ルート）・読み書きの別を返す。端末のファイルを使う前に確認する。", obj({}, []), Level.R0, info),
        ToolSpec("device_list", "端末の許可フォルダ内のファイル・フォルダ一覧を返す。", obj({"device": dev, "root": root, "path": {"type": "string"}}, ["device", "root"]),
                 Level.R0, lst, reads_private=True),
        ToolSpec("device_read", "端末の許可フォルダ内のテキストファイルを読む（最大約200KB）。読むと、この依頼では外部通信ツールが使えなくなる。",
                 obj({"device": dev, "root": root, "path": {"type": "string"}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_READ}}, ["device", "root", "path"]),
                 Level.R0, read, reads_private=True),
        ToolSpec("device_write", "端末の許可フォルダ（読み書きで許可されたもの）にテキストファイルを書く。承認が必要。mode: create=新規のみ / overwrite=上書き / append=追記。",
                 obj({"device": dev, "root": root, "path": {"type": "string"}, "content": {"type": "string"},
                      "mode": {"type": "string", "enum": ["create", "overwrite", "append"]}}, ["device", "root", "path", "content"]),
                 Level.R2, write,
                 summarizer=lambda a: f"端末 {a.get('device')} の {a.get('root')}:{a.get('path')} に{ {'create': '新規作成', 'overwrite': '上書き', 'append': '追記'}.get(a.get('mode', 'create'), '書き込み') }します（{len(str(a.get('content', '')).encode('utf-8'))} バイト）"),
    ]
