"""調整さんCSVを読み込み、チーム名・日程・参加可否を返す"""
import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


def date_sort_key(date_str: str) -> tuple[int, int, int]:
    """
    日付文字列を (年, 月, 日) の数値タプルに変換してソートキーとして使う。
    「8月1日（土）」→ (0, 8, 1)
    「2026年8月1日（土）」→ (2026, 8, 1)
    年が省略された場合は 0 とし、年ありの日付と混在しても月日で比較される。
    NFKC正規化で康熙部首（⽉⽇など）を標準漢字に統一してからパースする。
    """
    s = unicodedata.normalize("NFKC", str(date_str))
    year_m = re.search(r"(\d{4})年", s)
    month_day_m = re.search(r"(\d+)月(\d+)日", s)
    year = int(year_m.group(1)) if year_m else 0
    if month_day_m:
        return (year, int(month_day_m.group(1)), int(month_day_m.group(2)))
    return (0, 99, 99)


AVAILABLE = "◯"
MAYBE = "△"
UNAVAILABLE = "×"


@dataclass
class AvailabilityData:
    title: str
    teams: list[str]
    dates: list[str]
    # availability[date][team] = "◯" / "△" / "×"
    availability: dict[str, dict[str, str]] = field(default_factory=dict)
    comments: dict[str, str] = field(default_factory=dict)


def parse_chouseisan_csv(filepath: str | Path) -> AvailabilityData:
    """調整さん形式のCSVを読み込む（CP932エンコード対応）"""
    path = Path(filepath)
    with open(path, encoding="cp932", errors="replace", newline="") as f:
        rows = list(csv.reader(f))

    # タイトル行（1行目）
    title = rows[0][0].strip() if rows else ""

    # ヘッダー行を探す（「日程」で始まる行）
    header_row_idx = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == "日程":
            header_row_idx = i
            break

    if header_row_idx is None:
        raise ValueError("ヘッダー行（日程）が見つかりません")

    teams = [t.strip() for t in rows[header_row_idx][1:] if t.strip()]

    dates = []
    availability: dict[str, dict[str, str]] = {}
    comments: dict[str, str] = {}

    for row in rows[header_row_idx + 1:]:
        if not row or not row[0].strip():
            continue
        date_str = row[0].strip()

        # コメント行はスキップ
        if date_str == "コメント":
            for i, team in enumerate(teams):
                comment = row[i + 1].strip() if i + 1 < len(row) else ""
                if comment:
                    comments[team] = comment
            continue

        dates.append(date_str)
        availability[date_str] = {}
        for i, team in enumerate(teams):
            val = row[i + 1].strip() if i + 1 < len(row) else UNAVAILABLE
            # ◯/○ の表記ゆれを統一
            if val in ("◯", "○", "〇"):
                val = AVAILABLE
            elif val in ("△",):
                val = MAYBE
            else:
                val = UNAVAILABLE
            availability[date_str][team] = val

    return AvailabilityData(
        title=title,
        teams=teams,
        dates=dates,
        availability=availability,
        comments=comments,
    )
