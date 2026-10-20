"""権限レベル（R0〜R3）と、その判定。"""
from __future__ import annotations

from enum import Enum, IntEnum


class Level(IntEnum):
    """ツールの権限レベル。"""

    R0 = 0  # 読み取り専用
    R1 = 1  # 可逆な操作（監査ログ必須）
    R2 = 2  # 影響のある操作（承認必須）
    R3 = 3  # 禁止（実装しない）


class Disposition(Enum):
    """権限レベルに対する扱い。"""

    AUTO = "auto"
    AUTO_LOGGED = "auto_logged"
    REQUIRES_APPROVAL = "requires_approval"
    DENIED = "denied"


class PolicyError(Exception):
    """権限ポリシーに反する登録・実行。"""


def decide(level: Level) -> Disposition:
    """権限レベルから扱いを決める。未知の値は拒否側に倒す。"""
    if not isinstance(level, Level):
        return Disposition.DENIED
    return {
        Level.R0: Disposition.AUTO,
        Level.R1: Disposition.AUTO_LOGGED,
        Level.R2: Disposition.REQUIRES_APPROVAL,
        Level.R3: Disposition.DENIED,
    }[level]
