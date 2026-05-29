"""総当たり組み合わせ生成（ホーム/アウェイ対応、奇数チーム対応）"""
from dataclasses import dataclass


BYE = "__BYE__"


@dataclass
class Match:
    home: str
    away: str
    leg: int        # 1=第1戦, 2=第2戦
    match_id: int   # 全試合通し番号


def generate_matches(teams: list[str], double_round: bool = True) -> list[Match]:
    """
    総当たり組み合わせを生成する。
    double_round=True  → 2回戦総当たり（ホーム/アウェイ各1回）
    double_round=False → 1回戦総当たり
    奇数チームはBYEを追加して偶数にする。
    """
    working = list(teams)
    if len(working) % 2 == 1:
        working.append(BYE)

    n = len(working)
    rounds = _circle_method(working)  # [(home, away), ...] × ラウンド数

    matches: list[Match] = []
    match_id = 1

    for round_matches in rounds:
        for home, away in round_matches:
            if BYE in (home, away):
                continue
            matches.append(Match(home=home, away=away, leg=1, match_id=match_id))
            match_id += 1

    if double_round:
        first_leg_count = len(matches)
        for i in range(first_leg_count):
            m = matches[i]
            matches.append(Match(home=m.away, away=m.home, leg=2, match_id=match_id))
            match_id += 1

    return matches


def _circle_method(teams: list[str]) -> list[list[tuple[str, str]]]:
    """円形配置法で総当たり組み合わせを生成する"""
    n = len(teams)
    fixed = teams[0]
    rotating = teams[1:]
    rounds = []

    for _ in range(n - 1):
        current = [fixed] + rotating
        round_matches = []
        for j in range(n // 2):
            round_matches.append((current[j], current[n - 1 - j]))
        rounds.append(round_matches)
        rotating = [rotating[-1]] + rotating[:-1]  # 右回転

    return rounds
