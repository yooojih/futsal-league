"""複数リーグの統合スケジュール生成"""
from dataclasses import dataclass, field
from .round_robin import Match
from .parser import AvailabilityData, AVAILABLE, MAYBE


@dataclass
class LeagueConfig:
    name: str
    matches: list[Match]
    availability: AvailabilityData
    already_played: dict[str, int] = field(default_factory=dict)
    pace_multiplier: float = 1.0  # 進行係数: 1.4なら他リーグの1.4倍のペースで消化


@dataclass
class MultiScheduledMatch:
    match: Match
    league_name: str
    date: str
    slot: int       # 当日の試合順（1始まり）
    use_maybe: bool = False


@dataclass
class MultiAssignResult:
    scheduled: list[MultiScheduledMatch] = field(default_factory=list)
    unscheduled: dict[str, list[Match]] = field(default_factory=dict)
    games_count: dict[str, dict[str, int]] = field(default_factory=dict)
    games_per_day: dict[str, int] = field(default_factory=dict)


def assign_multi_league(
    leagues: list[LeagueConfig],
    all_dates: list[str],
    target_games_per_day: int = 3,
    excluded_dates: set[str] | None = None,
    pre_blocked: dict[str, set[str]] | None = None,
    pre_blocked_counts: dict[str, int] | None = None,
) -> MultiAssignResult:
    """
    全リーグの試合を統合して日程に割り当てる。

    割り当て方針:
    - 1日の試合数上限は全リーグ合計で target_games_per_day
    - 同一チームは全リーグ通じて1日1試合まで
    - 【同一カテゴリ優先】1日分のスロットをまずリーグA で埋め、
      埋まらない分を次のリーグBで補う → 同じ日に同じリーグの試合が集まりやすい
    - リーグ優先度は「チームあたり消化試合数が少ない順」
    """
    _excluded = excluded_dates or set()
    _pre_blocked: dict[str, set[str]] = pre_blocked or {}

    games_count: dict[str, dict[str, int]] = {}
    remaining: dict[str, list[Match]] = {}

    for league in leagues:
        gc = dict(league.already_played)
        for team in league.availability.teams:
            gc.setdefault(team, 0)
        games_count[league.name] = gc
        remaining[league.name] = list(league.matches)

    scheduled: list[MultiScheduledMatch] = []
    games_per_day: dict[str, int] = {}

    def league_priority(lg: LeagueConfig) -> float:
        """
        チームあたり消化数 ÷ 進行係数 を優先度スコアとする。
        係数が高いほどスコアが小さくなり（÷で割るため）より早く優先される。
        例: 係数1.4のリーグは他より1.4倍多くスロットを獲得する。
        """
        gc = games_count[lg.name]
        avg = sum(gc.values()) / max(len(gc), 1)
        return avg / lg.pace_multiplier

    def try_assign_league(
        league: LeagueConfig,
        date: str,
        teams_used: set[str],
        day_count: int,
        allow_maybe: bool,
    ) -> int:
        """
        1リーグ分の試合をこの日に割り当てられるだけ割り当てる。
        追加できた試合数を返す。
        """
        added = 0
        avail = league.availability.availability.get(date, {})
        gc = games_count[league.name]
        rem = remaining[league.name]
        rem.sort(key=lambda m: gc.get(m.home, 0) + gc.get(m.away, 0))

        assigned_ids: set[int] = set()
        for match in rem:
            if day_count + added >= target_games_per_day:
                break
            if match.home in teams_used or match.away in teams_used:
                continue
            home_ok = _is_available(avail.get(match.home, "×"), allow_maybe)
            away_ok = _is_available(avail.get(match.away, "×"), allow_maybe)
            if not (home_ok and away_ok):
                continue

            use_maybe_flag = (
                avail.get(match.home) == MAYBE or avail.get(match.away) == MAYBE
            )
            scheduled.append(MultiScheduledMatch(
                match=match,
                league_name=league.name,
                date=date,
                slot=day_count + added + 1,
                use_maybe=use_maybe_flag,
            ))
            gc[match.home] = gc.get(match.home, 0) + 1
            gc[match.away] = gc.get(match.away, 0) + 1
            teams_used.add(match.home)
            teams_used.add(match.away)
            assigned_ids.add(match.match_id)
            added += 1

        remaining[league.name] = [m for m in rem if m.match_id not in assigned_ids]
        return added

    for date in all_dates:
        if date in _excluded:
            continue
        # 確定済み試合のチームを事前にブロック
        teams_used_today: set[str] = set(_pre_blocked.get(date, set()))
        # pre_blocked_countsがあればそれを使う。なければチーム数から推算（フォールバック）
        if pre_blocked_counts and date in pre_blocked_counts:
            day_count = pre_blocked_counts[date]
        else:
            day_count = len(teams_used_today) // 2

        # リーグを優先度順に並べ、1リーグずつスロットを埋める
        # → 同じリーグの試合が同じ日にまとまりやすい
        for league in sorted(leagues, key=league_priority):
            if day_count >= target_games_per_day:
                break
            # フェーズ1: ◯のみ
            day_count += try_assign_league(league, date, teams_used_today, day_count, allow_maybe=False)

        # フェーズ2: まだ目標に届かなければ △ も使って補填
        if day_count < target_games_per_day:
            for league in sorted(leagues, key=league_priority):
                if day_count >= target_games_per_day:
                    break
                day_count += try_assign_league(league, date, teams_used_today, day_count, allow_maybe=True)

        if day_count > 0:
            games_per_day[date] = day_count

    return MultiAssignResult(
        scheduled=scheduled,
        unscheduled={lg.name: remaining[lg.name] for lg in leagues},
        games_count=games_count,
        games_per_day=games_per_day,
    )


def _is_available(status: str, allow_maybe: bool) -> bool:
    if status == AVAILABLE:
        return True
    if status == MAYBE and allow_maybe:
        return True
    return False
