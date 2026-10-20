"""update_*：Faraway の自己更新。

- update_check（R0）… 更新元（設定で固定した main）の最新と、現在との差を調べる
- update_prepare（R1）… 新しい版を展開し、自己検査・テストを通す（稼働中の版には触れない。数分かかる）
- update_apply（R2）… 検証済みの版に切り替えて再起動する（承認が必要）。起動に失敗すれば自動で前の版に戻る
- update_rollback（R2）… 直前の版に戻して再起動する（承認が必要）
更新元のリポジトリ・ブランチは設定で固定され、モデルは変更できない。反映されるのは main にマージ済みのコードだけ。
"""
from __future__ import annotations

from typing import Any, Callable

from core.updater import UpdateError, Updater
from policy.levels import Level
from policy.registry import ToolSpec


def build_tools(up: Updater) -> list[ToolSpec]:
    def guard(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def run(a: dict[str, Any]) -> dict[str, Any]:
            try:
                return fn(a)
            except UpdateError as e:
                return {"error": "update_error", "reason": str(e)}
            except Exception as e:
                return {"error": "update_failed", "reason": type(e).__name__}
        return run

    def obj(props: dict[str, Any], req: list[str]) -> dict[str, Any]:
        return {"type": "object", "properties": props, "required": req, "additionalProperties": False}

    sha = {"type": "string", "description": "コミット SHA（7〜40桁）。update_check / update_prepare の戻り値の latest / sha"}
    return [
        ToolSpec("update_check", "Faraway の更新を確認する。更新元の main の最新と、現在の版との差（コミット一覧）を返す。", obj({}, []), Level.R0,
                 guard(lambda a: up.check()), egress=True),
        ToolSpec("update_prepare", "新しい版を展開し、自己検査とテストを通して「検証済み」にする（稼働中の版には触れない。数分かかる）。sha 省略で最新。",
                 obj({"sha": sha}, []), Level.R1, guard(lambda a: up.prepare(a.get("sha"))), egress=True),
        ToolSpec("update_apply", "検証済みの版に切り替えて Faraway を再起動する（承認が必要。起動に失敗すれば自動で前の版に戻る）。",
                 obj({"sha": sha}, ["sha"]), Level.R2, guard(lambda a: up.apply(a["sha"])),
                 summarizer=lambda a: f"Faraway を {up.version()[:12]} → {str(a.get('sha', ''))[:12]} に更新して再起動します（テスト・自己検査は通過済み。失敗時は自動で元に戻ります）"),
        ToolSpec("update_rollback", "直前の版に戻して Faraway を再起動する（承認が必要）。", obj({}, []), Level.R2, guard(lambda a: up.rollback()),
                 summarizer=lambda a: "Faraway を直前の版に戻して再起動します"),
    ]
