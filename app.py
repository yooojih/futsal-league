"""フットサルリーグ 統合スケジューラー（最大4リーグ対応）"""
import streamlit as st
import json
import unicodedata
from pathlib import Path
from collections import defaultdict
import pandas as pd

from scheduler.parser import parse_chouseisan_csv, AvailabilityData, date_sort_key
from scheduler.round_robin import generate_matches, Match
from scheduler.multi_league import LeagueConfig, assign_multi_league, MultiAssignResult, MultiScheduledMatch
from scheduler.excel_output import build_multi_excel


def _build_canonical_date_map(date_strings: list[str]) -> dict[tuple, str]:
    """
    日付文字列のリストから (year, month, day) キー → 代表文字列 のマップを作る。
    年なし "5月3日（土）" と年あり "2026年5月3日（土）" が同じキーになる場合は
    長い方（年あり）を代表として採用する。
    """
    raw: dict[str, tuple] = {d: date_sort_key(str(d).strip()) for d in date_strings}
    max_year = max((k[0] for k in raw.values() if k[0] > 0), default=0)

    def _norm(d: str) -> tuple:
        k = raw[d]
        return (max_year, k[1], k[2]) if k[0] == 0 and max_year > 0 else k

    canonical: dict[tuple, str] = {}
    for d in date_strings:
        k = _norm(d)
        if k not in canonical or len(str(d)) > len(str(canonical[k])):
            canonical[k] = d
    return canonical


def _normalize_date(date_str: str, canonical: dict[tuple, str]) -> str:
    """date_str を canonical マップで正規化した文字列に変換する。"""
    d = str(date_str).strip()
    raw_keys = {d: date_sort_key(d)}
    max_year = max((v[0] for v in canonical.keys() if v[0] > 0), default=0)
    k = date_sort_key(d)
    if k[0] == 0 and max_year > 0:
        k = (max_year, k[1], k[2])
    return canonical.get(k, d)

STATE_FILE = Path("data/season_state.json")
LEAGUE_ICONS = ["🔵", "🔴", "🟢", "🟡"]
DEFAULT_NAMES = ["U18_1部", "U18_2部", "U15_1部", "U15_2部"]


# ------------------------------------------------------------------
# 状態管理
# ------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"leagues_config": [], "all_matches": {}, "periods": [], "games_count": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# 作業状態の保存・復元（season_state.json の "current_period" キー）
# ------------------------------------------------------------------
def _ser_result(result: MultiAssignResult) -> dict:
    return {
        "scheduled": [
            {
                "match": {"home": sm.match.home, "away": sm.match.away,
                          "leg": sm.match.leg, "match_id": sm.match.match_id},
                "league_name": sm.league_name,
                "date": sm.date,
                "slot": sm.slot,
                "use_maybe": sm.use_maybe,
            }
            for sm in result.scheduled
        ],
        "unscheduled": {
            ln: [{"home": m.home, "away": m.away, "leg": m.leg, "match_id": m.match_id}
                 for m in ms]
            for ln, ms in result.unscheduled.items()
        },
        "games_count": result.games_count,
        "games_per_day": result.games_per_day,
    }


def _deser_result(data: dict) -> MultiAssignResult:
    return MultiAssignResult(
        scheduled=[
            MultiScheduledMatch(
                match=Match(**sm["match"]),
                league_name=sm["league_name"],
                date=sm["date"],
                slot=sm["slot"],
                use_maybe=sm["use_maybe"],
            )
            for sm in data["scheduled"]
        ],
        unscheduled={
            ln: [Match(**m) for m in ms]
            for ln, ms in data["unscheduled"].items()
        },
        games_count=data["games_count"],
        games_per_day=data["games_per_day"],
    )


def _ser_avails(avails: dict) -> dict:
    return {
        ln: {
            "title": av.title, "teams": av.teams,
            "dates": av.dates, "availability": av.availability,
            "comments": av.comments,
        }
        for ln, av in avails.items()
    }


def _deser_avails(data: dict) -> dict:
    return {
        ln: AvailabilityData(
            title=d["title"], teams=d["teams"],
            dates=d["dates"], availability=d["availability"],
            comments=d["comments"],
        )
        for ln, d in data.items()
    }


def save_working_state():
    """現在の作業状態を season_state.json に保存する"""
    if "unified_result" not in st.session_state:
        return
    state["current_period"] = {
        "period_name": st.session_state.get("period_name", ""),
        "pre_played_matches": st.session_state.get("pre_played_matches", {}),
        "confirmed": {
            ln: list(ids)
            for ln, ids in st.session_state.get("confirmed", {}).items()
        },
        "excluded_dates": list(st.session_state.get("excluded_dates", set())),
        "max_games": st.session_state.get("max_games", 3),
        "all_dates": st.session_state.get("all_dates", []),
        "unified_result": _ser_result(st.session_state.unified_result),
        "avails": _ser_avails(st.session_state.get("avails", {})),
    }
    save_state(state)


def restore_working_state():
    """season_state.json の current_period からセッション状態を復元する"""
    cp = state.get("current_period", {})
    st.session_state.period_name = cp.get("period_name", "")
    st.session_state["period_name_widget"] = cp.get("period_name", "")
    st.session_state["max_games_widget"] = cp.get("max_games", 3)
    st.session_state.pre_played_matches = cp.get("pre_played_matches", {})
    st.session_state.confirmed = {
        ln: set(ids) for ln, ids in cp.get("confirmed", {}).items()
    }
    st.session_state.excluded_dates = set(cp.get("excluded_dates", []))
    st.session_state.max_games = cp.get("max_games", 3)
    st.session_state.all_dates = cp.get("all_dates", [])
    st.session_state.unified_result = _deser_result(cp["unified_result"])
    st.session_state.avails = _deser_avails(cp.get("avails", {}))


if "state" not in st.session_state:
    st.session_state.state = load_state()
state = st.session_state.state

st.set_page_config(page_title="フットサルリーグ スケジューラー", layout="wide")
st.title("⚽ フットサルリーグ 統合スケジューラー")


# ------------------------------------------------------------------
# サイドバー: リーグ設定
# ------------------------------------------------------------------
with st.sidebar:
    st.header("リーグ設定")

    saved_configs = state.get("leagues_config", [])
    n_leagues = st.number_input(
        "リーグ数", min_value=1, max_value=4,
        value=max(len(saved_configs), 2),
    )

    leagues_config = []
    for i in range(n_leagues):
        saved = saved_configs[i] if i < len(saved_configs) else {}
        st.markdown(f"**{LEAGUE_ICONS[i]} リーグ {i + 1}**")
        name = st.text_input(
            "リーグ名", value=saved.get("name", DEFAULT_NAMES[i]),
            key=f"lname_{i}",
        )
        double = st.radio(
            "試合形式",
            ["2回戦総当たり", "1回戦総当たり"],
            index=0 if saved.get("double_round", True) else 1,
            key=f"ldouble_{i}",
            horizontal=True,
        ) == "2回戦総当たり"
        pace = st.number_input(
            "進行係数",
            min_value=0.5, max_value=5.0,
            value=float(saved.get("pace_multiplier", 1.0)),
            step=0.1, format="%.1f",
            key=f"lpace_{i}",
            help="1.0=標準ペース。1.4なら他リーグの1.4倍のスピードで試合を消化します。",
        )
        leagues_config.append({"name": name, "double_round": double, "pace_multiplier": pace})
        if i < n_leagues - 1:
            st.markdown("---")

    state["leagues_config"] = leagues_config

    st.markdown("---")
    st.subheader("確定済みの期")
    for p in state.get("periods", []):
        st.write(f"✅ {p['name']}")
    if not state.get("periods"):
        st.caption("まだありません")

    if st.button("🗑️ シーズンをリセット", type="secondary"):
        state.clear()
        state.update({"leagues_config": leagues_config, "all_matches": {}, "periods": [], "games_count": {}})
        for key in ["unified_result", "avails", "confirmed", "pre_played_matches",
                    "period_name", "excluded_dates", "max_games", "all_dates"]:
            st.session_state.pop(key, None)
        save_state(state)
        st.success("リセットしました")
        st.rerun()

    st.markdown("---")
    st.subheader("📦 状態のエクスポート / インポート")
    st.caption("別環境（Renderなど）への移行や、バックアップに使用します。")

    # エクスポート: 現在の state をそのままダウンロード
    _state_json_bytes = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        label="📤 状態ファイルをダウンロード",
        data=_state_json_bytes,
        file_name="season_state.json",
        mime="application/json",
        help="確定試合・消化数・作業中の期を含む状態ファイルを保存します。",
    )

    # インポート: アップロードした JSON を state に反映
    if "state_import_key" not in st.session_state:
        st.session_state.state_import_key = 0
    _state_import_file = st.file_uploader(
        "📥 状態ファイルを読み込む",
        type=["json"],
        key=f"state_import_{st.session_state.state_import_key}",
        help="別環境からエクスポートした season_state.json を読み込みます。現在の状態は上書きされます。",
    )
    if _state_import_file is not None:
        try:
            _imported_state = json.loads(_state_import_file.read().decode("utf-8"))
            if isinstance(_imported_state, dict):
                state.clear()
                state.update(_imported_state)
                save_state(state)
                # セッション状態をクリアして復元フローに乗せる
                for _k in ["unified_result", "avails", "confirmed", "pre_played_matches",
                           "period_name", "excluded_dates", "max_games", "all_dates"]:
                    st.session_state.pop(_k, None)
                st.session_state.state_import_key += 1
                st.success("状態ファイルを読み込みました。")
                st.rerun()
            else:
                st.error("形式が正しくありません（dictが必要です）。")
        except Exception as _import_err:
            st.error(f"読み込みエラー: {_import_err}")


# ------------------------------------------------------------------
# メイン: 保存済み作業状態の復元
# ------------------------------------------------------------------
if "current_period" in state and "unified_result" not in st.session_state:
    cp = state["current_period"]
    pname_saved = cp.get("period_name", "（名称なし）")
    n_conf = sum(len(v) for v in cp.get("confirmed", {}).values())
    st.info(f"保存済みの作業状態があります: **{pname_saved}**（確定済み {n_conf} 試合）")
    col_r1, col_r2 = st.columns([1, 1])
    with col_r1:
        if st.button("📂 前回の状態を復元して再開", type="primary"):
            restore_working_state()
            st.rerun()
    with col_r2:
        if st.button("🗑️ 保存状態を破棄して新規作成"):
            state.pop("current_period", None)
            save_state(state)
            st.rerun()
    st.markdown("---")

# ------------------------------------------------------------------
# メイン: 期の作成
# ------------------------------------------------------------------
st.header("新しい期のスケジュールを作成")

col_period, col_games = st.columns([2, 1])
with col_period:
    period_name = st.text_input(
        "期の名前", placeholder="例：中期（8〜10月）",
        key="period_name_widget",
    )
with col_games:
    max_games = st.number_input(
        "1日の目標試合数（全リーグ合計）",
        min_value=1, max_value=8, value=3,
        key="max_games_widget",
        help="全リーグ合計で1日に行う試合数の目標。参加可否の制約で達しない日は少なくなります。",
    )

# CSVアップロード（リーグ数に応じて列を分割）
st.subheader("調整さんCSVのアップロード（リーグごと）")
upload_cols = st.columns(n_leagues)
uploaded_files: dict[str, object] = {}
for i, (col, lc) in enumerate(zip(upload_cols, leagues_config)):
    with col:
        f = st.file_uploader(
            f"{LEAGUE_ICONS[i]} {lc['name']}",
            type=["csv"],
            key=f"csv_{i}",
        )
        if f:
            uploaded_files[lc["name"]] = f

all_uploaded = (len(uploaded_files) == n_leagues)

if period_name and not all_uploaded:
    missing = [lc["name"] for lc in leagues_config if lc["name"] not in uploaded_files]
    st.info(f"未アップロード: {', '.join(missing)}")

if period_name and all_uploaded:
    # ------------------------------------------------------------------
    # 消化済み試合の入力（任意）
    # ------------------------------------------------------------------
    with st.expander("📋 消化済み試合を入力（任意）", expanded=False):
        st.caption(
            "すでに行われた試合にチェックを入れ、日付とスロットを入力してください。"
            "スケジュール生成から除外され、消化数に加算されます。日付を入力した試合はExcelにも出力されます。"
        )

        # エクスポート / インポート
        # インポート後にアップローダーをリセットするためキーにカウンターを使う
        if "pre_played_import_key" not in st.session_state:
            st.session_state.pre_played_import_key = 0

        _pp_state = st.session_state.get("pre_played_matches", {})
        _io_col1, _io_col2 = st.columns([1, 2])
        with _io_col1:
            _export_json = json.dumps(_pp_state, ensure_ascii=False, indent=2)
            st.download_button(
                label="📤 エクスポート（JSON）",
                data=_export_json.encode("utf-8"),
                file_name="pre_played_matches.json",
                mime="application/json",
                help="現在の消化済み試合データをJSONファイルとして保存します。",
            )
        with _io_col2:
            _import_file = st.file_uploader(
                "📥 インポート（JSON）",
                type=["json"],
                key=f"pre_played_import_{st.session_state.pre_played_import_key}",
                help="エクスポートしたJSONファイルを読み込みます。現在のデータは上書きされます。",
                label_visibility="collapsed",
            )
            if _import_file is not None:
                try:
                    _imported = json.loads(_import_file.read().decode("utf-8"))
                    if isinstance(_imported, dict):
                        st.session_state.pre_played_matches = _imported
                        st.session_state.pre_played_import_key += 1  # アップローダーをリセット
                        st.success("インポートしました。")
                        st.rerun()
                    else:
                        st.error("形式が正しくありません（dictが必要です）。")
                except Exception as _e:
                    st.error(f"読み込みエラー: {_e}")

        st.markdown("---")

        _tmp_dir = Path("data")
        _tmp_dir.mkdir(exist_ok=True)

        # 試合リストを取得（state にあれば再利用、なければCSVから生成）
        _preview_store: dict[str, dict] = {}
        for _lname_s, _m_list in state.get("all_matches", {}).items():
            _preview_store[_lname_s] = {"matches": _m_list}

        for _lc in leagues_config:
            _lname = _lc["name"]
            if _lname not in _preview_store:
                _f = uploaded_files[_lname]
                _tmp = _tmp_dir / _f.name
                _tmp.write_bytes(_f.getvalue())
                try:
                    _avail_p = parse_chouseisan_csv(_tmp)
                    _raw_p = generate_matches(_avail_p.teams, double_round=_lc["double_round"])
                    _preview_store[_lname] = {
                        "matches": [
                            {"home": m.home, "away": m.away, "leg": m.leg, "match_id": m.match_id}
                            for m in _raw_p
                        ],
                    }
                except Exception:
                    _preview_store[_lname] = {"matches": []}

        if "pre_played_matches" not in st.session_state:
            st.session_state.pre_played_matches = {}

        _tabs = st.tabs([lc["name"] for lc in leagues_config])
        for _tab, _lc in zip(_tabs, leagues_config):
            with _tab:
                _lname = _lc["name"]
                _match_data = _preview_store.get(_lname, {}).get("matches", [])

                if not _match_data:
                    st.info("試合リストを取得できません（CSVを確認してください）")
                    continue

                _matches_p = [
                    Match(
                        home=d["home"], away=d["away"],
                        leg=d["leg"], match_id=d["match_id"],
                    )
                    for d in _match_data
                ]

                # 保存済みデータのサマリ表示
                _saved = st.session_state.pre_played_matches.get(_lname, [])
                if _saved:
                    _with_date = sum(1 for d in _saved if d.get("date"))
                    st.caption(
                        f"現在の設定: {len(_saved)} 試合を消化済みとして登録済み"
                        + (f"（うち {_with_date} 試合はExcel出力対象）" if _with_date else "")
                    )

                _cur_by_id = {d["match_id"]: d for d in _saved}

                _df = pd.DataFrame([{
                    "消化済み": m.match_id in _cur_by_id,
                    "日付": _cur_by_id.get(m.match_id, {}).get("date", ""),
                    "スロット": int(_cur_by_id[m.match_id]["slot"]) if m.match_id in _cur_by_id else 1,
                    "前後半": "前半戦" if m.leg == 1 else "後半戦",
                    "ホーム": m.home,
                    "アウェイ": m.away,
                } for m in _matches_p])

                # st.form で包むことで、入力中の再実行による入力リセットを防ぐ
                with st.form(key=f"pre_played_form_{_lname}"):
                    _edited = st.data_editor(
                        _df,
                        column_config={
                            "消化済み": st.column_config.CheckboxColumn(default=False),
                            "日付": st.column_config.TextColumn(
                                help="例: 5月3日（土）",
                            ),
                            "スロット": st.column_config.NumberColumn(
                                min_value=1, max_value=8, step=1, default=1,
                            ),
                            "前後半": st.column_config.TextColumn(disabled=True),
                            "ホーム": st.column_config.TextColumn(disabled=True),
                            "アウェイ": st.column_config.TextColumn(disabled=True),
                        },
                        hide_index=True,
                        use_container_width=True,
                    )
                    if st.form_submit_button("✅ この内容を適用", type="primary"):
                        _new_data = []
                        for i, row in _edited.iterrows():
                            if row["消化済み"]:
                                _slot_raw = row["スロット"]
                                _new_data.append({
                                    "match_id": _matches_p[i].match_id,
                                    "home": _matches_p[i].home,
                                    "away": _matches_p[i].away,
                                    "leg": _matches_p[i].leg,
                                    "date": unicodedata.normalize("NFKC", str(row["日付"])).strip() if row["日付"] else "",
                                    "slot": int(_slot_raw) if pd.notna(_slot_raw) else 1,
                                })
                        st.session_state.pre_played_matches[_lname] = _new_data
                        st.rerun()

    if st.button("🗓️ スケジュールを自動生成", type="primary"):
        avails: dict[str, AvailabilityData] = {}
        tmp_dir = Path("data")
        tmp_dir.mkdir(exist_ok=True)

        for lc in leagues_config:
            lname = lc["name"]
            f = uploaded_files[lname]
            tmp = tmp_dir / f.name
            tmp.write_bytes(f.read())
            try:
                avails[lname] = parse_chouseisan_csv(tmp)
            except Exception as e:
                st.error(f"{lname}: CSV読み込みエラー: {e}")
                st.stop()

        # 全対戦リスト（初回生成 or 既存を再利用）
        all_match_store: dict[str, list] = dict(state.get("all_matches", {}))
        league_config_objs: list[LeagueConfig] = []
        # pre_played_matches: {lname: [{match_id, home, away, leg, date, slot}, ...]}
        _pre_played: dict[str, list[dict]] = st.session_state.get("pre_played_matches", {})

        for lc in leagues_config:
            lname = lc["name"]
            avail = avails[lname]

            if lname not in all_match_store:
                raw = generate_matches(avail.teams, double_round=lc["double_round"])
                all_match_store[lname] = [
                    {"home": m.home, "away": m.away, "leg": m.leg, "match_id": m.match_id}
                    for m in raw
                ]

            # 既確定試合を除外（保存済み期 + 手動入力の消化済み）
            played_ids: set[int] = set()
            for p in state.get("periods", []):
                played_ids.update(p.get("scheduled_ids", {}).get(lname, []))
            played_ids.update(d["match_id"] for d in _pre_played.get(lname, []))

            all_matches = [
                Match(home=m["home"], away=m["away"], leg=m["leg"], match_id=m["match_id"])
                for m in all_match_store[lname]
            ]
            remaining = [m for m in all_matches if m.match_id not in played_ids]

            # already_played: 期持ち越し分 + 消化済み入力分
            already = dict(state.get("games_count", {}).get(lname, {}))
            for info in _pre_played.get(lname, []):
                already[info["home"]] = already.get(info["home"], 0) + 1
                already[info["away"]] = already.get(info["away"], 0) + 1

            league_config_objs.append(LeagueConfig(
                name=lname,
                matches=remaining,
                availability=avail,
                already_played=already,
                pace_multiplier=float(lc.get("pace_multiplier", 1.0)),
            ))

        state["all_matches"] = all_match_store
        save_state(state)

        # 全リーグの日程を統合（1つ目CSVの順序を基準に結合）
        first_dates = list(avails[leagues_config[0]["name"]].dates)
        extra_dates = []
        for avail in avails.values():
            for d in avail.dates:
                if d not in first_dates and d not in extra_dates:
                    extra_dates.append(d)
        all_dates = first_dates + extra_dates

        unified_result = assign_multi_league(
            leagues=league_config_objs,
            all_dates=all_dates,
            target_games_per_day=max_games,
        )

        # 消化済み入力分（日付あり）をMultiScheduledMatchに変換してunified_resultに追加
        # CSV日付と消化済み日付を合わせて正規化マップを構築し、日付文字列を統一する
        _all_known_dates = list(all_dates) + [
            str(info["date"]).strip()
            for entries in _pre_played.values()
            for info in entries
            if info.get("date")
        ]
        _canonical_map = _build_canonical_date_map(_all_known_dates)

        _pre_played_sms: list[MultiScheduledMatch] = []
        for lc in leagues_config:
            lname = lc["name"]
            for info in _pre_played.get(lname, []):
                if info.get("date"):
                    _norm_date = _normalize_date(str(info["date"]).strip(), _canonical_map)
                    _pre_played_sms.append(MultiScheduledMatch(
                        match=Match(
                            home=info["home"], away=info["away"],
                            leg=info["leg"], match_id=info["match_id"],
                        ),
                        league_name=lname,
                        date=_norm_date,
                        slot=info.get("slot", 1),
                        use_maybe=False,
                    ))

        if _pre_played_sms:
            _gpd = dict(unified_result.games_per_day)
            for _sm in _pre_played_sms:
                _gpd[_sm.date] = _gpd.get(_sm.date, 0) + 1
            unified_result = MultiAssignResult(
                scheduled=_pre_played_sms + unified_result.scheduled,
                unscheduled=unified_result.unscheduled,
                games_count=unified_result.games_count,
                games_per_day=_gpd,
            )

        # ①全試合を自動確定（消化済み入力分を含む）
        st.session_state.confirmed = {
            lc["name"]: {
                sm.match.match_id
                for sm in unified_result.scheduled
                if sm.league_name == lc["name"]
            }
            for lc in leagues_config
        }
        st.session_state.excluded_dates = set()
        st.session_state.unified_result = unified_result
        st.session_state.avails = avails
        st.session_state.period_name = period_name
        st.session_state.max_games = max_games
        st.session_state.all_dates = all_dates
        save_working_state()
        st.rerun()


# ------------------------------------------------------------------
# 結果表示
# ------------------------------------------------------------------
if "unified_result" not in st.session_state:
    st.stop()

unified_result: MultiAssignResult = st.session_state.unified_result
avails: dict[str, AvailabilityData] = st.session_state.avails
pname: str = st.session_state.period_name
max_games_disp: int = st.session_state.get("max_games", 3)
all_dates_stored: list[str] = st.session_state.get("all_dates", [])
confirmed: dict[str, set[int]] = st.session_state.confirmed
excluded_dates: set[str] = st.session_state.excluded_dates

league_names = [lc["name"] for lc in leagues_config]
icon_map = {lc["name"]: LEAGUE_ICONS[i] for i, lc in enumerate(leagues_config)}


def is_confirmed(sm: MultiScheduledMatch) -> bool:
    return sm.match.match_id in confirmed.get(sm.league_name, set())


def calc_confirmed_gc() -> dict[str, dict[str, int]]:
    gc: dict[str, dict[str, int]] = {}
    for lname, avail in avails.items():
        d = dict(state.get("games_count", {}).get(lname, {}))
        for team in avail.teams:
            d.setdefault(team, 0)
        gc[lname] = d
    for sm in unified_result.scheduled:
        if is_confirmed(sm) and sm.date not in excluded_dates:
            gc[sm.league_name][sm.match.home] = gc[sm.league_name].get(sm.match.home, 0) + 1
            gc[sm.league_name][sm.match.away] = gc[sm.league_name].get(sm.match.away, 0) + 1
    return gc


# サマリメトリクス
confirmed_and_active = [
    sm for sm in unified_result.scheduled
    if is_confirmed(sm) and sm.date not in excluded_dates
]
total_confirmed_cnt = len(confirmed_and_active)
total_excluded = sum(
    1 for sm in unified_result.scheduled if sm.date in excluded_dates
)
total_unscheduled = sum(len(v) for v in unified_result.unscheduled.values())
# 取消済み = 除外日以外で未確定の試合（再生成の対象）
total_cancelled = sum(
    1 for sm in unified_result.scheduled
    if not is_confirmed(sm) and sm.date not in excluded_dates
)

st.header(f"日程確定: {pname}")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("確定済み試合数", total_confirmed_cnt)
c2.metric("取消済み試合数", total_cancelled)
c3.metric("除外日の試合数", total_excluded)
c4.metric("割り当て不可試合数", total_unscheduled)
c5.metric("除外日数", len(excluded_dates))

# リーグ別進捗
with st.expander("リーグ別 進捗"):
    prog_cols = st.columns(len(leagues_config))
    for i, lc in enumerate(leagues_config):
        lname = lc["name"]
        lg_confirmed = sum(
            1 for sm in unified_result.scheduled
            if sm.league_name == lname and is_confirmed(sm) and sm.date not in excluded_dates
        )
        lg_excluded = sum(
            1 for sm in unified_result.scheduled
            if sm.league_name == lname and sm.date in excluded_dates
        )
        lg_unscheduled = len(unified_result.unscheduled.get(lname, []))
        with prog_cols[i]:
            st.markdown(f"**{icon_map[lname]} {lname}**")
            st.metric("確定試合数", lg_confirmed)
            if lg_excluded:
                st.caption(f"🚫 除外: {lg_excluded}試合")
            if lg_unscheduled:
                st.caption(f"⚠️ 未割当: {lg_unscheduled}試合")

# ③ 再生成ボタン
needs_regen = total_cancelled > 0 or total_unscheduled > 0 or total_excluded > 0
if needs_regen:
    st.markdown("---")
    regen_count = total_cancelled + total_unscheduled + total_excluded
    if st.button(f"🔄 取消・除外・未割り当て分を再生成（{regen_count}試合）", type="primary"):
        # 確定済み試合のチームを日付ごとにブロック（試合数も明示的に渡す）
        pre_blocked: dict[str, set[str]] = defaultdict(set)
        pre_blocked_counts: dict[str, int] = defaultdict(int)
        for sm in unified_result.scheduled:
            if is_confirmed(sm) and sm.date not in excluded_dates:
                pre_blocked[sm.date].add(sm.match.home)
                pre_blocked[sm.date].add(sm.match.away)
                pre_blocked_counts[sm.date] += 1

        # 再スケジュール対象:
        #   - 除外日の試合（他の日に再配置）
        #   - 取消済み試合（除外日以外の未確定）
        #   - 元々未割り当て
        # 確定済みの試合は絶対に含めない
        regen_configs = []
        for lc in leagues_config:
            lname = lc["name"]
            confirmed_ids_lg = confirmed.get(lname, set())

            excluded_matches = [
                sm.match for sm in unified_result.scheduled
                if sm.league_name == lname
                and sm.date in excluded_dates
                and sm.match.match_id not in confirmed_ids_lg
            ]
            unconfirmed_matches = [
                sm.match for sm in unified_result.scheduled
                if sm.league_name == lname
                and not is_confirmed(sm)
                and sm.date not in excluded_dates
            ]
            regen_matches = excluded_matches + unconfirmed_matches + list(
                unified_result.unscheduled.get(lname, [])
            )
            # match_id でユニーク化（重複排除）・確定済み二重チェック
            seen_ids: set[int] = set()
            unique_regen: list[Match] = []
            for m in regen_matches:
                if m.match_id not in seen_ids and m.match_id not in confirmed_ids_lg:
                    seen_ids.add(m.match_id)
                    unique_regen.append(m)

            gc_now = calc_confirmed_gc().get(lname, {})
            regen_configs.append(LeagueConfig(
                name=lname,
                matches=unique_regen,
                availability=avails[lname],
                already_played=gc_now,
                pace_multiplier=float(lc.get("pace_multiplier", 1.0)),
            ))

        regen_result = assign_multi_league(
            leagues=regen_configs,
            all_dates=all_dates_stored,
            target_games_per_day=max_games_disp,
            excluded_dates=excluded_dates,
            pre_blocked=dict(pre_blocked),
            pre_blocked_counts=dict(pre_blocked_counts),
        )

        # 確定済み試合を抽出し、regenで再割り当てされたものは除外して重複を防ぐ
        kept = [
            sm for sm in unified_result.scheduled
            if is_confirmed(sm) and sm.date not in excluded_dates
        ]
        regen_keys = {(sm.league_name, sm.match.match_id) for sm in regen_result.scheduled}
        kept = [sm for sm in kept if (sm.league_name, sm.match.match_id) not in regen_keys]

        # マージしてスロット番号を日付ごとに再計算
        # ※除外日の試合は regen_configs に含めて再配置済みのため、ここには含めない
        raw_merged = kept + regen_result.scheduled

        # 年なし/年あり混在を正規化して同じ日は同じ日付文字列に統一
        _regen_canonical = _build_canonical_date_map([sm.date for sm in raw_merged])

        def _regen_norm(d: str) -> tuple:
            k = date_sort_key(str(d).strip())
            max_y = max((ky[0] for ky in _regen_canonical.keys() if ky[0] > 0), default=0)
            return (max_y, k[1], k[2]) if k[0] == 0 and max_y > 0 else k

        date_counters: dict[tuple, int] = defaultdict(int)
        merged_scheduled = []
        for sm in sorted(raw_merged, key=lambda x: (_regen_norm(x.date), x.slot)):
            nk = _regen_norm(sm.date)
            date_counters[nk] += 1
            merged_scheduled.append(MultiScheduledMatch(
                match=sm.match,
                league_name=sm.league_name,
                date=_regen_canonical.get(nk, sm.date),  # 正規化した日付文字列に統一
                slot=date_counters[nk],
                use_maybe=sm.use_maybe,
            ))

        gpd: dict[str, int] = defaultdict(int)
        for sm in merged_scheduled:
            gpd[sm.date] += 1

        new_result = MultiAssignResult(
            scheduled=merged_scheduled,
            unscheduled=regen_result.unscheduled,
            games_count=regen_result.games_count,
            games_per_day=dict(gpd),
        )

        # 新規割り当て分も自動確定
        new_confirmed = {lname: set(ids) for lname, ids in confirmed.items()}
        for sm in regen_result.scheduled:
            new_confirmed.setdefault(sm.league_name, set()).add(sm.match.match_id)

        st.session_state.unified_result = new_result
        st.session_state.confirmed = new_confirmed
        save_working_state()
        st.success(f"再生成完了: {len(regen_result.scheduled)}試合を新たに割り当てました。")
        st.rerun()

st.markdown("---")

# 消化済み試合は一覧表示しない
_pre_played_ids: set[tuple[str, int]] = {
    (lname, d["match_id"])
    for lname, entries in st.session_state.get("pre_played_matches", {}).items()
    for d in entries
}

# 日付ごとの試合一覧
by_date: dict[str, list[MultiScheduledMatch]] = defaultdict(list)
for sm in unified_result.scheduled:
    if (sm.league_name, sm.match.match_id) not in _pre_played_ids:
        by_date[sm.date].append(sm)

for date in sorted(by_date.keys(), key=date_sort_key):
    matches = sorted(by_date[date], key=lambda sm: sm.slot)
    is_excluded = date in excluded_dates

    if is_excluded:
        # 除外日: 折りたたんで表示
        col_hd, col_btn = st.columns([6, 2])
        with col_hd:
            st.markdown(f"**🚫 {date}**　（除外中）")
        with col_btn:
            if st.button("除外を解除", key=f"unex_{date}"):
                excluded_dates.discard(date)
                # 除外解除 → 再確定
                for sm in matches:
                    confirmed.setdefault(sm.league_name, set()).add(sm.match.match_id)
                st.rerun()
        st.markdown("---")
        continue

    n = len(matches)
    warn_str = "　⚠️目標未達" if n < max_games_disp else ""

    col_hd, col_btn = st.columns([6, 2])
    with col_hd:
        st.markdown(f"**✅ {date}**　{n}試合{warn_str}")
    with col_btn:
        if st.button("この日を除外", key=f"ex_{date}"):
            excluded_dates.add(date)
            for sm in matches:
                confirmed.get(sm.league_name, set()).discard(sm.match.match_id)
            st.rerun()

    # 列ヘッダー
    hc = st.columns([1, 2, 5, 5, 2, 2])
    for col_w, label in zip(hc, ["スロット", "リーグ", "ホーム（H）", "アウェイ（A）", "前後半", ""]):
        col_w.markdown(f"<small><b>{label}</b></small>", unsafe_allow_html=True)

    for sm in matches:
        lname = sm.league_name
        lg_icon = icon_map.get(lname, "⚪")
        leg_label = "前半戦" if sm.match.leg == 1 else "後半戦"
        maybe_tag = " 🔺" if sm.use_maybe else ""
        this_confirmed = is_confirmed(sm)

        col1, col2, col3, col4, col5, col6 = st.columns([1, 2, 5, 5, 2, 2])
        with col1:
            st.write(f"第{sm.slot}試合")
        with col2:
            st.write(f"{lg_icon} {lname}")
        with col3:
            st.write(sm.match.home + maybe_tag)
        with col4:
            st.write(sm.match.away)
        with col5:
            st.write(leg_label)
        with col6:
            key_base = f"{lname}_{sm.match.match_id}"
            if this_confirmed:
                if st.button("取消", key=f"unc_{key_base}"):
                    confirmed[lname].discard(sm.match.match_id)
                    st.rerun()
            else:
                if st.button("確定", key=f"con_{key_base}", type="primary"):
                    confirmed.setdefault(lname, set()).add(sm.match.match_id)
                    st.rerun()

    st.markdown("---")

# ------------------------------------------------------------------
# チーム別消化数 / Excelダウンロード / 期保存
# ------------------------------------------------------------------
if total_confirmed_cnt > 0:
    with st.expander("確定済み消化試合数（リーグ・チーム別）"):
        gc = calc_confirmed_gc()
        summary_cols = st.columns(len(leagues_config))
        for i, lc in enumerate(leagues_config):
            lname = lc["name"]
            with summary_cols[i]:
                st.markdown(f"**{icon_map[lname]} {lname}**")
                df = pd.DataFrame(
                    gc[lname].items(), columns=["チーム", "消化数"]
                ).sort_values("消化数", ascending=False)
                st.dataframe(df, use_container_width=True, hide_index=True)

    confirmed_scheduled = confirmed_and_active
    confirmed_unscheduled = {
        lname: unified_result.unscheduled.get(lname, []) + [
            sm.match for sm in unified_result.scheduled
            if sm.league_name == lname and sm.date in excluded_dates
        ]
        for lname in league_names
    }

    try:
        excel_bytes = build_multi_excel(
            scheduled=confirmed_scheduled,
            unscheduled=confirmed_unscheduled,
            avails=avails,
            leagues_config=leagues_config,
            period_name=pname,
        )
    except Exception as _ex:
        import traceback
        st.error(f"Excel生成エラー: {_ex}")
        st.code(traceback.format_exc())
        st.caption(f"confirmed_scheduled件数: {len(confirmed_scheduled)}, availsキー: {list(avails.keys())}")
        st.stop()

    # ファイルに保存してパスを表示（ダウンロードボタンが動かない環境向けフォールバック）
    _safe_pname = pname.replace("/", "-").replace("\\", "-").replace(":", "-")
    _excel_path = Path("data") / f"schedule_{_safe_pname}.xlsx"
    _excel_path.write_bytes(excel_bytes)

    dl_col, save_col = st.columns([3, 1])
    with dl_col:
        st.download_button(
            label=f"📥 確定済み試合をExcelダウンロード（{len(excel_bytes):,} bytes）",
            data=excel_bytes,
            file_name=f"schedule_{_safe_pname}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        st.caption(f"ダウンロードできない場合はこちらを開いてください: `{_excel_path.resolve()}`")
    with save_col:
        if st.button("💾 作業状態を保存", help="確定・除外の状態をファイルに保存します。次回起動時に復元できます。"):
            save_working_state()
            st.success("保存しました")

    st.markdown("---")
    excluded_cnt = sum(
        1 for sm in unified_result.scheduled if sm.date in excluded_dates
    )
    carry = total_unscheduled + excluded_cnt
    note = f"（除外・未割り当て計{carry}試合は次期に持ち越し）" if carry else ""
    if st.button(f"✅ この期を保存して完了　{note}", type="secondary"):
        state["periods"].append({
            "name": pname,
            "scheduled_ids": {
                lname: [sm.match.match_id for sm in confirmed_and_active if sm.league_name == lname]
                for lname in league_names
            },
        })
        state["games_count"] = calc_confirmed_gc()
        state.pop("current_period", None)   # 作業状態をクリア
        save_state(state)
        for key in ["unified_result", "avails", "confirmed", "excluded_dates",
                    "period_name", "max_games", "all_dates", "pre_played_matches"]:
            st.session_state.pop(key, None)
        st.success(f"「{pname}」を保存しました。次の期のCSVをアップロードしてください。")
        st.rerun()

else:
    st.info("確定済みの試合がありません。除外を解除するか再生成してください。")
