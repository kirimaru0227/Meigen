from flask import Flask, render_template, jsonify, request, redirect, url_for, make_response, send_file, abort, session, flash
import secrets
from datetime import datetime, timezone, timedelta
import time
import threading
import calendar as calendar_module
import re
import traceback
import json
import uuid
import sqlite3
import io
import zipfile
from pathlib import Path

import jpholiday

import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
# 録音管理画面の設定：運用前に必ず自分だけが知る値へ変更してください。
RECORDING_ADMIN_PASSWORD = "Meigen123"
FLASK_SESSION_SECRET = "nidc2i39nfow19dnocms269"
app.secret_key = FLASK_SESSION_SECRET
# 管理者ログインをブラウザ終了後も維持する（ログアウト操作またはCookie削除まで）
app.permanent_session_lifetime = timedelta(days=3650)


# ==================================================
# アクセス解析（Neon PostgreSQL）
# ==================================================

import os
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import DictCursor

PAGE_ANALYTICS_LABELS = {
    "home": "ホーム", "special_timetable": "特別時間割", "schedule": "時間割",
    "timetable_page": "駅時刻表", "train_detail": "列車時刻表", "settings": "設定",
    "operation_page": "時刻表", "calendar_page": "カレンダー", "more_page": "その他",
    "map_page": "地図", "range_page": "範囲表", "line_page": "路線",
    "roster_page": "名簿", "recording_page": "録音", "quiz_page": "クイズ",
    "stamp_page": "スタンプ", "video_page": "動画", "calculator_page": "電卓",
    "calculator_history_page": "電卓履歴", "unit_converter_page": "単位変換",
    "admin_dashboard": "管理者用ページ", "form_page": "アンケート", "clock page": "時計", "database page": "データベース"
}


class AnalyticsConnection:
    """psycopg2接続をSQLiteに近い execute / fetch 形式で扱う。"""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=None):
        cursor = self.connection.cursor()
        cursor.execute(query, params or ())
        return cursor


@contextmanager
def analytics_connect():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Renderの環境変数 DATABASE_URL が設定されていません。")
    raw_connection = psycopg2.connect(
        database_url, connect_timeout=10, cursor_factory=DictCursor
    )
    try:
        yield AnalyticsConnection(raw_connection)
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        raw_connection.close()


def init_analytics_db():
    with analytics_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS page_views (
                id BIGSERIAL PRIMARY KEY,
                page TEXT NOT NULL,
                visited_at TEXT NOT NULL,
                visitor_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS button_clicks (
                id BIGSERIAL PRIMARY KEY,
                page TEXT NOT NULL,
                button TEXT NOT NULL,
                clicked_at TEXT NOT NULL,
                visitor_id TEXT
            )
        """)
        # 既存テーブルにも利用者ID列を追加（既存データは保持）
        for table in ("page_views", "button_clicks"):
            columns = {
                row["column_name"]
                for row in conn.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema = current_schema() AND table_name = %s""",
                    (table,),
                ).fetchall()
            }
            if "visitor_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN visitor_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_visitor ON page_views(visitor_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_page ON page_views(page)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_button_clicks_visitor ON button_clicks(visitor_id)")


def analytics_page_name(endpoint):
    if endpoint == "timetable_page":
        station = request.args.get("station", "").strip()
        line_id = request.args.get("line", "") or selected_route()
        line_name = TIMETABLE_LINE_CONFIG.get(line_id, {}).get("name", "時刻表")
        if station:
            return f"{line_name} {station}駅時刻表"
        return f"時刻表（{line_name}・駅未選択）"
    if endpoint == "train_detail":
        train_number = (request.view_args or {}).get("train_number", "")
        if train_number:
            return f"{str(train_number).rstrip(chr(77))}M 列車時刻表"
        return "列車時刻表"
    if endpoint in PAGE_ANALYTICS_LABELS:
        return PAGE_ANALYTICS_LABELS[endpoint]
    return (endpoint or "不明").replace("_", " ")


@app.context_processor
def inject_analytics_page_name():
    return {"analytics_current_page": analytics_page_name(request.endpoint)}


def analytics_owner_excluded():
    return bool(session.get("recording_admin_authenticated"))


def analytics_now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def analytics_get_visitor_id():
    value = request.cookies.get("myHomeAnalyticsVisitor", "")
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid4())


def analytics_set_visitor_cookie(response, visitor_id):
    response.set_cookie(
        "myHomeAnalyticsVisitor", visitor_id,
        max_age=60 * 60 * 24 * 730,
        httponly=True, samesite="Lax", secure=request.is_secure,
    )
    return response


init_analytics_db()


@app.after_request
def record_analytics_page_view(response):
    endpoint = request.endpoint or ""
    should_track = (
        request.method == "GET"
        and response.status_code < 400
        and response.mimetype == "text/html"
        and not endpoint.startswith("static")
        and not endpoint.startswith("api_")
        and endpoint not in {"recording_admin_login", "recording_admin_logout", "analytics_page"}
        and not analytics_owner_excluded()
    )
    if should_track:
        visitor_id = analytics_get_visitor_id()
        try:
            with analytics_connect() as conn:
                conn.execute(
                    "INSERT INTO page_views (page, visited_at, visitor_id) VALUES (%s, %s, %s)",
                    (analytics_page_name(endpoint), analytics_now(), visitor_id),
                )
        except Exception:
            app.logger.exception("アクセス解析（ページ）の記録に失敗しました")
        analytics_set_visitor_cookie(response, visitor_id)
    return response


@app.route("/api/analytics/click", methods=["POST"])
def analytics_record_click():
    if analytics_owner_excluded():
        return jsonify({"ok": True, "ignored": True})
    data = request.get_json(silent=True) or {}
    page = str(data.get("page", "不明")).strip()[:80] or "不明"
    button = str(data.get("button", "名称未設定")).strip()[:120] or "名称未設定"
    if page in {"管理者用ページ", "アクセス解析"}:
        return jsonify({"ok": True, "ignored": True})
    visitor_id = analytics_get_visitor_id()
    try:
        with analytics_connect() as conn:
            conn.execute(
                "INSERT INTO button_clicks (page, button, clicked_at, visitor_id) VALUES (%s, %s, %s, %s)",
                (page, button, analytics_now(), visitor_id),
            )
        response = jsonify({"ok": True})
        return analytics_set_visitor_cookie(response, visitor_id)
    except Exception:
        app.logger.exception("アクセス解析（クリック）の記録に失敗しました")
        return jsonify({"ok": False}), 500


@app.route("/admin/analytics", methods=["GET", "POST"])
def analytics_page():
    if not session.get("recording_admin_authenticated"):
        return redirect(url_for("recording_admin_login", next=url_for("analytics_page")))
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "delete_visitor":
            visitor_id = request.form.get("visitor_id", "").strip()
            if visitor_id:
                with analytics_connect() as conn:
                    page_result = conn.execute(
                        "DELETE FROM page_views WHERE visitor_id = %s", (visitor_id,)
                    )
                    click_result = conn.execute(
                        "DELETE FROM button_clicks WHERE visitor_id = %s", (visitor_id,)
                    )
                deleted_total = page_result.rowcount + click_result.rowcount
                flash(
                    f"利用者 #{visitor_id[:8]} のアクセス履歴・クリック履歴を削除しました。"
                    if deleted_total else "該当する利用者の履歴は見つかりませんでした。"
                )
            return redirect(url_for(
                "analytics_page",
                period=request.form.get("period", "all")
                if request.form.get("period") in {"all", "today"} else "all"
            ))
        if action == "reset":
            with analytics_connect() as conn:
                conn.execute("DELETE FROM page_views")
                conn.execute("DELETE FROM button_clicks")
            flash("アクセス解析データをリセットしました。")
            return redirect(url_for("analytics_page"))

    period = request.args.get("period", "all")
    if period not in {"all", "today"}:
        period = "all"
    today_start = datetime.now(JST).strftime("%Y-%m-%d") + " 00:00:00"
    page_where = "WHERE visited_at >= %s" if period == "today" else ""
    click_where = "WHERE clicked_at >= %s" if period == "today" else ""
    page_params = (today_start,) if period == "today" else ()
    click_params = (today_start,) if period == "today" else ()

    with analytics_connect() as conn:
        total_views = conn.execute(
            f"SELECT COUNT(*) FROM page_views {page_where}", page_params
        ).fetchone()[0]
        total_clicks = conn.execute(
            f"SELECT COUNT(*) FROM button_clicks {click_where}", click_params
        ).fetchone()[0]
        visitor_count = conn.execute(
            f"SELECT COUNT(DISTINCT visitor_id) FROM page_views {page_where} "
            + ("AND" if page_where else "WHERE") + " visitor_id IS NOT NULL",
            page_params,
        ).fetchone()[0]
        page_rows = conn.execute(f"""
            SELECT page, COUNT(*) AS count, COUNT(DISTINCT visitor_id) AS visitors
            FROM page_views {page_where} GROUP BY page ORDER BY count DESC, page
        """, page_params).fetchall()
        click_rows = conn.execute(f"""
            SELECT page, button, COUNT(*) AS count, COUNT(DISTINCT visitor_id) AS visitors
            FROM button_clicks {click_where} GROUP BY page, button
            ORDER BY count DESC, page, button
        """, click_params).fetchall()
        visitor_click_rows = conn.execute(f"""
            SELECT visitor_id, COUNT(*) AS clicks, MAX(clicked_at) AS last_clicked,
                   STRING_AGG(DISTINCT page || '：' || button, ',') AS buttons
            FROM button_clicks {click_where}
            {"AND" if click_where else "WHERE"} visitor_id IS NOT NULL
            GROUP BY visitor_id ORDER BY last_clicked DESC
        """, click_params).fetchall()
        recent_rows = conn.execute(f"""
            SELECT page, visited_at, visitor_id FROM page_views {page_where}
            ORDER BY id DESC
        """, page_params).fetchall()
        recent_click_rows = conn.execute(f"""
            SELECT page, button, clicked_at, visitor_id FROM button_clicks {click_where}
            ORDER BY id DESC
        """, click_params).fetchall()
        visitor_rows = conn.execute(f"""
            SELECT pv.visitor_id,
                   COUNT(DISTINCT pv.id) AS visits,
                   COUNT(DISTINCT bc.id) AS clicks,
                   MAX(pv.visited_at) AS last_seen,
                   STRING_AGG(DISTINCT pv.page, ',') AS pages
            FROM page_views pv
            LEFT JOIN button_clicks bc ON bc.visitor_id = pv.visitor_id
                {"AND bc.clicked_at >= %s" if period == "today" else ""}
            {"WHERE pv.visited_at >= %s AND pv.visitor_id IS NOT NULL" if period == "today" else "WHERE pv.visitor_id IS NOT NULL"}
            GROUP BY pv.visitor_id ORDER BY last_seen DESC
        """, ((today_start, today_start) if period == "today" else ())).fetchall()
    return render_template(
        "analytics.html",
        total_views=total_views,
        total_clicks=total_clicks,
        visitor_count=visitor_count,
        page_rows=page_rows,
        click_rows=click_rows,
        visitor_click_rows=visitor_click_rows,
        recent_rows=recent_rows,
        recent_click_rows=recent_click_rows,
        visitor_rows=visitor_rows,
        period=period,
    )

JST = timezone(timedelta(hours=9))
WEEKDAYS = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日"]


# ==================================================
# 時間割
# ==================================================

CLASS_SCHEDULES = {
    "1組": {
        "月曜日": ["文学国語", "コミⅡ", "情報Ⅰ", "保健", "日本史/世界史", "数学ⅡBC", "論理国語"],
        "火曜日": ["数学ⅡBC", "数学ⅡBC", "日本史/世界史", "地理総合", "体育", "コミⅡ", "化学基礎"],
        "水曜日": ["古典探究", "情報Ⅰ", "数学ⅡBC", "論理表現Ⅱ", "総探", "総探"],
        "木曜日": ["地理総合", "数学ⅡBC", "論理国語", "論理表現Ⅱ", "コミⅡ", "体育", "日本史/世界史"],
        "金曜日": ["文学国語", "古典探究", "化学基礎", "日本史/世界史", "コミⅡ", "数学ⅡBC", "LHR"],
    },
    "2組": {
        "月曜日": ["論理国語", "コミⅡ", "文学国語", "情報Ⅰ", "日本史/世界史", "数学ⅡBC", "化学基礎"],
        "火曜日": ["数学ⅡBC", "数学ⅡBC", "日本史/世界史", "古典探究", "体育", "コミⅡ", "文学国語"],
        "水曜日": ["地理総合", "古典探究", "数学ⅡBC", "論理表現Ⅱ", "総探", "総探"],
        "木曜日": ["化学基礎", "数学ⅡBC", "保健", "論理表現Ⅱ", "コミⅡ", "体育", "日本史/世界史"],
        "金曜日": ["情報Ⅰ", "地理総合", "論理国語", "日本史/世界史", "コミⅡ", "数学ⅡBC", "LHR"],
    },
    "3組": {
        "月曜日": ["数学ⅡBC", "保健", "論理国語", "文学探究A", "コミⅡ", "化学基礎", "地理総合"],
        "火曜日": ["地理総合", "生物/物理", "コミⅡ", "数学ⅡBC", "情報Ⅰ", "体育", "化学基礎"],
        "水曜日": ["数学ⅡBC", "地理総合", "論理表現Ⅱ", "化学基礎", "総探", "総探"],
        "木曜日": ["化学基礎", "コミⅡ", "文学探究A", "体育", "数学ⅡBC", "生物/物理", "論理国語"],
        "金曜日": ["数学ⅡBC", "数学ⅡBC", "論理表現Ⅱ", "コミⅡ", "生物/物理", "情報Ⅰ", "LHR"],
    },
    "4組": {
        "月曜日": ["数学ⅡBC", "論理国語", "化学基礎", "地理総合", "コミⅡ", "文学探究A", "保健"],
        "火曜日": ["生物/物理", "情報Ⅰ", "コミⅡ", "数学ⅡBC", "文学探究A", "体育", "地理総合"],
        "水曜日": ["数学ⅡBC", "化学基礎", "論理表現Ⅱ", "情報Ⅰ", "総探", "総探"],
        "木曜日": ["論理国語", "コミⅡ", "地理総合", "体育", "数学ⅡBC", "化学基礎", "生物/物理"],
        "金曜日": ["数学ⅡBC", "数学ⅡBC", "論理表現Ⅱ", "コミⅡ", "化学基礎", "生物/物理", "LHR"],
    },
}

CLASS_NAMES = list(CLASS_SCHEDULES.keys())

# 日付ごとの特別時間割（アプリと同じフォルダのJSONに保存）
SPECIAL_SCHEDULE_FILE = Path(__file__).resolve().parent / "special_schedule.json"

def load_special_schedule():
    try:
        data = json.loads(SPECIAL_SCHEDULE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def save_special_schedule(data):
    SPECIAL_SCHEDULE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ==================================================
# 路線・天気の設定
# ==================================================

ROUTE_OPTIONS = {
    "tohoku_shinkansen": "東北新幹線",
    "utsunomiya": "JR宇都宮線",
    "ueno_tokyo": "上野東京ライン",
    "shonan_shinjuku": "湘南新宿ライン",
    "ryomo": "両毛線",
    "tobu_nikko": "東武日光線",
    "tobu_utsunomiya": "東武宇都宮線",
    "tobu_isesaki": "東武伊勢崎線",
    "tobu_sano": "東武佐野線",
}

WEATHER_OPTIONS = {
    "utsunomiya": "宇都宮市",
    "oyama": "小山市",
    "tochigi": "栃木市",
    "sano": "佐野市",
    "ashikaga": "足利市",
}


# ==================================================
# 運行情報の設定
# ==================================================

OPERATION_SOURCE_URL = "https://transit.yahoo.co.jp/diainfo/area/4"

OPERATION_SOURCE_URLS = {
    "tohoku_shinkansen": "https://transit.yahoo.co.jp/diainfo/1/0",
    "utsunomiya": "https://transit.yahoo.co.jp/diainfo/46/46",
    "ueno_tokyo": "https://transit.yahoo.co.jp/diainfo/627/0",
    "shonan_shinjuku": "https://transit.yahoo.co.jp/diainfo/25/0",
    "ryomo": "https://transit.yahoo.co.jp/diainfo/168/0",
    "tobu_nikko": "https://transit.yahoo.co.jp/diainfo/80/0",
    "tobu_utsunomiya": "https://transit.yahoo.co.jp/diainfo/169/0",
    "tobu_isesaki": "https://transit.yahoo.co.jp/diainfo/639/0",
    "tobu_sano": "https://transit.yahoo.co.jp/diainfo/172/0",
}

OPERATION_ROUTE_MATCHES = {
    "tohoku_shinkansen": ("東北新幹線",),
    "utsunomiya": (
        "宇都宮線[東京～宇都宮]",
        "宇都宮線[東京〜宇都宮]",
        "宇都宮線",
    ),
    "ueno_tokyo": ("上野東京ライン",),
    "shonan_shinjuku": ("湘南新宿ライン",),
    "ryomo": ("両毛線",),
    "tobu_nikko": (
        "東武日光線[南栗橋～東武日光]",
        "東武日光線[南栗橋〜東武日光]",
        "東武日光線",
    ),
    "tobu_utsunomiya": ("東武宇都宮線",),
    "tobu_isesaki": (
        "東武伊勢崎線[久喜～伊勢崎]",
        "東武伊勢崎線[久喜〜伊勢崎]",
        "東武伊勢崎線",
        "東武スカイツリーライン",
    ),
    "tobu_sano": ("東武佐野線",),
}

OPERATION_CACHE = {}
OPERATION_CACHE_SECONDS = 50

OPERATION_ALERT_CACHE = {
    "cached_at": 0,
    "data": [],
}
OPERATION_ALERT_CACHE_SECONDS = 50

OPERATION_LOCK = threading.Lock()


# ==================================================
# 学校行事
# ==================================================

SCHOOL_EVENTS = [
    {"date": "2026-4-27", "title": "内科検診", "detail": ""},
    {"date": "2026-4-29", "title": "水①～⑤授業", "detail": ""},
    {"date": "2026-4-29", "title": "⑤授業参観", "detail": ""},
    {"date": "2026-4-29", "title": "PTA総会", "detail": ""},
    {"date": "2026-4-30", "title": "新体力テスト･身体計測", "detail": ""},
    {"date": "2026-4-30", "title": "交通安全教室", "detail": ""},
    {"date": "2026-4-30", "title": "生徒総会", "detail": ""},
    {"date": "2026-5-1", "title": "PTA総会代休", "detail": ""},
    {"date": "2026-5-2", "title": "吹奏楽部第18回定期演奏会", "detail": ""},
    {"date": "2026-5-2", "title": "吹奏楽部第18回定期演奏会", "detail": ""},
    {"date": "2026-5-7", "title": "いじめ･教育相談アンケート", "detail": ""},
    {"date": "2026-5-9", "title": "自習室空調工事", "detail": ""},
    {"date": "2026-5-10", "title": "自習室空調工事", "detail": ""},
    {"date": "2026-5-12", "title": "眼科検診", "detail": ""},
    {"date": "2026-5-12", "title": "腎臓健診1次2次(該当者)", "detail": ""},
    {"date": "2026-5-15", "title": "授業①②", "detail": ""},
    {"date": "2026-5-15", "title": "創立記念式典", "detail": ""},
    {"date": "2026-5-15", "title": "創立記念講演会⑤⑥", "detail": ""},
    {"date": "2026-5-16", "title": "自習室空調工事", "detail": ""},
    {"date": "2026-5-16", "title": "第2回PTA本部委員会", "detail": ""},
    {"date": "2026-5-17", "title": "自習室空調工事(全停電)", "detail": ""},
    {"date": "2026-5-19", "title": "1学期中間試験(1日目)", "detail": ""},
    {"date": "2026-5-20", "title": "1学期中間試験(2日目)", "detail": ""},
    {"date": "2026-5-21", "title": "1学期中間試験(3日目)", "detail": ""},
    {"date": "2026-5-22", "title": "学校公開代休", "detail": ""},
    {"date": "2026-5-24", "title": "学校公開", "detail": ""},
    {"date": "2026-5-26", "title": "大学SG講座⑤⑥", "detail": ""},
    {"date": "2026-5-26", "title": "職員会議", "detail": ""},
    {"date": "2026-5-27", "title": "歯科検診❶(午前)", "detail": ""},
    {"date": "2026-6-1", "title": "いじめ･教育相談アンケート", "detail": ""},
    {"date": "2026-6-1", "title": "運動会準備⑦", "detail": ""},
    {"date": "2026-6-2", "title": "旭城大運動会", "detail": ""},
    {"date": "2026-6-3", "title": "臨時休校", "detail": ""},
    {"date": "2026-6-4", "title": "歯科検診❷", "detail": ""},
    {"date": "2026-6-5", "title": "腎臓健診2次(該当者)", "detail": ""},
    {"date": "2026-6-10", "title": "40分授業", "detail": ""},
    {"date": "2026-6-10", "title": "リーダーズシンポジウム", "detail": ""},
    {"date": "2026-6-11", "title": "耳鼻科検診❶", "detail": ""},
    {"date": "2026-6-18", "title": "耳鼻科検診❷", "detail": ""},
    {"date": "2026-6-19", "title": "第2回教科主任会", "detail": ""},
    {"date": "2026-6-24", "title": "職員会議", "detail": ""},
    {"date": "2026-6-29", "title": "1学期期末試験(1日目)", "detail": ""},
    {"date": "2026-6-30", "title": "1学期期末試験(2日目)", "detail": ""},
    {"date": "2026-7-1", "title": "1学期期末試験(3日目)", "detail": ""},
    {"date": "2026-7-2", "title": "1学期期末試験(4日目)", "detail": ""},
    {"date": "2026-7-3", "title": "フィールドワーク", "detail": ""},
    {"date": "2026-7-4", "title": "進研模試", "detail": ""},
    {"date": "2026-7-7", "title": "第3回PTA本部役員会", "detail": ""},
    {"date": "2026-7-9", "title": "学校運営協議会", "detail": ""},
    {"date": "2026-7-10", "title": "第1回授業評価アンケート", "detail": ""},
    {"date": "2026-7-11", "title": "土曜講座❷", "detail": ""},
    {"date": "2026-7-13", "title": "生徒会立会演説会", "detail": ""},
    {"date": "2026-7-13", "title": "成績個票", "detail": ""},
    {"date": "2026-7-15", "title": "木曜日課", "detail": ""},
    {"date": "2026-7-16", "title": "水曜日課", "detail": ""},
    {"date": "2026-7-16", "title": "職員会議", "detail": ""},
    {"date": "2026-7-21", "title": "1学期終業式", "detail": ""},
    {"date": "2026-7-22", "title": "前期課外", "detail": ""},
    {"date": "2026-7-23", "title": "前期課外", "detail": ""},
    {"date": "2026-7-24", "title": "前期課外", "detail": ""},
    {"date": "2026-7-27", "title": "前期課外", "detail": ""},
    {"date": "2026-7-28", "title": "前期課外", "detail": ""},
    {"date": "2026-7-29", "title": "東北大学オープンキャンパス", "detail": ""},
    {"date": "2026-7-30", "title": "東北大学オープンキャンパス", "detail": ""},
    {"date": "2026-7-31", "title": "前期課外", "detail": ""},
    {"date": "2026-8-6", "title": "新潟大学オープンキャンパス", "detail": ""},
    {"date": "2026-8-8", "title": "栃木県吹奏楽コンクール", "detail": ""},
    {"date": "2026-8-12", "title": "学校閉庁日", "detail": ""},
    {"date": "2026-8-13", "title": "学校閉庁日", "detail": ""},
    {"date": "2026-8-14", "title": "学校閉庁日", "detail": ""},
    {"date": "2026-8-18", "title": "後期課外(1日目)", "detail": ""},
    {"date": "2026-8-18", "title": "卒業生との交流会", "detail": ""},
    {"date": "2026-8-19", "title": "後期課外(2日目)", "detail": ""},
    {"date": "2026-8-20", "title": "1日体験学習", "detail": ""},
    {"date": "2026-8-21", "title": "後期課外(3日目)", "detail": ""},
    {"date": "2026-8-22", "title": "全統記述模試", "detail": ""},
    {"date": "2026-8-26", "title": "2学期始業式", "detail": ""},
    {"date": "2026-8-26", "title": "いじめ･教育相談アンケート", "detail": ""},
    {"date": "2026-8-26", "title": "旭城祭準備(午後)", "detail": ""},
    {"date": "2026-8-26", "title": "職員会議", "detail": ""},
    {"date": "2026-8-27", "title": "旭城祭準備 授業なし", "detail": ""},
    {"date": "2026-8-28", "title": "旭城祭 開祭式", "detail": ""},
    {"date": "2026-8-28", "title": "旭城祭 校内公開", "detail": ""},
    {"date": "2026-8-29", "title": "旭城祭 一般公開", "detail": ""},
    {"date": "2026-8-31", "title": "旭城祭 代休", "detail": ""},
    {"date": "2026-9-1", "title": "定期試験日課", "detail": ""},
    {"date": "2026-9-1", "title": "旭城祭 閉祭式", "detail": ""},
    {"date": "2026-9-2", "title": "定期試験日課", "detail": ""},
    {"date": "2026-9-3", "title": "定期試験日課", "detail": ""},
    {"date": "2026-9-4", "title": "定期試験日課", "detail": ""},
    {"date": "2026-9-4", "title": "性教育講話⑦", "detail": ""},
    {"date": "2026-9-8", "title": "第4回教科主任会", "detail": ""},
    {"date": "2026-9-10", "title": "実力テスト(1日目)", "detail": ""},
    {"date": "2026-9-11", "title": "実力テスト(2日目)", "detail": ""},
    {"date": "2026-9-12", "title": "土曜講座❸", "detail": ""},
    {"date": "2026-9-16", "title": "職員会議", "detail": ""},
    {"date": "2026-9-18", "title": "保護者会", "detail": ""},
    {"date": "2026-10-7", "title": "2学期中間試験(1日目)", "detail": ""},
    {"date": "2026-10-8", "title": "2学期中間試験(2日目)", "detail": ""},
    {"date": "2026-10-9", "title": "2学期中間試験(3日目)", "detail": ""},
    {"date": "2026-10-13", "title": "午前中授業", "detail": ""},
    {"date": "2026-10-14", "title": "修学旅行(1日目)", "detail": ""},
    {"date": "2026-10-15", "title": "修学旅行(2日目)", "detail": ""},
    {"date": "2026-10-16", "title": "修学旅行(3日目)", "detail": ""},
    {"date": "2026-10-17", "title": "修学旅行(4日目)", "detail": ""},
    {"date": "2026-10-19", "title": "代休", "detail": ""},
    {"date": "2026-10-27", "title": "心肺蘇生講習会", "detail": ""},
    {"date": "2026-10-28", "title": "⑤⑥SG大学講座", "detail": ""},
    {"date": "2026-10-28", "title": "第4回教科主任会", "detail": ""},
    {"date": "2026-10-29", "title": "40分授業", "detail": ""},
    {"date": "2026-10-30", "title": "進研模試(放課後)", "detail": ""},
    {"date": "2026-10-31", "title": "進研模試", "detail": ""},
    {"date": "2026-11-02", "title": "いじめ・教育相談アンケート", "detail": ""},
    {"date": "2026-11-04", "title": "⑤⑥講演会", "detail": ""},
    {"date": "2026-11-11", "title": "職員会議", "detail": ""},
    {"date": "2026-11-13", "title": "旭城駅伝大会", "detail": ""},
    {"date": "2026-11-17", "title": "旭城駅伝大会 予備日", "detail": ""},
    {"date": "2026-11-24", "title": "2学期期末試験 1日目", "detail": ""},
    {"date": "2026-11-25", "title": "2学期期末試験 2日目", "detail": ""},
    {"date": "2026-11-26", "title": "2学期期末試験 3日目", "detail": ""},
    {"date": "2026-11-27", "title": "2学期期末試験 4日目", "detail": ""},
    {"date": "2026-11-28", "title": "第6回教科主任会", "detail": ""},
    {"date": "2026-12-01", "title": "いじめ・教育相談アンケート", "detail": ""},
    {"date": "2026-12-04", "title": "第2回授業評価アンケート", "detail": ""},
    {"date": "2026-12-11", "title": "芸術鑑賞会(大平文化会館)", "detail": ""},
    {"date": "2026-12-12", "title": "土曜講座❹", "detail": ""},
    {"date": "2026-12-16", "title": "第7回教科主任会", "detail": ""},
    {"date": "2026-12-16", "title": "職員会議", "detail": ""},
    {"date": "2026-12-18", "title": "代休", "detail": ""},
    {"date": "2026-12-19", "title": "課題研究発表会(午前)", "detail": ""},
    {"date": "2026-12-19", "title": "人権教育(午後)", "detail": ""},
    {"date": "2026-12-23", "title": "終業式", "detail": ""},

]


# ==================================================
# 共通設定
# ==================================================

def selected_appearance():
    value = request.cookies.get("myHomeAppearance", "system")
    return value if value in ("light", "dark", "system") else "system"


@app.context_processor
def inject_appearance():
    return {"appearance": selected_appearance()}




def selected_class():
    value = request.cookies.get("myHomeClass")
    return value if value in CLASS_SCHEDULES else None


def selected_route():
    value = request.cookies.get("myHomeRoute", "ryomo")
    return value if value in ROUTE_OPTIONS else "ryomo"


def selected_weather_place():
    value = request.cookies.get("myHomeWeather", "sano")
    return value if value in WEATHER_OPTIONS else "sano"


def filtered_events(now):
    """ホームの「今週の予定」用に学校行事・祝日・個人予定をまとめる。"""
    start = now.date()
    end = start + timedelta(days=7)
    result = []

    # 学校行事（青）
    for event in SCHOOL_EVENTS:
        try:
            event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            continue

        if start <= event_date <= end:
            result.append({
                **event,
                "date": event_date.isoformat(),
                "kind": "school",
                "event_color": "#077AE3",
                "text_color": "#0563B5",
            })

    # 日本の祝日（赤）
    day = start
    while day <= end:
        holiday_name = jpholiday.is_holiday_name(day)
        if holiday_name:
            result.append({
                "date": day.isoformat(),
                "title": holiday_name,
                "detail": "",
                "kind": "holiday",
                "event_color": "#E53935",
                "text_color": "#C62828",
            })
        day += timedelta(days=1)

    # ユーザーがカレンダーで追加した予定（設定した色）
    personal_events = load_personal_events()  # 既存データは全員共通のまま
    for event in personal_events:
        try:
            event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            continue

        if not start <= event_date <= end:
            continue

        color_key = event.get("color", "blue")
        if color_key not in PERSONAL_EVENT_COLORS:
            color_key = "blue"
        result.append({
            **event,
            "date": event_date.isoformat(),
            "kind": "personal",
            "color": color_key,
            "event_color": PERSONAL_EVENT_COLORS[color_key],
            "text_color": PERSONAL_EVENT_TEXT_COLORS[color_key],
        })

    # 日付順。同じ日は祝日→学校行事→個人予定の順で表示する。
    priority = {"holiday": 0, "school": 1, "personal": 2}
    return sorted(
        result,
        key=lambda item: (item["date"], priority.get(item.get("kind"), 9)),
    )


def _normalise_text(value):
    return " ".join(
        (value or "")
        .replace("\u3000", " ")
        .replace("\xa0", " ")
        .split()
    )


def _normalise_route_name(value):
    """路線名比較用。空白・全角半角の違いを整える。"""
    value = _normalise_text(value)
    value = value.replace("〜", "～")
    value = value.replace("－", "～")
    value = value.replace("−", "～")
    return value.strip()


def _operation_result(route_key, ok, status, detail, source_url):
    return {
        "ok": ok,
        "route": ROUTE_OPTIONS.get(route_key, "路線"),
        "status": status,
        "detail": detail,
        "updated_at": datetime.now(JST).strftime("%H:%M"),
        "source_url": source_url,
    }


# ==================================================
# ホーム
# ==================================================

@app.route("/")
def home():
    now = datetime.now(JST)
    day_offset = max(-3650, min(request.args.get("day_offset", default=0, type=int), 3650))
    selected_day = now + timedelta(days=day_offset)
    weekday = WEEKDAYS[selected_day.weekday()] if selected_day.weekday() < 5 else ("土曜日" if selected_day.weekday() == 5 else "日曜日")
    class_name = selected_class(); weather_place = selected_weather_place()
    key = selected_day.strftime("%Y-%m-%d"); entry = load_special_schedule().get(key, {})
    mode = entry.get("__mode__", "") if isinstance(entry, dict) else ""
    message = entry.get("__message__", "") if isinstance(entry, dict) else ""
    is_holiday = jpholiday.is_holiday(selected_day.date())
    lessons = entry.get(class_name, CLASS_SCHEDULES.get(class_name, {}).get(weekday, [])) if class_name and isinstance(entry, dict) else []
    if is_holiday or mode in ("no_class", "special_message"): lessons = []
    elif mode == "timetable" and class_name: lessons = entry.get(class_name, [])
    is_special_schedule = bool(mode or (class_name and isinstance(entry, dict) and class_name in entry))
    return render_template("index.html", day_offset=day_offset, weekday=weekday, today_lessons=lessons,
        is_holiday=is_holiday, is_special_schedule=is_special_schedule, special_day_mode=mode, special_day_message=message,
        events=filtered_events(now), today=f"{selected_day.year}年{selected_day.month}月{selected_day.day}日",
        schedule_date_label=f"{selected_day.month}/{selected_day.day}({'月火水木金土日'[selected_day.weekday()]})",
        clock_date_label=f"{selected_day.year}年{selected_day.month}月{selected_day.day}日({'月火水木金土日'[selected_day.weekday()]})",
        class_name=class_name, class_names=CLASS_NAMES, weekday_short="月火水木金土日"[selected_day.weekday()],
        weather_place=weather_place, weather_label=WEATHER_OPTIONS[weather_place], railway_label=ROUTE_OPTIONS[selected_route()])

@app.route("/special-timetable", methods=["GET", "POST"])
def special_schedule():
    if not session.get("recording_admin_authenticated"):
        return redirect(url_for("recording_admin_login", next=url_for("special_schedule")))
    selected_date = request.values.get("date", datetime.now(JST).strftime("%Y-%m-%d"))
    try: target_day = datetime.strptime(selected_date, "%Y-%m-%d").date()
    except ValueError:
        target_day = datetime.now(JST).date(); selected_date = target_day.strftime("%Y-%m-%d")
    is_weekday = target_day.weekday() < 5 and not jpholiday.is_holiday(target_day); data = load_special_schedule(); class_name = selected_class()
    if request.method == "POST":
        if request.form.get("action") == "reset":
            data.pop(selected_date, None)
            save_special_schedule(data)
            return redirect(url_for("special_schedule", date=selected_date, reset=1))
        mode = request.form.get("mode", "timetable")
        if mode not in ("timetable", "no_class", "special_message"): mode = "timetable"
        class_name = request.form.get("class_name", "")
        if class_name not in CLASS_SCHEDULES: class_name = selected_class()
        entry = data.setdefault(selected_date, {}); entry["__mode__"] = mode
        if mode == "timetable" and class_name:
            entry[class_name] = [request.form.get(f"period_{i}", "").strip()[:40] for i in range(1, 8)]
        elif mode == "special_message": entry["__message__"] = request.form.get("message", "").strip()[:120] or "今日は特別な予定です"
        else: entry.pop("__message__", None)
        save_special_schedule(data)
        return redirect(url_for("special_schedule", date=selected_date, saved=1))
    entry = data.get(selected_date, {})
    mode = entry.get("__mode__", "timetable") if isinstance(entry, dict) else "timetable"
    lessons = entry.get(class_name, []) if class_name and isinstance(entry, dict) else []; lessons = (lessons + [""] * 7)[:7]
    return render_template("special_schedule.html", date=selected_date, class_name=class_name, class_names=CLASS_NAMES,
        lessons=lessons, mode=mode, message=entry.get("__message__", "") if isinstance(entry, dict) else "",
        is_weekday=is_weekday, saved=request.args.get("saved") == "1", reset=request.args.get("reset") == "1")

@app.route("/clock")
def clock_page():
    return render_template(
        "clock.html",
        weather_place=selected_weather_place(),
        railway_label=ROUTE_OPTIONS[selected_route()],
    )


# 週間時間割
# ==================================================

@app.route("/schedule", methods=["GET"])
def schedule():
    requested_class = request.args.get("class_name", "")

    class_name = (
        requested_class
        if requested_class in CLASS_SCHEDULES
        else selected_class()
    )

    return render_template(
        "schedule.html",
        schedule=CLASS_SCHEDULES.get(class_name, {}) if class_name else {},
        weekdays=WEEKDAYS,
        class_name=class_name,
        class_names=CLASS_NAMES,
    )




# ==================================================
# 両毛線時刻表
# ==================================================

TIMETABLE_STATIONS = {
    "小山": {
        "en": "Oyama",
        "上り": {"direction_label": "栃木･佐野･高崎方面", "platform": "6･8番線"},
    },
    "思川": {
        "en": "Omoigawa",
        "上り": {"direction_label": "栃木･佐野･高崎方面", "platform": "1番線"},
        "下り": {"direction_label": "小山方面", "platform": "2番線"},
    },
    "栃木": {
        "en": "Tochigi",
        "上り": {"direction_label": "佐野･足利･高崎方面", "platform": "2番線"},
        "下り": {"direction_label": "小山方面", "platform": "1番線"},
    },
    "大平下": {
        "en": "Ohirashita",
        "上り": {"direction_label": "佐野･足利･高崎方面", "platform": ""},
        "下り": {"direction_label": "栃木･小山方面", "platform": ""},
    },
    "岩舟": {
        "en": "Iwafune",
        "上り": {"direction_label": "佐野･足利･高崎方面", "platform": "2･3番線"},
        "下り": {"direction_label": "栃木･小山方面", "platform": "1番線"},
    },
    "佐野": {
        "en": "Sano",
        "上り": {"direction_label": "足利･桐生･高崎方面", "platform": "1番線"},
        "下り": {"direction_label": "栃木･小山方面", "platform": "2番線"},
    },
    "富田": {
        "en": "Tomita",
        "上り": {"direction_label": "足利･桐生･高崎方面", "platform": "1番線"},
        "下り": {"direction_label": "佐野･栃木･小山方面", "platform": "2番線"},
    },
    "あしかがフラワーパーク": {
        "en": "Ashikaga Flower Park",
        "上り": {"direction_label": "足利･桐生･高崎方面", "platform": ""},
        "下り": {"direction_label": "栃木･小山･東京方面", "platform": ""},
    },
    "足利": {
        "en": "Ashikaga",
        "上り": {"direction_label": "桐生･前橋･高崎方面", "platform": "2番線"},
        "下り": {"direction_label": "佐野･栃木･小山方面", "platform": "1番線"},
    },
    "山前": {
        "en": "Yamamae",
        "上り": {"direction_label": "桐生･前橋･高崎方面", "platform": "1番線"},
        "下り": {"direction_label": "足利･佐野･小山方面", "platform": "2･3番線"},
    },
    "小俣": {
        "en": "Omata",
        "上り": {"direction_label": "桐生･前橋･高崎方面", "platform": "1番線"},
        "下り": {"direction_label": "足利･佐野･小山方面", "platform": "2番線"},
    },
    # 列車詳細ページから全停車駅の時刻表へ移動できるように追加
    "桐生": {
        "en": "Kiryu",
        "上り": {"direction_label": "前橋･高崎方面", "platform": ""},
        "下り": {"direction_label": "足利･佐野･小山方面", "platform": ""},
    },
    "岩宿": {
        "en": "Iwajuku",
        "上り": {"direction_label": "前橋･高崎方面", "platform": ""},
        "下り": {"direction_label": "桐生･小山方面", "platform": ""},
    },
    "国定": {
        "en": "Kunisada",
        "上り": {"direction_label": "前橋･高崎方面", "platform": ""},
        "下り": {"direction_label": "桐生･小山方面", "platform": ""},
    },
    "伊勢崎": {
        "en": "Isesaki",
        "上り": {"direction_label": "前橋･高崎方面", "platform": ""},
        "下り": {"direction_label": "桐生･小山方面", "platform": ""},
    },
    "駒形": {
        "en": "Komagata",
        "上り": {"direction_label": "前橋･高崎方面", "platform": ""},
        "下り": {"direction_label": "伊勢崎･桐生･小山方面", "platform": ""},
    },
    "前橋大島": {
        "en": "Maebashi-Oshima",
        "上り": {"direction_label": "前橋･高崎方面", "platform": ""},
        "下り": {"direction_label": "伊勢崎･桐生･小山方面", "platform": ""},
    },
    "前橋": {
        "en": "Maebashi",
        "上り": {"direction_label": "新前橋･高崎方面", "platform": ""},
        "下り": {"direction_label": "伊勢崎･桐生･小山方面", "platform": ""},
    },
    "新前橋": {
        "en": "Shin-Maebashi",
        "上り": {"direction_label": "高崎方面", "platform": ""},
        "下り": {"direction_label": "前橋･伊勢崎･小山方面", "platform": ""},
    },
    "井野": {
        "en": "Ino",
        "上り": {"direction_label": "高崎方面", "platform": ""},
        "下り": {"direction_label": "新前橋･小山方面", "platform": ""},
    },
    "高崎問屋町": {
        "en": "Takasaki-Tonyamachi",
        "上り": {"direction_label": "高崎方面", "platform": ""},
        "下り": {"direction_label": "新前橋･小山方面", "platform": ""},
    },
    "高崎": {
        "en": "Takasaki",
        "上り": {"direction_label": "終点・高崎方面", "platform": ""},
        "下り": {"direction_label": "新前橋･小山方面", "platform": ""},
    },
}

# JR宇都宮線（東京～小山～宇都宮）の駅情報
UTSUNOMIYA_TIMETABLE_STATIONS = {
    "東京": {"en": "Tokyo", "下り": {"direction_label": "上野･大宮･小山･宇都宮方面", "platform": ""}},
    "上野": {"en": "Ueno", "上り": {"direction_label": "東京方面", "platform": ""}, "下り": {"direction_label": "大宮･小山･宇都宮方面", "platform": ""}},
    "尾久": {"en": "Oku", "上り": {"direction_label": "上野･東京方面", "platform": ""}, "下り": {"direction_label": "大宮･小山･宇都宮方面", "platform": ""}},
    "赤羽": {"en": "Akabane", "上り": {"direction_label": "上野･東京方面", "platform": ""}, "下り": {"direction_label": "大宮･小山･宇都宮方面", "platform": ""}},
    "浦和": {"en": "Urawa", "上り": {"direction_label": "赤羽･上野･東京方面", "platform": ""}, "下り": {"direction_label": "大宮･小山･宇都宮方面", "platform": ""}},
    "さいたま新都心": {"en": "Saitama-Shintoshin", "上り": {"direction_label": "浦和･上野･東京方面", "platform": ""}, "下り": {"direction_label": "大宮･小山･宇都宮方面", "platform": ""}},
    "大宮": {"en": "Omiya", "上り": {"direction_label": "浦和･上野･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "土呂": {"en": "Toro", "上り": {"direction_label": "大宮･上野･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "東大宮": {"en": "Higashi-Omiya", "上り": {"direction_label": "大宮･上野･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "蓮田": {"en": "Hasuda", "上り": {"direction_label": "大宮･上野･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "白岡": {"en": "Shiraoka", "上り": {"direction_label": "大宮･上野･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "新白岡": {"en": "Shin-Shiraoka", "上り": {"direction_label": "大宮･上野･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "久喜": {"en": "Kuki", "上り": {"direction_label": "大宮･上野･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "東鷲宮": {"en": "Higashi-Washinomiya", "上り": {"direction_label": "久喜･大宮･東京方面", "platform": ""}, "下り": {"direction_label": "古河･小山･宇都宮方面", "platform": ""}},
    "栗橋": {"en": "Kurihashi", "上り": {"direction_label": "久喜･大宮･東京方面", "platform": ""}, "下り": {"direction_label": "古河･小山･宇都宮方面", "platform": ""}},
    "古河": {"en": "Koga", "上り": {"direction_label": "久喜･大宮･東京方面", "platform": ""}, "下り": {"direction_label": "野木･小山･宇都宮方面", "platform": ""}},
    "野木": {"en": "Nogi", "上り": {"direction_label": "古河･大宮･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "間々田": {"en": "Mamada", "上り": {"direction_label": "古河･大宮･東京方面", "platform": ""}, "下り": {"direction_label": "小山･宇都宮方面", "platform": ""}},
    "小山": {"en": "Oyama", "上り": {"direction_label": "古河･大宮･上野･東京方面", "platform": ""}, "下り": {"direction_label": "小金井･宇都宮方面", "platform": ""}},
    "小金井": {"en": "Koganei", "上り": {"direction_label": "小山･大宮･東京方面", "platform": ""}, "下り": {"direction_label": "自治医大･宇都宮方面", "platform": ""}},
    "自治医大": {"en": "Jichi Medical University", "上り": {"direction_label": "小山･大宮･東京方面", "platform": ""}, "下り": {"direction_label": "石橋･宇都宮方面", "platform": ""}},
    "石橋": {"en": "Ishibashi", "上り": {"direction_label": "小山･大宮･東京方面", "platform": ""}, "下り": {"direction_label": "雀宮･宇都宮方面", "platform": ""}},
    "雀宮": {"en": "Suzumenomiya", "上り": {"direction_label": "石橋･小山･東京方面", "platform": ""}, "下り": {"direction_label": "宇都宮方面", "platform": ""}},
    "宇都宮": {"en": "Utsunomiya", "上り": {"direction_label": "小山･大宮･東京方面", "platform": ""}},
}

# 運行情報ページの時刻表路線選択。駅は指定区間内を路線順に掲載。
def _make_route_stations(names):
    result = {}
    for index, name in enumerate(names):
        result[name] = {
            "en": name,
            "上り": {"direction_label": names[max(0, index - 1)] + "方面", "platform": ""},
            "下り": {"direction_label": names[min(len(names) - 1, index + 1)] + "方面", "platform": ""},
        }
    if names:
        result[names[0]].pop("上り", None)
        result[names[-1]].pop("下り", None)
    return result

UTL_STATIONS = list(UTSUNOMIYA_TIMETABLE_STATIONS.keys())
UTL_STATIONS_REVERSED = list(reversed(UTL_STATIONS))
# 上野東京ライン：宇都宮～東京～熱海（東海道線内の各駅）
UTL_TOKAIDO = ["東京", "新橋", "品川", "川崎", "横浜", "戸塚", "大船", "藤沢", "辻堂", "茅ケ崎", "平塚", "大磯", "二宮", "国府津", "鴨宮", "小田原", "早川", "根府川", "真鶴", "湯河原", "熱海"]
# 湘南新宿ライン：宇都宮～大宮～新宿～逗子（停車駅）
SHONAN_STATIONS = ["宇都宮", "雀宮", "石橋", "自治医大", "小金井", "小山", "間々田", "野木", "古河", "栗橋", "東鷲宮", "久喜", "新白岡", "白岡", "蓮田", "東大宮", "土呂", "大宮", "浦和", "赤羽", "池袋", "新宿", "渋谷", "恵比寿", "大崎", "武蔵小杉", "横浜", "戸塚", "大船", "鎌倉", "逗子"]
SKYTREE_STATIONS = ["東武動物公園", "姫宮", "北春日部", "春日部", "一ノ割", "武里", "せんげん台", "大袋", "北越谷", "越谷", "新越谷", "蒲生", "新田", "獨協大学前", "草加", "谷塚", "竹ノ塚", "西新井", "梅島", "五反野", "小菅", "北千住", "牛田", "堀切", "鐘ケ淵", "東向島", "曳舟", "押上", "とうきょうスカイツリー", "浅草"]
NIKKO_STATIONS = ["東武動物公園", "杉戸高野台", "幸手", "南栗橋", "栗橋", "新古河", "柳生", "板倉東洋大前", "藤岡", "静和", "新大平下", "栃木", "新栃木", "合戦場", "家中", "東武金崎", "楡木", "樅山", "新鹿沼", "北鹿沼", "板荷", "下小代", "明神", "下今市", "上今市", "東武日光"]
UTSUNOMIYA_TOHBU_STATIONS = ["栃木", "新栃木", "野州平川", "野州大塚", "壬生", "国谷", "おもちゃのまち", "安塚", "西川田", "江曽島", "南宇都宮", "東武宇都宮"]
ISESAKI_STATIONS = ["東武動物公園", "和戸", "久喜", "鷲宮", "花崎", "加須", "南羽生", "羽生", "川俣", "茂林寺前", "館林", "多々良", "県", "福居", "東武和泉", "足利市", "野州山辺", "韮川", "太田"]
SANO_STATIONS = ["館林", "渡瀬", "田島", "佐野市", "佐野", "堀米", "吉水", "田沼", "多田", "葛生"]

TIMETABLE_LINE_CONFIG = {
    "ryomo": {"name": "両毛線", "stations": TIMETABLE_STATIONS, "color": "#FACC15", "text_color": "#292524"},
    "utsunomiya": {"name": "JR宇都宮線", "stations": UTSUNOMIYA_TIMETABLE_STATIONS, "color": "#F58A1F"},
    "ueno_tokyo": {"name": "上野東京ライン", "stations": _make_route_stations(UTL_STATIONS_REVERSED + UTL_TOKAIDO[1:]), "color": "#90268E"},
    "shonan_shinjuku": {"name": "湘南新宿ライン", "stations": _make_route_stations(SHONAN_STATIONS), "color": "#E31E27"},
    "tobu_skytree": {"name": "東武スカイツリーライン", "stations": _make_route_stations(SKYTREE_STATIONS), "color": "#0071BB"},
    "tobu_nikko": {"name": "東武日光線", "stations": _make_route_stations(NIKKO_STATIONS), "color": "#F58220"},
    "tobu_utsunomiya": {"name": "東武宇都宮線", "stations": _make_route_stations(UTSUNOMIYA_TOHBU_STATIONS), "color": "#D0A248"},
    "tobu_isesaki": {"name": "東武伊勢崎線", "stations": _make_route_stations(ISESAKI_STATIONS), "color": "#B00339"},
    "tobu_sano": {"name": "東武佐野線", "stations": _make_route_stations(SANO_STATIONS), "color": "#A065AA"},
}

# 設定画面のMy駅選択肢（両毛線：小山～小俣）
MY_STATION_OPTIONS = [
    "小山", "思川", "栃木", "大平下", "岩舟", "佐野",
    "富田", "あしかがフラワーパーク", "足利", "山前", "小俣",
]

def load_timetable_data():
    """複数路線形式の時刻表JSONを読み込む。旧形式も後方互換で扱う。"""
    timetable_path = Path(__file__).resolve().parent / "timetable.json"
    try:
        with timetable_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"lines": []}

    if isinstance(data, dict) and isinstance(data.get("lines"), list):
        return data
    # 旧形式 {line, trains} を両毛線として読み替える。
    if isinstance(data, dict) and isinstance(data.get("trains"), list):
        for train in data["trains"]:
            train.setdefault("operating_days", ["毎日"])
        return {"version": 2, "lines": [{
            "id": "ryomo", "name": data.get("line", "両毛線"),
            "stations": list(TIMETABLE_STATIONS.keys()), "trains": data["trains"]
        }]}
    return {"lines": []}


def timetable_line_data(line_id):
    data = load_timetable_data()
    return next((line for line in data.get("lines", []) if line.get("id") == line_id), None)


def train_operates_on_date(train, target_date):
    """運行日指定を判定。未指定の旧データは毎日運転として扱う。"""
    date_text = target_date.strftime("%Y-%m-%d")
    if date_text in train.get("excluded_dates", []):
        return False
    operating_days = train.get("operating_days") or ["毎日"]
    operating_dates = train.get("operating_dates", [])
    if "特定日" in operating_days and date_text in operating_dates:
        return True
    if "毎日" in operating_days or "全日" in operating_days:
        return True
    is_holiday = target_date.weekday() >= 5 or jpholiday.is_holiday(target_date)
    if "土休日" in operating_days and is_holiday:
        return True
    if "平日" in operating_days and not is_holiday:
        return True
    return False


@app.route("/timetable", methods=["GET"])
def timetable_page():
    data = load_timetable_data()
    available_lines = [
        {"id": line_id, "name": config["name"], "color": config.get("color", "#00A650")}
        for line_id, config in TIMETABLE_LINE_CONFIG.items()
    ]
    supported_ids = {item["id"] for item in available_lines}
    requested_line = request.args.get("line", "").strip()
    if requested_line not in supported_ids:
        preferred = selected_route()
        requested_line = preferred if preferred in supported_ids else (available_lines[0]["id"] if available_lines else "ryomo")
    line_id = requested_line
    line_data = timetable_line_data(line_id) or {"id": line_id, "name": TIMETABLE_LINE_CONFIG.get(line_id, {}).get("name", "時刻表"), "trains": []}
    stations = TIMETABLE_LINE_CONFIG.get(line_id, {}).get("stations", {})
    station_names = line_data.get("stations") or list(stations.keys())
    # 路線データにある駅だけを駅一覧として表示する。
    station_names = [name for name in station_names if name in stations]
    station = request.args.get("station", "").strip()
    # 駅一覧に常時表示しない駅でも、列車詳細ページから遷移できるようにする。
    if station not in stations:
        station = ""

    now = datetime.now(JST)
    departures_by_direction = {}
    if station:
        for direction in ("上り", "下り"):
            direction_info = stations[station].get(direction)
            if not direction_info:
                continue
            trains = []
            for train in line_data.get("trains", []):
                if train.get("direction") != direction or not train_operates_on_date(train, now.date()):
                    continue
                stop = train.get("stops", {}).get(station, {})
                arrival = stop.get("arrival")
                departure = stop.get("departure")
                event_time = arrival or departure
                if not event_time:
                    continue
                try:
                    hour, minute = (int(part) for part in event_time.split(":"))
                    event_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    arrival_dt = now.replace(hour=int(arrival.split(":")[0]), minute=int(arrival.split(":")[1]), second=0, microsecond=0) if arrival else None
                    departure_dt = now.replace(hour=int(departure.split(":")[0]), minute=int(departure.split(":")[1]), second=0, microsecond=0) if departure else None
                except (TypeError, ValueError, IndexError):
                    continue
                # 深夜帯の24時超えを含むデータは翌日扱いに補正する。
                if event_dt < now - timedelta(hours=12):
                    event_dt += timedelta(days=1)
                    if arrival_dt: arrival_dt += timedelta(days=1)
                    if departure_dt: departure_dt += timedelta(days=1)
                is_stopped = bool(arrival_dt and departure_dt and arrival_dt <= now < departure_dt)
                is_past = bool((departure_dt and departure_dt <= now) or (arrival_dt and not departure_dt and arrival_dt <= now))
                minutes_until = int((event_dt - now).total_seconds() // 60)
                display_time = departure or arrival
                display_dt = departure_dt or arrival_dt or event_dt
                trains.append({
                    "time": display_time, "time_minutes": display_dt.hour * 60 + display_dt.minute,
                    "train_number": train.get("train_number", ""), "type": train.get("type", "普通"),
                    "cars": train.get("cars", ""), "destination": train.get("destination", ""),
                    "origin": train.get("origin", ""), "note": train.get("note", ""),
                    "operating_days": train.get("operating_days", ["毎日"]),
                    "minutes_until": minutes_until, "is_past": is_past, "is_stopped": is_stopped,
                    "sort_dt": event_dt, "arrival_iso": arrival_dt.isoformat() if arrival_dt else "",
                    "departure_iso": departure_dt.isoformat() if departure_dt else "",
                    "event_kind": "arrival" if arrival else "departure",
                })
            trains.sort(key=lambda item: item["sort_dt"])
            future = [item for item in trains if not item["is_past"] and not item["is_stopped"] and item["sort_dt"] > now]
            for index, item in enumerate(future[:2]): item["upcoming_rank"] = index + 1
            for item in trains:
                item.setdefault("upcoming_rank", 0)
                item["event_iso"] = item["sort_dt"].isoformat()
                item.pop("sort_dt", None)
            departures_by_direction[direction] = {
                "info": direction_info, "trains": trains, "next_trains": future[:2],
                "has_arrival_times": any(item["arrival_iso"] for item in trains),
            }

    return render_template(
        "timetable.html", station=station, stations=stations, station_names=station_names,
        station_info=stations.get(station, {}), departures_by_direction=departures_by_direction,
        line_name=line_data.get("name", "時刻表"), line_id=line_id, available_lines=available_lines,
        now_label=now.strftime("%Y/%m/%d %H:%M"),
        operating_day_label="土休日" if now.weekday() >= 5 or jpholiday.is_holiday(now.date()) else "平日",
    )


@app.route("/train/<train_number>", defaults={"line_id": "ryomo"}, methods=["GET"])
@app.route("/train/<line_id>/<train_number>", methods=["GET"])
def train_detail(train_number, line_id):
    """列車ごとの全停車駅・到着時刻・発車時刻を表示する。"""
    line_data = timetable_line_data(line_id)
    if line_data is None:
        abort(404)
    train = next((item for item in line_data.get("trains", [])
                  if str(item.get("train_number", "")) == str(train_number)), None)
    if train is None:
        abort(404)
    stops = [{"station": name, "arrival": times.get("arrival"), "departure": times.get("departure")}
             for name, times in train.get("stops", {}).items()]
    return render_template(
        "train_detail.html", train=train, stops=stops,
        line_name=line_data.get("name", "時刻表"), line_id=line_id,
        back_station=request.args.get("station", "").strip(),
    )

# ==================================================
# 設定
# ==================================================

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        class_name = request.form.get("class_name", "")
        my_route = request.form.get("my_route", "ryomo")
        weather_place = request.form.get("weather_place", "sano")
        appearance = request.form.get("appearance", "system")
        calendar_layout = request.form.get("calendar_layout", "scroll")
        my_station = request.form.get("my_station", "").strip()

        if class_name not in CLASS_SCHEDULES:
            class_name = ""
        if my_route not in ROUTE_OPTIONS:
            my_route = "ryomo"
        if weather_place not in WEATHER_OPTIONS:
            weather_place = "sano"
        if appearance not in ("light", "dark", "system"):
            appearance = "system"
        if calendar_layout not in ("scroll", "compact"):
            calendar_layout = "scroll"
        if my_station not in MY_STATION_OPTIONS:
            my_station = ""

        response = make_response(redirect(url_for("settings")))

        cookie_options = {
            "max_age": 60 * 60 * 24 * 365,
            "samesite": "Lax",
        }

        if class_name:
            response.set_cookie("myHomeClass", class_name, **cookie_options)
        else:
            response.delete_cookie("myHomeClass")

        response.set_cookie("myHomeRoute", my_route, **cookie_options)
        response.set_cookie("myHomeWeather", weather_place, **cookie_options)
        response.set_cookie("myHomeAppearance", appearance, **cookie_options)
        response.set_cookie("myHomeCalendarLayout", calendar_layout, **cookie_options)
        if my_station:
            response.set_cookie("myHomeStation", my_station, **cookie_options)
        else:
            response.delete_cookie("myHomeStation")

        return response

    return render_template(
        "settings.html",
        class_name=selected_class(),
        class_names=CLASS_NAMES,
        my_route=selected_route(),
        route_options=ROUTE_OPTIONS,
        weather_place=selected_weather_place(),
        weather_options=WEATHER_OPTIONS,
        my_station=request.cookies.get("myHomeStation", "") if request.cookies.get("myHomeStation", "") in MY_STATION_OPTIONS else "",
        station_options=MY_STATION_OPTIONS,
        appearance=selected_appearance(),
        calendar_layout=request.cookies.get("myHomeCalendarLayout", "scroll")
            if request.cookies.get("myHomeCalendarLayout", "scroll") in ("scroll", "compact")
            else "scroll",
    )


# ==================================================
# Yahoo!路線情報の解析
# ==================================================

def _get_page_soup(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    response = requests.get(url, headers=headers, timeout=(5, 12))
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"

    return BeautifulSoup(response.text, "html.parser")


def _clean_operation_container(soup):
    """
    運行情報本体を優先して残す。
    属性情報が None の HTML 要素があっても停止しない。
    """
    work = BeautifulSoup(str(soup), "html.parser")

    for tag in work.select(
        "script, style, noscript, template, iframe, svg, nav, footer"
    ):
        tag.decompose()

    for tag in list(work.find_all(True)):
        attrs = getattr(tag, "attrs", None)

        if not isinstance(attrs, dict):
            continue

        tag_id = attrs.get("id") or ""
        tag_classes = attrs.get("class") or []

        if isinstance(tag_classes, str):
            tag_classes = [tag_classes]

        marker = " ".join([
            str(tag_id),
            " ".join(str(value) for value in tag_classes),
        ]).lower()

        if any(word in marker for word in (
            "tweet", "twitter", "comment", "review",
            "userpost", "user-post", "social",
        )):
            tag.decompose()

    return work


def _extract_updated(text):
    patterns = [
        r"\d{1,2}月\d{1,2}日\s*\d{1,2}時\d{1,2}分\s*更新",
        r"\d{1,2}月\d{1,2}日\s*\d{1,2}時\d{1,2}分\s*現在",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)

    return ""


# ==================================================
# 運行ステータスの判定
# ==================================================

def _find_status(text):
    """
    運行情報の文章から状態を判定する。
    「遅延情報はありません」などの否定表現を
    異常状態と誤判定しない。
    """
    text = _normalise_text(text)

    if not text:
        return "", -1

    # ------------------------------------------
    # 1. 平常運転を示す明確な文章を先に判定
    # ------------------------------------------
    normal_phrases = (
        "現在､事故･遅延に関する情報はありません",
        "現在、事故・遅延に関する情報はありません",
        "事故･遅延に関する情報はありません",
        "事故・遅延に関する情報はありません",
        "事故や遅延に関する情報はありません",
        "遅延に関する情報はありません",
        "遅れに関する情報はありません",
        "現在、運行情報はありません",
        "現在運行情報はありません",
        "平常運転",
    )

    for phrase in normal_phrases:
        index = text.find(phrase)
        if index >= 0:
            return "平常運転", index


    # ------------------------------------------
    # 2. 運転見合わせ・運転中止
    #    ただし「直通運転を中止」は遅延より優先しない
    # ------------------------------------------
    suspension_options = (
        "運転見合わせ",
        "運転を見合わせ",
        "運転取りやめ",
        "運転を取りやめ",
        "運転中止",
        "運転を中止",
    )

    # 「直通運転を中止」は路線全体の運転中止と区別する
    direct_stop_phrases = (
        "直通運転を中止",
        "直通運転中止",
    )

    has_direct_stop = any(
        phrase in text for phrase in direct_stop_phrases
    )

    # 遅延・遅れの記載があるか確認
    delay_phrases = (
        "列車遅延",
        "遅延",
        "遅れ",
    )

    has_delay = any(
        phrase in text for phrase in delay_phrases
    )

    # 直通運転中止と遅延が併記されている場合は「遅延」を優先
    if has_direct_stop and has_delay:
        delay_indexes = [
            text.find(phrase)
            for phrase in delay_phrases
            if text.find(phrase) >= 0
        ]
        return "遅延", min(delay_indexes)

    # 通常の運転中止・見合わせは従来どおり判定
    suspension_matches = [
        (text.find(phrase), phrase)
        for phrase in suspension_options
        if text.find(phrase) >= 0
        and phrase not in direct_stop_phrases
    ]

    if suspension_matches:
        suspension_matches.sort(key=lambda item: item[0])
        index, phrase = suspension_matches[0]
        return phrase, index

    # ------------------------------------------
    # 3. 運転再開後も遅れ・運休が残る場合
    # ------------------------------------------
    has_restart = any(
        phrase in text
        for phrase in (
            "運転を再開",
            "運転再開",
            "運転を再開しました",
        )
    )

    has_remaining_disruption = any(
        phrase in text
        for phrase in (
            "遅れが出ています",
            "遅延が出ています",
            "一部列車に遅れ",
            "一部の列車に遅れ",
            "一部列車に運休",
            "一部の列車に運休",
            "一部運休",
            "遅れや運休",
            "遅延や運休",
        )
    )

    if has_restart and has_remaining_disruption:
        index = text.find("運転")
        return "遅延", index if index >= 0 else 0

    # ------------------------------------------
    # 4. 運休・遅延
    # ------------------------------------------
    disruption_options = (
        "一部運休",
        "列車遅延",
        "遅延",
        "遅れ",
        "運休",
    )

    disruption_matches = [
        (text.find(phrase), phrase)
        for phrase in disruption_options
        if text.find(phrase) >= 0
    ]

    if disruption_matches:
        disruption_matches.sort(key=lambda item: item[0])
        index, phrase = disruption_matches[0]
        return "遅延", index

    # ------------------------------------------
    # 5. その他の運行情報
    # ------------------------------------------
    if has_restart:
        index = text.find("運転")
        return "運転再開", index if index >= 0 else 0

    if "運転計画" in text:
        return "運転計画", text.find("運転計画")

    if "その他" in text:
        return "その他", text.find("その他")

    return "", -1


# ==================================================
# 運行情報の詳細文を抽出
# ==================================================

def _extract_detail(text, status, status_index):
    """運行情報の詳細文を抽出する。"""

    text = _normalise_text(text)

    if not text:
        return "詳細は情報元のページをご確認ください。"

    if status == "平常運転":
        return "事故・遅延に関する情報はありません。"

    # 更新日時を除去
    text = re.sub(
        r"\d{1,2}月\d{1,2}日\s*"
        r"\d{1,2}時\d{1,2}分\s*(?:更新|現在)",
        "",
        text,
    )

    # 掲載日時を除去
    text = re.sub(
        r"[（(]\s*\d{1,2}月\d{1,2}日.*?掲載\s*[）)]",
        "",
        text,
    )

    # 「運転状況」より後ろを対象にする
    marker = "運転状況"
    marker_index = text.find(marker)

    if marker_index >= 0:
        text = text[marker_index + len(marker):]

    # 迂回ルート検索や登録案内以降を除去
    stop_markers = (
        "迂回ルート検索",
        "路線を登録すると",
        "この路線に関するつぶやき",
        "みんなの運行情報",
        "運行情報を投稿",
        "Yahoo! JAPAN",
    )

    positions = [
        text.find(marker)
        for marker in stop_markers
        if text.find(marker) >= 0
    ]

    if positions:
        text = text[:min(positions)]

    text = _normalise_text(text)

    # 詳細欄に混入する操作案内・状態ラベルを除去
    text = re.sub(r"路線を登録", "", text)
    text = re.sub(r"列車遅延", "", text)
    text = re.sub(r"[ \t　]{2,}", " ", text).strip(" \t　・|｜-—")

    # 先頭に路線名が残っている場合は除去
    route_names = (
        "東武宇都宮線",
        "東武日光線",
        "東武伊勢崎線",
        "東武スカイツリーライン",
        "東武佐野線",
        "東北新幹線",
        "上野東京ライン",
        "湘南新宿ライン",
        "宇都宮線",
        "両毛線",
    )

    for route_name in route_names:
        if text.startswith(route_name):
            text = text[len(route_name):].strip()
            break

    text = _normalise_text(text)

    # 詳細文の先頭にある「[東京～宇都宮] 」などの区間名を除去する。
    # 角括弧の直後にスペースがある場合も、ない場合も対応する。
    text = re.sub(r"^\s*[\[［][^\]］]+[\]］]\s*", "", text)

    # 詳細文の先頭にある「[東京～宇都宮]運転見合わせ」などの
    # 区間名・状態ラベルを除き、時刻（例: 16:14頃）以降の本文を表示する。
    time_match = re.search(r"\d{1,2}:\d{2}頃", text)
    if time_match:
        text = text[time_match.start():].strip()

    if not text:
        return "詳細は情報元のページをご確認ください。"

    return text[:4000]

# ==================================================
# Yahoo!運行情報ページの解析
# ==================================================

def _parse_operation_page(soup, route_key):
    """Yahoo!の路線個別ページから運行状況と詳細を取得する。"""
    route_label = ROUTE_OPTIONS.get(route_key, "路線")

    heading = soup.find("h1")

    if heading is None:
        raise ValueError("ページ内に路線名の見出しがありません")

    heading_text = _normalise_route_name(
        heading.get_text(" ", strip=True)
    )

    candidates = OPERATION_ROUTE_MATCHES.get(
        route_key,
        (route_label,),
    )

    candidate_names = {
        _normalise_route_name(name)
        for name in candidates
    }

    if heading_text not in candidate_names:
        raise ValueError(f"路線名が一致しません: {heading_text}")

    # HTML全体から見出し以降のテキストを取得する
    parts = []

    for item in heading.find_all_next(string=True):
        value = _normalise_text(str(item))

        if not value:
            continue

        if "に関するつぶやき" in value:
            break

        if "みんなの運行情報" in value:
            break

        parts.append(value)

    text = _normalise_text(" ".join(parts))
    print("[運行情報取得・確認] 路線:", route_label)
    print("[運行情報取得・確認] 取得文章:", text[:1500])

    text = re.sub(
        r"\d{1,2}月\d{1,2}日\s*"
        r"\d{1,2}時\d{1,2}分\s*(?:更新|現在)",
        "",
        text,
    )

    text = _normalise_text(text)

    status, status_index = _find_status(text)

    # 上野東京ライン・湘南新宿ラインの直通運転中止は、
    # 路線全体の運転中止とは区別して専用ステータスにする。
    direct_stop_route_keys = {"ueno_tokyo", "shonan_shinjuku"}
    direct_stop_match = re.search(
        r"との直通運転を中止(?:しています|しております|しました)?",
        text,
    )
    if route_key in direct_stop_route_keys and direct_stop_match:
        status = "直通運転中止"
        status_index = direct_stop_match.start()

    if not status:
        raise ValueError(
            f"{route_label}の運行状況を判定できません: {text[:250]}"
        )

    detail = _extract_detail(text, status, status_index)

    print(
        f"[運行情報解析] {route_label}: "
        f"status={status!r}, detail={detail[:200]!r}"
    )

    return status, detail

# ==================================================
# 選択中の路線の運行情報
# ==================================================

def fetch_operation_status(route_key):
    route_key = (
        route_key if route_key in ROUTE_OPTIONS else "ryomo"
    )

    route_label = ROUTE_OPTIONS[route_key]

    source_url = OPERATION_SOURCE_URLS.get(
        route_key,
        OPERATION_SOURCE_URL,
    )

    now = time.time()

    with OPERATION_LOCK:
        cached = OPERATION_CACHE.get(route_key)

        if (
            cached
            and now - cached["cached_at"] < OPERATION_CACHE_SECONDS
        ):
            return dict(cached["data"])

    try:
        soup = _get_page_soup(source_url)

        status, detail = _parse_operation_page(
            soup,
            route_key,
        )

        result = _operation_result(
            route_key,
            True,
            status,
            detail,
            source_url,
        )

    except Exception as exc:
        print(
            f"\n[運行情報取得エラー] {route_label}: {exc}"
        )
        traceback.print_exc()

        result = _operation_result(
            route_key,
            False,
            "取得できません",
            (
                "運行情報を取得・判定できませんでした。"
                "情報元のページで最新情報をご確認ください。"
            ),
            source_url,
        )

    with OPERATION_LOCK:
        OPERATION_CACHE[route_key] = {
            "cached_at": time.time(),
            "data": dict(result),
        }

    return dict(result)


# ==================================================
# 複数路線の運行異常情報
# ==================================================

def fetch_operation_alerts():
    now = time.time()

    with OPERATION_LOCK:
        cached_at = OPERATION_ALERT_CACHE["cached_at"]

        if now - cached_at < OPERATION_ALERT_CACHE_SECONDS:
            return [
                dict(item)
                for item in OPERATION_ALERT_CACHE["data"]
            ]

    alerts = []

    abnormal_statuses = (
        "運転見合わせ",
        "運転取りやめ",
        "運転中止",
        "一部運休",
        "運休",
        "列車遅延",
        "遅延",
        "運転計画",
        "運行情報あり",
        "その他",
        "運転再開",
        "直通運転中止",
    )

    for route_key, route_label in ROUTE_OPTIONS.items():
        operation = fetch_operation_status(route_key)

        status = _normalise_text(
            operation.get("status", "")
        )

        detail = _normalise_text(
            operation.get("detail", "")
        )

        # 詳細欄の先頭に付く状態ラベル「運転見合わせ 」を除去
        detail = re.sub(r"^運転見合わせ[ \\t　]+", "", detail).strip()

        source_url = operation.get(
            "source_url",
            OPERATION_SOURCE_URL,
        )

        if not operation.get("ok"):
            alerts.append({
                "route": route_label,
                "status": "取得できません",
                "detail": (
                    "運行状況を確認できません。"
                    "情報元のページで最新情報をご確認ください。"
                ),
                "updated_at": operation.get("updated_at"),
                "source_url": source_url,
                "icon": "warning.png",
            })
            continue

        if status == "平常運転":
            continue

        if not any(
            word in status for word in abnormal_statuses
        ):
            continue

        # 運転再開後も遅れ・運休が残る場合は遅延扱い
        if (
            "運転再開" in detail
            and any(
                word in detail
                for word in ("遅れ", "遅延", "運休")
            )
        ):
            status = "遅延"

        if status == "直通運転中止":
            icon = "direct_stop.png"

        elif any(word in status for word in (
            "運転見合わせ",
            "運転取りやめ",
            "運転中止",
            "一部運休",
            "運休",
        )):
            icon = "stop.png"

        elif "遅延" in status:
            icon = "delay.png"

        else:
            icon = "warning.png"

        alerts.append({
            "route": route_label,
            "status": status,
            "detail": detail or "詳細は情報元のページをご確認ください。",
            "updated_at": operation.get("updated_at"),
            "source_url": source_url,
            "icon": icon,
        })

    with OPERATION_LOCK:
        OPERATION_ALERT_CACHE["cached_at"] = time.time()
        OPERATION_ALERT_CACHE["data"] = [
            dict(item) for item in alerts
        ]

    return [
        dict(item)
        for item in alerts
    ]


# ==================================================
# 運行情報ページ
# ==================================================

@app.route("/operation")
def operation_page():
    route_key = selected_route()

    operation = fetch_operation_status(route_key)
    operation_alerts = fetch_operation_alerts()

    # 時刻表の路線選択は運行情報ページに配置する。
    timetable_lines = [
        {"id": line_id, "name": config["name"], "color": config.get("color", "#00A650")}
        for line_id, config in TIMETABLE_LINE_CONFIG.items()
    ]
    timetable_line_id = request.args.get("timetable_line", "").strip()
    valid_timetable_ids = {item["id"] for item in timetable_lines}
    if timetable_line_id not in valid_timetable_ids:
        timetable_line_id = route_key if route_key in valid_timetable_ids else "ryomo"
    timetable_station_names = list(TIMETABLE_LINE_CONFIG[timetable_line_id]["stations"].keys())
    # 運行情報ページの両毛線駅選択は、小山～小俣までに限定する。
    # 桐生以遠の駅情報は列車詳細など既存機能のため設定側に残す。
    if timetable_line_id == "ryomo" and "小俣" in timetable_station_names:
        timetable_station_names = timetable_station_names[:timetable_station_names.index("小俣") + 1]

    my_station = request.cookies.get("myHomeStation", "").strip()
    if my_station not in MY_STATION_OPTIONS:
        my_station = ""

    timetable_line_color = TIMETABLE_LINE_CONFIG[timetable_line_id].get("color", "#FACC15")
    timetable_line_text_color = TIMETABLE_LINE_CONFIG[timetable_line_id].get("text_color", "#FFFFFF")

    return render_template(
        "operation.html",
        operation=operation,
        operation_alerts=operation_alerts,
        railway_label=ROUTE_OPTIONS[route_key],
        weather_place=selected_weather_place(),
        my_station=my_station,
        my_station_name=my_station,
        timetable_lines=timetable_lines,
        timetable_line_id=timetable_line_id,
        timetable_line_color=timetable_line_color,
        timetable_line_text_color=timetable_line_text_color,
        timetable_station_names=timetable_station_names,
    )


# ==================================================
# ユーザー予定の保存
# ==================================================

PERSONAL_EVENTS_FILE = Path(__file__).with_name("personal_events.json")
PERSONAL_EVENTS_LOCK = threading.Lock()

PERSONAL_EVENT_COLORS = {
    "blue": "#077AE3",
    "red": "#E53935",
    "orange": "#F07800",
    "yellow": "#FDD835",
    "green": "#2E9D50",
    "purple": "#8E44AD",
    "pink": "#E84393",
}

# 予定名用の文字色。黄色も背景に埋もれないよう濃い色を使う。
PERSONAL_EVENT_TEXT_COLORS = {
    "blue": "#0563B5",
    "red": "#C62828",
    "orange": "#B94F00",
    "yellow": "#8A6D00",
    "green": "#20743A",
    "purple": "#71368A",
    "pink": "#B91F70",
}


def load_personal_events():
    """保存済みのユーザー予定を読み込む。"""
    with PERSONAL_EVENTS_LOCK:
        try:
            with PERSONAL_EVENTS_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, list):
                return []
            return [
                event for event in data
                if isinstance(event, dict)
                and event.get("id")
                and event.get("date")
                and event.get("title")
            ]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []


def save_personal_events(events):
    """ユーザー予定をJSONファイルへ保存する。"""
    with PERSONAL_EVENTS_LOCK:
        temporary_file = PERSONAL_EVENTS_FILE.with_suffix(".tmp")
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(events, file, ensure_ascii=False, indent=2)
        temporary_file.replace(PERSONAL_EVENTS_FILE)


# ==================================================
# カレンダー
# ==================================================

@app.route("/calendar", methods=["GET", "POST"])
def calendar_page():
    now = datetime.now(JST)

    if request.method == "POST":
        # 個人予定の新規作成・編集・削除はブラウザーの localStorage で処理する。
        # 旧フォームからのPOSTで共有予定JSONを書き換えない。
        return redirect(url_for("calendar_page", year=now.year, month=now.month))

    try:
        year = int(request.args.get("year", now.year))
        month = int(request.args.get("month", now.month))
        if not 1 <= month <= 12 or not 1 <= year <= 9999:
            raise ValueError
    except (ValueError, TypeError):
        year, month = now.year, now.month

    month_calendar = calendar_module.Calendar(
        firstweekday=6
    ).monthdatescalendar(year, month)

    events_by_date = {}

    # 学校行事
    for event in SCHOOL_EVENTS:
        try:
            event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            continue
        events_by_date.setdefault(event_date.isoformat(), []).append({
            **event, "kind": "school"
        })

    # personal_events.json の既存予定は全員共通。新規予定はブラウザー側で追加する。
    personal_events = load_personal_events()
    for event in personal_events:
        try:
            event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            continue
        color_key = event.get("color", "blue")
        events_by_date.setdefault(event_date.isoformat(), []).append({
            **event,
            "kind": "personal",
            "color": color_key if color_key in PERSONAL_EVENT_COLORS else "blue",
        })

    # 日本の祝日（振替休日・国民の休日を含む）
    for week in month_calendar:
        for day in week:
            holiday_name = jpholiday.is_holiday_name(day)
            if holiday_name:
                events_by_date.setdefault(day.isoformat(), []).insert(0, {
                    "id": "holiday-" + day.isoformat(),
                    "date": day.isoformat(),
                    "title": holiday_name,
                    "detail": "",
                    "kind": "holiday",
                })

    holiday_dates = {
        day.isoformat()
        for week in month_calendar
        for day in week
        if jpholiday.is_holiday(day)
    }

    weeks = []
    for week in month_calendar:
        days = []
        for day in week:
            days.append({
                "date": day,
                "in_month": day.month == month,
                "is_holiday": day.isoformat() in holiday_dates,
                "events": events_by_date.get(day.isoformat(), []),
            })
        weeks.append(days)

    previous_year = year if month > 1 else year - 1
    previous_month = month - 1 if month > 1 else 12
    next_year = year if month < 12 else year + 1
    next_month = month + 1 if month < 12 else 1

    return render_template(
        "calendar.html",
        year=year,
        month=month,
        weeks=weeks,
        today=now.date(),
        personal_event_colors=PERSONAL_EVENT_COLORS,
        personal_event_text_colors=PERSONAL_EVENT_TEXT_COLORS,
        previous_year=previous_year,
        previous_month=previous_month,
        next_year=next_year,
        next_month=next_month,
        weather_place=selected_weather_place(),
        railway_label=ROUTE_OPTIONS[selected_route()],
    )


# ==================================================
# その他
# ==================================================

@app.route("/more")
def more_page():
    return render_template(
        "more.html",
        weather_place=selected_weather_place(),
        railway_label=ROUTE_OPTIONS[selected_route()],
    )

# ==================================================
# その他：各ツールのページ
# ==================================================

# ==================================================
# 追加ツール：仮ページ
# ==================================================

@app.route("/map")
def map_page():
    return render_template("map.html")


@app.route("/database")
def database_page():
    return redirect("https://1drv.ms/o/c/da9538a4f9ea7cf2/EiUgrA7IIWBJijIwtkU7a8gB06UBYVQdAmJdf8D-YfVB8g?e=DCUq1E")


@app.route("/range", methods=["GET"])
def range_page():
    requested_class = request.args.get("class_name", "")
    class_name = requested_class if requested_class in CLASS_SCHEDULES else selected_class()
    range_group = "12" if class_name in ("1組", "2組") else "34" if class_name in ("3組", "4組") else None
    return render_template(
        "range.html",
        class_name=class_name,
        class_names=CLASS_NAMES,
        range_group=range_group,
    )


@app.route("/line")
def line_page():
    return redirect("https://line.me/R/ti/p/@396dngoe")




# ==================================================
# アンケート
# ==================================================

FORM_DB = Path(__file__).resolve().parent / "form_submissions.sqlite3"
FORM_UPLOAD_DIR = Path(__file__).resolve().parent / "form_uploads"
FORM_ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
FORM_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB


def form_connect():
    conn = sqlite3.connect(FORM_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_form_db():
    FORM_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with form_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS form_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                image_filename TEXT NOT NULL DEFAULT '',
                image_original_name TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL
            )
        """)


init_form_db()


@app.route("/form", methods=["GET", "POST"])
def form_page():
    categories = ("ご意見", "不具合", "その他")
    error = ""
    if request.method == "POST":
        category = request.form.get("category", "").strip()
        message = request.form.get("message", "").strip()
        uploaded = request.files.get("image")

        if category not in categories:
            error = "項目を選択してください。"
        elif not message:
            error = "内容を入力してください。"
        elif len(message) > 10000:
            error = "入力できる文字数は10,000文字までです。"
        elif category != "不具合" and uploaded and uploaded.filename:
            error = "画像を添付できるのは「不具合」の場合のみです。"
        else:
            image_filename = ""
            original_name = ""
            if category == "不具合" and uploaded and uploaded.filename:
                original_name = Path(uploaded.filename.replace("\\", "/")).name[:200]
                extension = Path(original_name).suffix.lower()
                if extension not in FORM_ALLOWED_IMAGE_EXTENSIONS:
                    error = "画像はPNG・JPG・JPEG・WEBP・GIF形式にしてください。"
                else:
                    image_data = uploaded.stream.read(FORM_MAX_IMAGE_BYTES + 1)
                    if len(image_data) > FORM_MAX_IMAGE_BYTES:
                        error = "画像の容量は8MB以下にしてください。"
                    elif not image_data:
                        error = "画像ファイルを読み込めませんでした。"
                    else:
                        image_filename = f"{uuid.uuid4().hex}{extension}"
                        FORM_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                        (FORM_UPLOAD_DIR / image_filename).write_bytes(image_data)

            if not error:
                try:
                    with form_connect() as conn:
                        conn.execute("""
                            INSERT INTO form_submissions
                                (category, message, image_filename, image_original_name, submitted_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            category, message, image_filename, original_name,
                            datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
                        ))
                    # 送信完了後は確認ダイアログを表示するページへリダイレクト
                    return redirect(url_for("form_page", sent="1"))
                except Exception:
                    if image_filename:
                        try:
                            (FORM_UPLOAD_DIR / image_filename).unlink(missing_ok=True)
                        except OSError:
                            pass
                    app.logger.exception("アンケートの保存に失敗しました")
                    error = "送信に失敗しました。時間をおいて再度お試しください。"

    return render_template(
        "form.html",
        error=error,
        submitted=(request.method == "GET" and request.args.get("sent") == "1"),
    )


@app.route("/admin/forms", methods=["GET", "POST"])
def form_admin():
    if not session.get("recording_admin_authenticated"):
        return redirect(url_for("recording_admin_login", next=url_for("form_admin")))
    if request.method == "POST":
        submission_id = request.form.get("submission_id", type=int)
        if submission_id:
            with form_connect() as conn:
                row = conn.execute(
                    "SELECT image_filename FROM form_submissions WHERE id = ?",
                    (submission_id,)
                ).fetchone()
                conn.execute("DELETE FROM form_submissions WHERE id = ?", (submission_id,))
            if row and row["image_filename"]:
                try:
                    (FORM_UPLOAD_DIR / row["image_filename"]).unlink(missing_ok=True)
                except OSError:
                    app.logger.exception("アンケート添付画像の削除に失敗しました")
            flash("アンケートを削除しました。", "success")
        return redirect(url_for("form_admin"))

    with form_connect() as conn:
        submissions = conn.execute(
            "SELECT * FROM form_submissions ORDER BY id DESC"
        ).fetchall()
        counts = {
            row["category"]: row["total"]
            for row in conn.execute(
                "SELECT category, COUNT(*) AS total FROM form_submissions GROUP BY category"
            ).fetchall()
        }
    return render_template("form_admin.html", submissions=submissions, counts=counts)


@app.route("/admin/forms/image/<filename>")
def form_attachment(filename):
    if not session.get("recording_admin_authenticated"):
        abort(403)
    # DBに登録されたランダム生成名の画像だけを配信する
    with form_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM form_submissions WHERE image_filename = ?", (filename,)
        ).fetchone()
    if not row:
        abort(404)
    path = FORM_UPLOAD_DIR / Path(filename).name
    if not path.is_file():
        abort(404)
    return send_file(path)


@app.route("/roster")
def roster_page():
    """4桁番号と名前だけで管理し、番号から学年・クラス・出席番号を取得する名簿ページ。"""
    roster_file = Path(__file__).resolve().parent / "roster_data.json"
    try:
        with roster_file.open("r", encoding="utf-8") as f:
            raw_students = json.load(f)
    except (OSError, json.JSONDecodeError):
        raw_students = []

    # JSONの最上位は、生徒データを並べた配列であることを確認します。
    if not isinstance(raw_students, list):
        raw_students = []

    students = []
    for item in raw_students:
        # 配列内に辞書以外の値が混ざっていても処理を止めません。
        if not isinstance(item, dict):
            continue
        number = str(item.get("number", "")).strip()
        name = str(item.get("name", "")).strip()
        # 4桁の番号以外は名簿データとして扱わない
        if not re.fullmatch(r"\d{4}", number) or not name:
            continue
        students.append({
            "number": number,
            "grade": number[0],
            "class": number[1],
            "attendance": int(number[2:4]),
            "name": name,
        })

    query = request.args.get("q", "").strip()
    class_filter = request.args.get("class", "").strip()
    results = students
    if class_filter in {"1", "2", "3", "4"}:
        results = [s for s in results if s["class"] == class_filter]
    else:
        class_filter = ""
    if query:
        compact = re.sub(r"\s+", "", query)
        # 4桁番号（1桁目=学年、2桁目=クラス、下2桁=出席番号）
        if re.fullmatch(r"\d{4}", compact):
            results = [s for s in results if s["number"] == compact]
        else:
            # 「1年2組」「2組」「01番」「01」などを認識
            grade_match = re.search(r"(\d)年", compact)
            class_match = re.search(r"(\d)組", compact)
            attendance_match = re.fullmatch(r"(\d{1,2})番?", compact)
            if grade_match or class_match:
                grade = grade_match.group(1) if grade_match else None
                klass = class_match.group(1) if class_match else None
                results = [s for s in students
                           if (grade is None or s["grade"] == grade)
                           and (klass is None or s["class"] == klass)]
            elif attendance_match:
                attendance = int(attendance_match.group(1))
                results = [s for s in results if s["attendance"] == attendance]
            else:
                results = [s for s in results if query.casefold() in s["name"].casefold()]

    results = sorted(results, key=lambda s: (int(s["number"])))
    return render_template(
        "roster.html", students=results, query=query, class_filter=class_filter,
        weather_place=selected_weather_place(),
        railway_label=ROUTE_OPTIONS[selected_route()],
    )

# ==================================================
# 録音（MP3は static/recording/ に手動で配置）
# ==================================================

BASE_DIR = Path(__file__).resolve().parent
RECORDING_DIR = BASE_DIR / "static" / "recording"
RECORDING_DB = BASE_DIR / "recordings.db"

# タグ・人物の表示順は録音管理画面から編集し、SQLiteに保存します。
TAG_ORDER_SETTING_KEY = "recording_tag_order"
PEOPLE_ORDER_SETTING_KEY = "recording_people_order"


def _get_saved_order(conn, setting_key):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (setting_key,)).fetchone()
    if not row:
        return []
    try:
        value = json.loads(row["value"])
        return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def get_saved_tag_order(conn):
    return _get_saved_order(conn, TAG_ORDER_SETTING_KEY)


def get_saved_people_order(conn):
    return _get_saved_order(conn, PEOPLE_ORDER_SETTING_KEY)


def get_recording_db():
    conn = sqlite3.connect(RECORDING_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_recording_db():
    RECORDING_DIR.mkdir(parents=True, exist_ok=True)
    with get_recording_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                people TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for path in RECORDING_DIR.iterdir():
            if path.is_file() and path.suffix.lower() == ".mp3":
                conn.execute(
                    "INSERT OR IGNORE INTO recordings (filename, title) VALUES (?, ?)",
                    (path.name, path.stem.replace('_', ' '))
                )


def split_tags(value):
    return [part.strip() for part in (value or '').replace('、', ',').split(',') if part.strip()]


@app.route("/recording", methods=["GET", "POST"])
def recording_page():
    init_recording_db()
    if request.method == "POST":
        filename = Path(request.form.get("filename", "").strip()).name
        title = request.form.get("title", "").strip()
        people = request.form.get("people", "").strip()
        recorded_at = request.form.get("recorded_at", "").strip()
        tags = request.form.get("tags", "").strip()
        audio_path = RECORDING_DIR / filename
        if filename.lower().endswith(".mp3") and audio_path.is_file() and title:
            with get_recording_db() as conn:
                conn.execute(
                    """INSERT INTO recordings (filename, title, people, recorded_at, tags)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(filename) DO UPDATE SET
                         title=excluded.title, people=excluded.people,
                         recorded_at=excluded.recorded_at, tags=excluded.tags""",
                    (filename, title, people, recorded_at, tags)
                )
        return redirect(url_for("recording_page"))

    query = request.args.get("q", "").strip()
    search_scope = request.args.get("scope", "all").strip().lower()
    if search_scope not in {"all", "title", "people", "date", "tags"}:
        search_scope = "all"
    # 初期表示は管理画面で設定した人物順にします。
    sort_by = request.args.get("sort", "people").strip().lower()
    if sort_by not in {"date", "people", "tags"}:
        sort_by = "people"

    with get_recording_db() as conn:
        rows = conn.execute("SELECT * FROM recordings").fetchall()
        tag_options, person_options = [], []
        seen_tags, seen_people = set(), set()
        for row in rows:
            for tag in split_tags(row['tags']):
                if tag.casefold() not in seen_tags:
                    seen_tags.add(tag.casefold()); tag_options.append(tag)
            for person in (row['people'] or '').replace('、', ',').split(','):
                person = person.strip()
                if person and person.casefold() not in seen_people:
                    seen_people.add(person.casefold()); person_options.append(person)
        saved_order = get_saved_tag_order(conn)
        saved_people_order = get_saved_people_order(conn)
        rank = {tag.casefold(): i for i, tag in enumerate(saved_order)}
        people_rank = {person.casefold(): i for i, person in enumerate(saved_people_order)}
        tag_options.sort(key=lambda tag: (0, rank[tag.casefold()]) if tag.casefold() in rank else (1, tag.casefold()))
        person_options.sort(key=lambda person: (0, people_rank[person.casefold()]) if person.casefold() in people_rank else (1, person.casefold()))

        if query:
            like = f"%{query}%"
            search_columns = {
                "title": "title LIKE ? OR filename LIKE ?",
                "people": "people LIKE ?",
                "date": "recorded_at LIKE ?",
                "tags": "tags LIKE ?",
                "all": "title LIKE ? OR filename LIKE ? OR people LIKE ? OR recorded_at LIKE ? OR tags LIKE ?",
            }
            where_clause = search_columns[search_scope]
            recordings = conn.execute(
                f"SELECT * FROM recordings WHERE ({where_clause})",
                tuple([like] * where_clause.count('?'))
            ).fetchall()
        else:
            recordings = rows

    def sort_key(item):
        if sort_by == 'people':
            # 管理画面で保存した人物順を録音一覧の「人名順」にも反映します。
            # 複数人物が登録されている場合は、設定順で最も先の人物を基準にします。
            item_people = [
                person.strip()
                for person in (item['people'] or '').replace('、', ',').split(',')
                if person.strip()
            ]
            if item_people:
                person_key = min(
                    (0, people_rank[person.casefold()], person.casefold())
                    if person.casefold() in people_rank
                    else (1, len(people_rank), person.casefold())
                    for person in item_people
                )
            else:
                person_key = (2, len(people_rank), '')
            return (person_key, (item['title'] or '').casefold())
        if sort_by == 'tags':
            # 管理画面で保存したタグ順を使い、録音に付いているタグのうち
            # 最も優先順位が高いタグを基準に並べます。未登録タグは末尾側です。
            item_tags = split_tags(item['tags'])
            if not item_tags:
                tag_key = (2, '', '')
            else:
                tag_key = min(
                    (0, tag_rank[tag.casefold()], tag.casefold())
                    if tag.casefold() in tag_rank
                    else (1, 0, tag.casefold())
                    for tag in item_tags
                )
            return (tag_key, (item['title'] or '').casefold())
        return (item['recorded_at'] or '', (item['title'] or '').casefold())

    # 保存済みの表示順を録音一覧の「タグ順」ソートにも共通利用します。
    tag_rank = {tag.casefold(): i for i, tag in enumerate(saved_order)}
    recordings = sorted(recordings, key=sort_key, reverse=(sort_by == 'date'))

    return render_template(
        "recording.html", recordings=recordings, query=query,
        search_scope=search_scope, tag_options=tag_options,
        person_options=person_options, sort_by=sort_by,
        weather_place=selected_weather_place(), railway_label=ROUTE_OPTIONS[selected_route()],
    )


@app.route("/recording/download/<path:filename>")
def download_recording(filename):
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith('.mp3'):
        abort(404)
    path = RECORDING_DIR / safe_name
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=safe_name, mimetype='audio/mpeg')


@app.route("/recording/download-all")
def download_all_recordings():
    init_recording_db()
    memory_file = io.BytesIO()
    with get_recording_db() as conn, zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as archive:
        rows = conn.execute("SELECT filename, title FROM recordings ORDER BY title COLLATE NOCASE").fetchall()
        added = 0
        for row in rows:
            filename = Path(row['filename']).name
            path = RECORDING_DIR / filename
            if filename.lower().endswith('.mp3') and path.is_file():
                archive.write(path, arcname=filename)
                added += 1
    if added == 0:
        return "ダウンロードできる録音ファイルがありません。", 404
    memory_file.seek(0)
    return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name='recordings.zip')


# ==================================================
# 録音管理（タイトル・人物名・日付・タグの編集）
# ==================================================

@app.route("/admin", methods=["GET"])
def admin_dashboard():
    """管理者用ページ。録音・スタンプ・時間割の管理入口。"""
    if not session.get("recording_admin_authenticated"):
        return redirect(url_for("recording_admin_login", next=url_for("admin_dashboard")))
    return render_template("admin_dashboard.html")


@app.route("/recording/admin/login", methods=["GET", "POST"])
def recording_admin_login():
    configured_password = RECORDING_ADMIN_PASSWORD
    error = ""
    if request.method == "POST":
        submitted = request.form.get("password", "")
        if configured_password and secrets.compare_digest(
            submitted.encode("utf-8"), configured_password.encode("utf-8")
        ):
            session["recording_admin_authenticated"] = True
            session.permanent = True
            next_url = request.form.get("next", request.args.get("next", ""))
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("admin_dashboard"))
        error = "管理用パスワードが未設定か、パスワードが違います。"
    return render_template("recording_admin_login.html", error=error, next_url=request.args.get("next", ""))


@app.route("/recording/admin/logout", methods=["POST"])
def recording_admin_logout():
    session.pop("recording_admin_authenticated", None)
    return redirect(url_for("recording_admin_login"))


@app.route("/recording/admin", methods=["GET", "POST"])
def recording_admin():
    if not session.get("recording_admin_authenticated"):
        return redirect(url_for("recording_admin_login"))
    init_recording_db()
    if request.method == "POST":
        action = request.form.get("action", "save_all")
        query = request.form.get("q", "")
        if action == "save_tag_order":
            submitted_order = []
            seen = set()
            for raw_tag in request.form.getlist("tag_order"):
                tag = raw_tag.strip()
                if tag and tag.casefold() not in seen:
                    submitted_order.append(tag)
                    seen.add(tag.casefold())
            with get_recording_db() as conn:
                all_rows = conn.execute("SELECT tags FROM recordings").fetchall()
                existing_tags = []
                existing_seen = set()
                for row in all_rows:
                    for tag in split_tags(row["tags"]):
                        if tag.casefold() not in existing_seen:
                            existing_seen.add(tag.casefold())
                            existing_tags.append(tag)
                # 画面送信後に追加されたタグも失わず、最後に追加する
                for tag in existing_tags:
                    if tag.casefold() not in seen:
                        submitted_order.append(tag)
                        seen.add(tag.casefold())
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (TAG_ORDER_SETTING_KEY, json.dumps(submitted_order, ensure_ascii=False))
                )
                conn.commit()
            flash("タグの並び順を保存しました。検索画面にも反映されます。", "success")
            return redirect(url_for("recording_admin", q=query))
        if action == "save_people_order":
            submitted_order = []
            seen = set()
            for raw_person in request.form.getlist("people_order"):
                person = raw_person.strip()
                if person and person.casefold() not in seen:
                    submitted_order.append(person)
                    seen.add(person.casefold())
            with get_recording_db() as conn:
                all_rows = conn.execute("SELECT people FROM recordings").fetchall()
                existing_people = []
                existing_seen = set()
                for row in all_rows:
                    for person in (row["people"] or "").replace("、", ",").split(","):
                        person = person.strip()
                        if person and person.casefold() not in existing_seen:
                            existing_seen.add(person.casefold())
                            existing_people.append(person)
                # 保存画面を開いた後に追加された人物名も順番から失わない
                for person in existing_people:
                    if person.casefold() not in seen:
                        submitted_order.append(person)
                        seen.add(person.casefold())
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (PEOPLE_ORDER_SETTING_KEY, json.dumps(submitted_order, ensure_ascii=False))
                )
                conn.commit()
            flash("人物の並び順を保存しました。検索画面にも反映されます。", "success")
            return redirect(url_for("recording_admin", q=query))
        if action == "delete":
            rec_id = request.form.get("id", type=int)
            with get_recording_db() as conn:
                row = conn.execute("SELECT filename FROM recordings WHERE id = ?", (rec_id,)).fetchone()
                if row is None:
                    flash("削除対象の録音が見つかりませんでした。", "error")
                else:
                    filename = Path(row["filename"]).name
                    conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
                    conn.commit()
                    audio_path = RECORDING_DIR / filename
                    if audio_path.is_file() and audio_path.resolve().parent == RECORDING_DIR.resolve():
                        try:
                            audio_path.unlink()
                            flash("録音情報と音声ファイルを削除しました。", "success")
                        except OSError:
                            flash("録音情報は削除しましたが、音声ファイルを削除できませんでした。", "error")
                    else:
                        flash("録音情報を削除しました。音声ファイルは見つかりませんでした。", "success")
            return redirect(url_for("recording_admin", q=query))
        if action == "save_all":
            saved = errors = 0
            with get_recording_db() as conn:
                for raw_id in request.form.getlist("recording_ids"):
                    try:
                        rec_id = int(raw_id)
                    except (TypeError, ValueError):
                        errors += 1
                        continue
                    title = request.form.get(f"title_{rec_id}", "").strip()
                    people = request.form.get(f"people_{rec_id}", "").strip()
                    recorded_at = request.form.get(f"recorded_at_{rec_id}", "").strip()
                    raw_tags = request.form.get(f"tags_{rec_id}", "")
                    tags = ",".join(dict.fromkeys(tag.strip() for tag in raw_tags.replace("、", ",").split(",") if tag.strip()))
                    if not title:
                        errors += 1
                        continue
                    result = conn.execute("UPDATE recordings SET title=?, people=?, recorded_at=?, tags=? WHERE id=?", (title, people, recorded_at, tags, rec_id))
                    if result.rowcount:
                        saved += 1
                conn.commit()
            flash(f"{saved}件を保存しました。" + (f" {errors}件は未保存です。" if errors else ""), "error" if errors else "success")
        return redirect(url_for("recording_admin", q=query))
    query = request.args.get("q", "").strip()
    with get_recording_db() as conn:
        if query:
            like = f"%{query}%"
            rows = conn.execute("SELECT * FROM recordings WHERE title LIKE ? OR filename LIKE ? OR people LIKE ? OR tags LIKE ? ORDER BY COALESCE(recorded_at, '') DESC, title COLLATE NOCASE", (like, like, like, like)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM recordings ORDER BY COALESCE(recorded_at, '') DESC, title COLLATE NOCASE").fetchall()
    with get_recording_db() as conn:
        all_tag_rows = conn.execute("SELECT tags FROM recordings").fetchall()
        all_people_rows = conn.execute("SELECT people FROM recordings").fetchall()
        all_tags = []
        seen_tags = set()
        for row in all_tag_rows:
            for tag in split_tags(row["tags"]):
                if tag.casefold() not in seen_tags:
                    seen_tags.add(tag.casefold())
                    all_tags.append(tag)
        all_people = []
        seen_people = set()
        for row in all_people_rows:
            for person in (row["people"] or "").replace("、", ",").split(","):
                person = person.strip()
                if person and person.casefold() not in seen_people:
                    seen_people.add(person.casefold())
                    all_people.append(person)
        saved_order = get_saved_tag_order(conn)
        saved_people_order = get_saved_people_order(conn)
    order_rank = {tag.casefold(): i for i, tag in enumerate(saved_order)}
    all_tags.sort(key=lambda tag: (0, order_rank[tag.casefold()]) if tag.casefold() in order_rank else (1, tag.casefold()))
    people_order_rank = {person.casefold(): i for i, person in enumerate(saved_people_order)}
    all_people.sort(key=lambda person: (0, people_order_rank[person.casefold()]) if person.casefold() in people_order_rank else (1, person.casefold()))
    return render_template("recording_admin.html", recordings=rows, query=query, tag_order=all_tags, people_order=all_people)


# ==================================================
# スタンプ（画像は static/stamps/ に手動で配置）
# 録音とは別DBを使い、既存の録音データに影響しないようにします。
# ==================================================
STAMP_DIR = BASE_DIR / "static" / "stamps"
STAMP_DB = BASE_DIR / "stamps.db"
STAMP_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp"}
STAMP_TAG_ORDER_SETTING_KEY = "stamp_tag_order"
STAMP_PEOPLE_ORDER_SETTING_KEY = "stamp_people_order"


def get_stamp_db():
    conn = sqlite3.connect(STAMP_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_stamp_db():
    STAMP_DIR.mkdir(parents=True, exist_ok=True)
    with get_stamp_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stamp_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stamps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                people TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 既存DBにも番号列を追加し、既存スタンプにはID順で1から番号を付けます。
        columns = {row[1] for row in conn.execute("PRAGMA table_info(stamps)").fetchall()}
        if "number" not in columns:
            conn.execute("ALTER TABLE stamps ADD COLUMN number INTEGER NOT NULL DEFAULT 0")
            existing = conn.execute("SELECT id FROM stamps ORDER BY id").fetchall()
            for number, row in enumerate(existing, start=1):
                conn.execute("UPDATE stamps SET number = ? WHERE id = ?", (number, row["id"]))
        # フォルダに新しく置かれた画像を自動登録。既存のタイトル等は上書きしません。
        for path in STAMP_DIR.iterdir():
            if path.is_file() and path.suffix.lower() in STAMP_IMAGE_EXTENSIONS and not path.name.startswith("."):
                conn.execute(
                    "INSERT OR IGNORE INTO stamps (filename, title) VALUES (?, ?)",
                    (path.name, path.stem.replace("_", " "))
                )
        # 新しく検出した画像には、現在の最大番号の次の番号を付けます。
        next_number = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM stamps").fetchone()[0]
        unnumbered = conn.execute("SELECT id FROM stamps WHERE number IS NULL OR number <= 0 ORDER BY id").fetchall()
        for row in unnumbered:
            conn.execute("UPDATE stamps SET number = ? WHERE id = ?", (next_number, row["id"]))
            next_number += 1
        # フォルダからなくなった画像の管理情報も一覧から除外します。
        rows = conn.execute("SELECT id, filename FROM stamps").fetchall()
        for row in rows:
            filename = Path(row["filename"]).name
            if filename != row["filename"] or not (STAMP_DIR / filename).is_file():
                conn.execute("DELETE FROM stamps WHERE id = ?", (row["id"],))


def get_stamp_saved_order(conn, key):
    row = conn.execute("SELECT value FROM stamp_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return []
    try:
        value = json.loads(row["value"])
        return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def stamp_metadata_options(rows):
    tags, people, seen_tags, seen_people = [], [], set(), set()
    for row in rows:
        for tag in split_tags(row["tags"]):
            if tag.casefold() not in seen_tags:
                seen_tags.add(tag.casefold())
                tags.append(tag)
        for person in (row["people"] or "").replace("、", ",").split(","):
            person = person.strip()
            if person and person.casefold() not in seen_people:
                seen_people.add(person.casefold())
                people.append(person)
    return tags, people



@app.route("/quiz")
def quiz_page():
    quiz_path = Path(app.root_path) / "static" / "quiz_data.json"
    try:
        with open(quiz_path, encoding="utf-8") as f:
            quiz_data = json.load(f)
    except (OSError, ValueError):
        quiz_data = []
    return render_template(
        "quiz.html",
        quiz_data=quiz_data,
        weather_place=selected_weather_place(),
        railway_label=ROUTE_OPTIONS[selected_route()],
    )


@app.route("/stamp", methods=["GET"])
def stamp_page():
    init_stamp_db()
    query = request.args.get("q", "").strip()
    search_scope = request.args.get("scope", "all").strip().lower()
    if search_scope not in {"all", "title", "people", "tags"}:
        search_scope = "all"
    sort_by = request.args.get("sort", "people").strip().lower()
    if sort_by not in {"number", "title", "people", "tags"}:
        sort_by = "people"

    with get_stamp_db() as conn:
        all_rows = conn.execute("SELECT * FROM stamps").fetchall()
        tag_options, person_options = stamp_metadata_options(all_rows)
        saved_tags = get_stamp_saved_order(conn, STAMP_TAG_ORDER_SETTING_KEY)
        saved_people = get_stamp_saved_order(conn, STAMP_PEOPLE_ORDER_SETTING_KEY)

    tag_rank = {item.casefold(): i for i, item in enumerate(saved_tags)}
    people_rank = {item.casefold(): i for i, item in enumerate(saved_people)}
    tag_options.sort(key=lambda item: (0, tag_rank[item.casefold()]) if item.casefold() in tag_rank else (1, item.casefold()))
    person_options.sort(key=lambda item: (0, people_rank[item.casefold()]) if item.casefold() in people_rank else (1, item.casefold()))

    rows = list(all_rows)
    if query:
        q = query.casefold()
        def matches(row):
            fields = {
                "title": [row["title"], row["filename"]],
                "people": [row["people"]],
                "tags": [row["tags"]],
                "all": [row["title"], row["filename"], row["people"], row["tags"]],
            }[search_scope]
            return any(q in (value or "").casefold() for value in fields)
        rows = [row for row in rows if matches(row)]

    def sort_key(row):
        title_key = (row["title"] or row["filename"]).casefold()
        if sort_by == "number":
            return (int(row["number"] or 0), title_key, row["filename"].casefold())
        if sort_by == "title":
            return (title_key, row["filename"].casefold())
        if sort_by == "tags":
            vals = split_tags(row["tags"])
            rank = min([(0, tag_rank[v.casefold()], v.casefold()) if v.casefold() in tag_rank else (1, 0, v.casefold()) for v in vals], default=(2, 0, ""))
            # タグのグループ内では、スタンプ番号の昇順に並べる
            return (rank, int(row["number"] or 0), title_key, row["filename"].casefold())
        vals = [v.strip() for v in (row["people"] or "").replace("、", ",").split(",") if v.strip()]
        rank = min([(0, people_rank[v.casefold()], v.casefold()) if v.casefold() in people_rank else (1, len(people_rank), v.casefold()) for v in vals], default=(2, len(people_rank), ""))
        # 同じ人物のグループ内では、スタンプ番号の昇順に並べる
        return (rank, int(row["number"] or 0), title_key, row["filename"].casefold())
    rows.sort(key=sort_key)
    return render_template(
        "stamp.html", stamps=rows, query=query, search_scope=search_scope,
        sort_by=sort_by, tag_options=tag_options, person_options=person_options,
        weather_place=selected_weather_place(), railway_label=ROUTE_OPTIONS[selected_route()]
    )


@app.route("/stamp/admin", methods=["GET", "POST"])
def stamp_admin():
    if not session.get("recording_admin_authenticated"):
        return redirect(url_for("recording_admin_login", next=url_for("stamp_admin")))
    init_stamp_db()
    query = request.args.get("q", "").strip()
    if request.method == "POST":
        action = request.form.get("action", "save_all")
        query = request.form.get("q", query).strip()
        if action in {"save_tag_order", "save_people_order"}:
            is_tags = action == "save_tag_order"
            key = STAMP_TAG_ORDER_SETTING_KEY if is_tags else STAMP_PEOPLE_ORDER_SETTING_KEY
            field = "tag_order" if is_tags else "people_order"
            submitted, seen = [], set()
            for raw in request.form.getlist(field):
                value = raw.strip()
                if value and value.casefold() not in seen:
                    submitted.append(value)
                    seen.add(value.casefold())
            with get_stamp_db() as conn:
                all_rows = conn.execute("SELECT tags, people FROM stamps").fetchall()
                _, all_people = stamp_metadata_options(all_rows)
                all_tags, _ = stamp_metadata_options(all_rows)
                existing = all_tags if is_tags else all_people
                for value in existing:
                    if value.casefold() not in seen:
                        submitted.append(value)
                        seen.add(value.casefold())
                conn.execute("INSERT INTO stamp_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(submitted, ensure_ascii=False)))
                conn.commit()
            flash(("タグ" if is_tags else "人物") + "の表示順を保存しました。", "success")
            return redirect(url_for("stamp_admin", q=query))
        if action == "delete":
            stamp_id = request.form.get("id", type=int)
            with get_stamp_db() as conn:
                row = conn.execute("SELECT filename FROM stamps WHERE id = ?", (stamp_id,)).fetchone()
                if row is None:
                    flash("削除対象のスタンプが見つかりませんでした。", "error")
                else:
                    filename = Path(row["filename"]).name
                    conn.execute("DELETE FROM stamps WHERE id = ?", (stamp_id,))
                    conn.commit()
                    image_path = (STAMP_DIR / filename).resolve()
                    if image_path.parent == STAMP_DIR.resolve() and image_path.is_file():
                        try:
                            image_path.unlink()
                            flash("スタンプ情報と画像を削除しました。", "success")
                        except OSError:
                            flash("情報は削除しましたが、画像ファイルを削除できませんでした。", "error")
                    else:
                        flash("スタンプ情報を削除しました。画像ファイルは見つかりませんでした。", "success")
            return redirect(url_for("stamp_admin", q=query))
        if action == "save_all":
            saved = errors = 0
            with get_stamp_db() as conn:
                for raw_id in request.form.getlist("stamp_ids"):
                    try:
                        stamp_id = int(raw_id)
                    except (TypeError, ValueError):
                        errors += 1
                        continue
                    title = request.form.get(f"title_{stamp_id}", "").strip()
                    people = request.form.get(f"people_{stamp_id}", "").strip()
                    tags = ",".join(dict.fromkeys(tag.strip() for tag in request.form.get(f"tags_{stamp_id}", "").replace("、", ",").split(",") if tag.strip()))
                    try:
                        number = int(request.form.get(f"number_{stamp_id}", "").strip())
                    except (TypeError, ValueError):
                        errors += 1
                        continue
                    if not title or number < 1:
                        errors += 1
                        continue
                    result = conn.execute("UPDATE stamps SET number=?, title=?, people=?, tags=? WHERE id=?", (number, title, people, tags, stamp_id))
                    if result.rowcount:
                        saved += 1
                conn.commit()
            flash(f"{saved}件のスタンプ情報を保存しました。" + (f" {errors}件は未保存です。" if errors else ""), "error" if errors else "success")
            return redirect(url_for("stamp_admin", q=query))

    with get_stamp_db() as conn:
        if query:
            like = f"%{query}%"
            rows = conn.execute("SELECT * FROM stamps WHERE title LIKE ? OR filename LIKE ? OR people LIKE ? OR tags LIKE ? ORDER BY number, title COLLATE NOCASE", (like, like, like, like)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM stamps ORDER BY number, title COLLATE NOCASE").fetchall()
        all_rows = conn.execute("SELECT * FROM stamps").fetchall()
        tags, people = stamp_metadata_options(all_rows)
        saved_tags = get_stamp_saved_order(conn, STAMP_TAG_ORDER_SETTING_KEY)
        saved_people = get_stamp_saved_order(conn, STAMP_PEOPLE_ORDER_SETTING_KEY)
    trank = {v.casefold(): i for i, v in enumerate(saved_tags)}
    prank = {v.casefold(): i for i, v in enumerate(saved_people)}
    tags.sort(key=lambda v: (0, trank[v.casefold()]) if v.casefold() in trank else (1, v.casefold()))
    people.sort(key=lambda v: (0, prank[v.casefold()]) if v.casefold() in prank else (1, v.casefold()))
    return render_template("stamp_admin.html", stamps=rows, query=query, tag_order=tags, people_order=people,
                           weather_place=selected_weather_place(), railway_label=ROUTE_OPTIONS[selected_route()])


@app.route("/video")
def video_page():
    return redirect("https://youtube.com/playlist?list=PLCGJL6NeediL94IzE4Kk-dxjv5h7SRTzR&si=w2oGM9GDTKQxV6wT")


@app.route("/calculator")
def calculator_page():
    return render_template(
        "calculator.html",
        weather_place=selected_weather_place(),
        railway_label=ROUTE_OPTIONS[selected_route()],
    )


@app.route("/calculator/history")
def calculator_history_page():
    return render_template(
        "calculator_history.html",
        weather_place=selected_weather_place(),
        railway_label=ROUTE_OPTIONS[selected_route()],
    )


@app.route("/unit-converter")
def unit_converter_page():
    return render_template(
        "unit_converter.html",
        weather_place=selected_weather_place(),
        railway_label=ROUTE_OPTIONS[selected_route()],
    )

# ==================================================
# 運行情報API
# ==================================================

@app.route("/api/operation")
def api_operation():
    response = jsonify(
        fetch_operation_status(selected_route())
    )

    response.headers["Cache-Control"] = "no-store"

    return response


# ==================================================
# 複数路線の運行異常情報API
# ==================================================

@app.route("/api/operation-alerts")
def api_operation_alerts():
    response = jsonify({
        "alerts": fetch_operation_alerts(),
        "updated_at": datetime.now(JST).strftime("%H:%M"),
    })

    response.headers["Cache-Control"] = "no-store"

    return response


# ==================================================
# 時刻API
# ==================================================

@app.route("/api/time")
def api_time():
    now = datetime.now(JST)

    return jsonify({
        "time": now.strftime("%H:%M"),
        "date": f"{now.year}年{now.month}月{now.day}日",
        "weekday": "月火水木金土日"[now.weekday()],
    })


# ==================================================
# 起動
# ==================================================

if __name__ == "__main__":
    app.run(debug=True)