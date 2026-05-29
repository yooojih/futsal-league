"""
参加可否・1日の目標試合数・消化均等化を考慮してスケジュールを割り当てる。

アルゴリズム概要（グリーディ法）:
1. 未消化試合を「両チームの消化数の合計が少ない順」に優先度付けする
2. 日程候補を日付順に走査する
3. 各日程でまず「◯のみ」で目標試合数まで埋め、
   目標に届かない場合は「△」も使って補填する
4. 目標試合数に達したら次の日程へ
"""
from dataclasses import dataclass, field
from .round_robin import Match
from .parser import AvailabilityData, AVAILABLE, MAYBE


@dataclass
class ScheduledMatch:
    match: Match
    date: str
    use_maybe: bool = False  # △を使って成立した試合かどうか


@dataclass
class AssignResult:
    scheduled: list[ScheduledMatch] = field(default_factory=list)
    unscheduled: list[Match] = field(default_factory=list)
    # games_count[team] = 消化試合数
    games_count: dict[str, int] = field(default_factory=dict)
    # 日付ごとの試合数サマリ
    games_per_day: dict[str, int] = field(default_factory=dict)


def assign_schedule(
    matches: list[Match],
    availability: AvailabilityData,
    target_games_per_day: int = 3,
    already_played: dict[str, int] | None = None,
) -> AssignResult:
    """
    matches: 今期に割り当てる試合リスト（未消化分）
    availability: 参加可否データ
    target_games_per_day: 1日の目標試合数（原則この数を目指す）
    already_played: 前期からの持ち越し消化数 {チーム名: 試合数}
    """
    games_count: dict[str, int] = dict(already_played or {})
    for team in availability.teams:
        games_count.setdefault(team, 0)

    remaining = list(matches)
    scheduled: list[ScheduledMatch] = []
    games_per_day: dict[str, int] = {}

    for date in availability.dates:
        avail = availability.availability.get(date, {})
        day_count = 0
        teams_used_today: set[str] = set()  # 当日すでに試合が入ったチーム

        # フェーズ1: ◯のみで目標試合数まで割り当て
        # フェーズ2: 目標に届かなければ△も使って補填
        for use_maybe in (False, True):
            if day_count >= target_games_per_day:
                break

            # 優先度: 両チームの消化試合数合計が少ない順
            remaining.sort(key=lambda m: games_count.get(m.home, 0) + games_count.get(m.away, 0))

            still_remaining = []
            for match in remaining:
                if day_count >= target_games_per_day:
                    still_remaining.append(match)
                    continue

                # 同一チームが当日すでに試合済みならスキップ（1日1試合制約）
                if match.home in teams_used_today or match.away in teams_used_today:
                    still_remaining.append(match)
                    continue

                home_ok = _is_available(avail.get(match.home, "×"), use_maybe)
                away_ok = _is_available(avail.get(match.away, "×"), use_maybe)

                if home_ok and away_ok:
                    use_maybe_flag = (
                        avail.get(match.home) == MAYBE or avail.get(match.away) == MAYBE
                    )
                    scheduled.append(ScheduledMatch(
                        match=match,
                        date=date,
                        use_maybe=use_maybe_flag,
                    ))
                    games_count[match.home] = games_count.get(match.home, 0) + 1
                    games_count[match.away] = games_count.get(match.away, 0) + 1
                    teams_used_today.add(match.home)
                    teams_used_today.add(match.away)
                    day_count += 1
                else:
                    still_remaining.append(match)

            remaining = still_remaining

        if day_count > 0:
            games_per_day[date] = day_count

    return AssignResult(
        scheduled=scheduled,
        unscheduled=remaining,
        games_count=games_count,
        games_per_day=games_per_day,
    )


def _is_available(status: str, allow_maybe: bool) -> bool:
    if status == AVAILABLE:
        return True
    if status == MAYBE and allow_maybe:
        return True
    return False
