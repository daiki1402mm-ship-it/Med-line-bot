import os
import re
import psycopg2
from psycopg2.extras import DictCursor
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, PostbackEvent
from datetime import datetime, date, timedelta
import pytz
from utils import get_db_connection, parse_date, calculate_salary

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

# 💡【特注フィルター】utilsのparse_dateが「5月」を「月曜日」と誤認するのを完全に防ぐ
def safe_parse_date(text, default_date):
    if not text: return default_date
    
    m_exact = re.search(r'(?<!\d)(\d{1,2})[/月](\d{1,2})日?', text)
    if m_exact:
        month, day = int(m_exact.group(1)), int(m_exact.group(2))
        year = default_date.year
        if month < default_date.month and default_date.month >= 11:
            year += 1
        try: return date(year, month, day)
        except ValueError: pass
        
    m_day = re.match(r'^(\d{1,2})日$', text.strip())
    if m_day:
        day = int(m_day.group(1))
        try: return date(default_date.year, default_date.month, day)
        except ValueError: pass

    p = parse_date(text)
    if p: return p
    
    return default_date


# --- 1. 自動通知システム（cron-job POST対応・リマインド・給料日 統合版） ---

@app.route("/ping", methods=['GET', 'POST'])
def ping():
    return "Awake!", 200

@app.route("/notify", methods=['GET', 'POST'])
def notify():
    jst = pytz.timezone('Asia/Tokyo')
    now = datetime.now(jst)
    today_jst = now.date()
    
    is_evening = now.hour >= 17
    target_date = today_jst + timedelta(days=1) if is_evening else today_jst
    
    today_str = today_jst.strftime('%Y-%m-%d')
    target_str = target_date.strftime('%Y-%m-%d')

    pay_labels = []
    if target_date.day == 10: pay_labels.append("💰【給料日(ファミマ)】")
    if target_date.day == 25: pay_labels.append("💰【給料日(トライ)】")
    pay_str = "".join(pay_labels)

    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cursor:
                cursor.execute("SELECT value FROM settings WHERE key = 'line_user_id'")
                row = cursor.fetchone()
                if not row:
                    print("Notify Error: line_user_id not found")
                    return "Error: No User ID", 200 
                user_id = row['value']

                cursor.execute("""
                    SELECT subject_name, content, deadline 
                    FROM assignments 
                    WHERE remind_date::text = %s 
                      AND is_completed = FALSE
                """, (today_str,))
                reminders = cursor.fetchall()

                cursor.execute("SELECT period, subject_name FROM attendance WHERE date::text = %s ORDER BY period ASC", (target_str,))
                lectures = cursor.fetchall()

                cursor.execute("SELECT detail, start_time FROM lifestyle_schedules WHERE event_date::text = %s ORDER BY start_time ASC", (target_str,))
                lifestyle = cursor.fetchall()
        
        time_label = "【明日" if is_evening else "【今日"
        msg = f"{time_label} {target_date.strftime('%m/%d')} の通知】{pay_str}\n"
        
        if reminders:
            msg += "\n⚠️【提出物リマインド】\n" + "\n".join([
                f"・{r['subject_name']}\n  (締切: {r['deadline'].strftime('%m/%d') if hasattr(r['deadline'], 'strftime') else str(r['deadline'])})" 
                for r in reminders
            ])
            
        msg += f"\n🗓 大学の講義:\n" + ("\n".join([f" {l['period']}限: {l['subject_name']}" for l in lectures]) if lectures else " なし！📚")
        
        if lifestyle:
            msg += f"\n\n🏠 その他の予定:\n" + "\n".join([
                f" {l['start_time'].strftime('%H:%M') if l['start_time'] else '時間指定なし'} : {l['detail']}" 
                for l in lifestyle
            ])
        
        line_bot_api.push_message(user_id, TextSendMessage(text=msg))
        print(f"Notification Sent: {today_jst}")
        return "OK", 200

    except Exception as e:
        print(f"NOTIFY CRASH: {e}")
        return f"Error: {e}", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(PostbackEvent)
def handle_postback(event):
    data = dict(re.findall(r'([^&=]+)=([^&]*)', event.postback.data))
    if data.get('action') == 'set_status':
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE attendance SET status = %s WHERE date = %s AND period = %s", (data['status'], data['date'], data['period']))
                conn.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 更新完了！"))
        except: pass

# --- 2. メインメッセージ処理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    user_id = event.source.user_id
    now = datetime.now(pytz.timezone('Asia/Tokyo'))
    today = now.date()
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO settings (key, value) VALUES ('line_user_id', %s) ON CONFLICT (key) DO UPDATE SET value = %s", (user_id, user_id))
            conn.commit()
    except: pass

    lines = user_msg.strip().split('\n')

    # ==========================================
    # A. 単一行の「照会・確認」コマンド
    # ==========================================
    if len(lines) == 1:
        line0 = lines[0].strip()
        
        # 試験のカウントダウン確認機能
        if "テスト" in line0 or "試験" in line0 or "CBT" in line0:
            try:
                with get_db_connection() as conn:
                    with conn.cursor(cursor_factory=DictCursor) as cur:
                        cur.execute("SELECT subject_name, exam_date, location FROM exams WHERE exam_date >= CURRENT_DATE ORDER BY exam_date ASC LIMIT 5")
                        tests = cur.fetchall()

                if not tests:
                    return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="今のところ予定されている試験はないみたい！やったね！"))
                
                reply = "直近の試験予定だよ！🩺\n\n"
                for t in tests:
                    t_date = t['exam_date']
                    if isinstance(t_date, str): t_date = date.fromisoformat(t_date)
                    delta = (t_date - today).days
                    loc_str = f" (📍{t['location']})" if t['location'] else ""
                    
                    if delta > 0:
                        reply += f"📅 {t['subject_name']}{loc_str}\n期日: {t_date.strftime('%Y-%m-%d')} (あと {delta} 日)\n\n"
                    elif delta == 0:
                        reply += f"🚨 {t['subject_name']}{loc_str}\n今日が本番！全力を出し切って！\n\n"
                
                reply += "日々の勉強大変だと思うけど、応援してるよ！"
                return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            except Exception as e:
                return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"データ取得エラー😭: {e}"))

        if line0 in ["給料", "実績", "きゅうりょう", "じっせき"]:
            try:
                with get_db_connection() as conn:
                    with conn.cursor(cursor_factory=DictCursor) as cur:
                        first_day = today.replace(day=1)
                        cur.execute("SELECT job_name, SUM(pay_amount) as total, COUNT(*) as shift_count FROM work_results WHERE work_date >= %s AND work_date <= %s GROUP BY job_name ORDER BY total DESC", (first_day.isoformat(), today.isoformat()))
                        rows = cur.fetchall()
                if not rows: return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"まだ{today.month}月の実績は登録されてへんで！"))
                msg = f"💰 【{today.month}月の稼ぎ状況】\n"
                total_all = 0
                for r in rows:
                    msg += f"・{r['job_name']}: ¥{r['total']:,} ({r['shift_count']}回)\n"
                    total_all += r['total']
                return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg + f"--------------\n合計目安: ¥{total_all:,}"))
            except Exception as e: return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"集計エラー: {e}"))

        if re.search(r'(予定|よてい)(は|確認|教えて|[？?])?$', line0) and "削除" not in line0:
            clean_date_str = re.sub(r'(の?予定|よてい)(は|確認|教えて|[？?])?$', '', line0).strip()
            d_obj = safe_parse_date(clean_date_str, today)
            
            try:
                with get_db_connection() as conn:
                    with conn.cursor(cursor_factory=DictCursor) as cur:
                        cur.execute("SELECT period, subject_name FROM attendance WHERE date::text = %s ORDER BY period ASC", (d_obj.strftime('%Y-%m-%d'),))
                        recs = cur.fetchall()
                        cur.execute("SELECT detail, start_time, end_time FROM lifestyle_schedules WHERE event_date::text = %s ORDER BY start_time ASC", (d_obj.strftime('%Y-%m-%d'),))
                        life_recs = cur.fetchall()
                msg = f"【{d_obj.strftime('%m/%d')} 予定】\n📚 講義:\n" + ("\n".join([f" {r['period']}限:{r['subject_name']}" for r in recs]) if recs else " なし")
                if life_recs:
                    msg += "\n🏠 その他:\n" + "\n".join([f" {r['start_time'].strftime('%H:%M') if r['start_time'] else '時間指定なし'}〜:{r['detail']}" for r in life_recs])
                return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
            except Exception as e:
                return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"予定確認エラー: {e}"))

        if "テンプレ" in line0 or "入力" in line0:
            return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📋 テンプレ:\n水曜 4限 眼科 追加\n明日 1限 提出物 リマインド 1日前\n明日の予定 削除\n5/20 泌尿器科 試験 場所:第1講義室"))
        
        if "ゆめちゃん" in line0:
            return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ゆめちゃんとの時間はしっかり確保してね！\n試験 ＞ 部活 ＞ バイト の優先順位でスケジュール管理していこう！"))
        
        if "休み" in line0 or "いつから" in line0:
            try:
                base_date = safe_parse_date(line0, today)
                days = 150 if "夏休み" in line0 else (60 if "2ヶ月" in line0 else 30)
                end_search = base_date + timedelta(days=days)
                with get_db_connection() as conn:
                    with conn.cursor(cursor_factory=DictCursor) as cursor:
                        cursor.execute("SELECT date, period, subject_name, status FROM attendance WHERE date >= %s AND date <= %s ORDER BY date ASC, period ASC", (base_date.isoformat(), end_search.isoformat()))
                        all_recs = cursor.fetchall()
                sched = {}
                for r in all_recs:
                    d_str = r['date'].strftime('%Y-%m-%d') if hasattr(r['date'], 'strftime') else str(r['date'])
                    if d_str not in sched: sched[d_str] = []
                    sched[d_str].append(r)
                res = []
                summer_start = None
                curr = base_date
                while curr <= end_search:
                    if curr.weekday() < 5:
                        d_str = curr.strftime('%Y-%m-%d')
                        day_data = sched.get(d_str, [])
                        occ = {str(r['period']) for r in day_data if r['status'] not in ['休講', '欠席'] and "休み" not in r['subject_name']}
                        spec = [r['subject_name'] for r in day_data if "休み" in r['subject_name']]
                        if "夏休み" in spec and summer_start is None: summer_start = curr
                        wd = "月火水木金"[curr.weekday()]
                        if not occ: res.append(f"・{curr.strftime('%m/%d')}({wd}): {spec[0] if spec else '(全休)'}")
                        else:
                            empty = [p for p in range(1, 7) if str(p) not in occ]
                            if empty: res.append(f"・{curr.strftime('%m/%d')}({wd}): {','.join(map(str, empty))}限空き")
                    curr += timedelta(days=1)
                reply = f"☀️ 夏休みは【{summer_start.strftime('%m/%d')}】から！" if "夏休み" in line0 and summer_start else f"🗓 {base_date.strftime('%m/%d')}以降の休み:\n" + "\n".join(res[:30])
                return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            except Exception as e: return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"エラー: {e}"))

    # ==========================================
    # B. 複数行対応の「更新」コマンド
    # ==========================================
    replies = []
    del_count = 0
    add_life_count = 0
    add_lec_count = 0
    
    current_date = today
    current_range = None
    
    range_match = re.search(r'(\d{1,2}[/月]\d{1,2})[〜~～-](\d{1,2}[/月]\d{1,2})', lines[0])
    if range_match:
        current_range = (safe_parse_date(range_match.group(1), today), safe_parse_date(range_match.group(2), today))
        current_date = current_range[0]
        lines[0] = lines[0].replace(range_match.group(0), "").strip()

    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                for line in lines:
                    line = line.strip()
                    if not line: continue
                    if line in ["給料", "実績", "きゅうりょう", "じっせき"]: continue 
                    
                    m_date = re.match(r'^(今日|明日|明後日|明々後日|(?:\d{1,2}[/月])?\d{1,2}日?|[月火水木金土日]曜日?)(?:\s+|$)(.*)', line)
                    if m_date:
                        date_str = m_date.group(1)
                        current_date = safe_parse_date(date_str, current_date)
                        line = m_date.group(2).strip()
                        if not line: continue
                            
                    target_dates = []
                    if current_range:
                        wd_map = {'月':0, '火':1, '水':2, '木':3, '金':4, '土':5, '日':6}
                        found_wd = None
                        for k, v in wd_map.items():
                            if f"{k}曜" in line:
                                found_wd = v
                                break
                        if found_wd is not None:
                            curr = current_range[0]
                            while curr <= current_range[1]:
                                if curr.weekday() == found_wd: target_dates.append(curr)
                                curr += timedelta(days=1)
                    if not target_dates: target_dates = [current_date]

                    for t_date in target_dates:
                        if "削除" in line or "消して" in line:
                            if "実績" in line:
                                job = "ファミマ" if "ファミマ" in line else "トライ" if "トライ" in line else ""
                                cur.execute("DELETE FROM work_results WHERE work_date = %s AND job_name LIKE %s", (t_date.isoformat(), f"%{job}%"))
                                del_count += cur.rowcount
                            else:
                                m_period = re.search(r'(\d+)限', line)
                                if m_period:
                                    cur.execute("DELETE FROM attendance WHERE date = %s AND period = %s", (t_date.isoformat(), m_period.group(1)))
                                    del_count += cur.rowcount
                                else:
                                    del_time_match = re.search(r'(\d{1,2})[時:](半|\d{1,2})?分?', line)
                                    del_start_t = None
                                    if del_time_match:
                                        h1 = del_time_match.group(1)
                                        m_raw = del_time_match.group(2)
                                        m1 = "30" if m_raw == "半" else (m_raw or "00")
                                        del_start_t = f"{h1.zfill(2)}:{m1.zfill(2)}:00"

                                    kw_text = line.replace("削除", "").replace("消して", "")
                                    kw_text = re.sub(r'\d{1,2}[時:](?:半|\d{1,2})?分?(?:\s*[-－ー~〜～]\s*\d{1,2}[時:]?(?:半|\d{1,2})?分?)?', '', kw_text)
                                    noise = ["の", "は", "予定", "？", "!", "！"]
                                    for n in noise: kw_text = kw_text.replace(n, " ")
                                    keywords = [w for w in kw_text.split() if w]
                                    
                                    query = "DELETE FROM lifestyle_schedules WHERE event_date = %s"
                                    params = [t_date.isoformat()]
                                    
                                    if del_start_t:
                                        query += " AND start_time = %s"
                                        params.append(del_start_t)
                                        
                                    for k in keywords:
                                        query += " AND detail LIKE %s"
                                        params.append(f"%{k}%")
                                        
                                    cur.execute(query, params)
                                    del_count += cur.rowcount
                            continue

                        m_add = re.search(r'(\d+)限\s*(.+?)\s*追加', line)
                        if m_add:
                            cur.execute("INSERT INTO attendance (date, period, subject_name, status) VALUES (%s, %s, %s, '予定')", (t_date.isoformat(), m_add.group(1), m_add.group(2)))
                            add_lec_count += 1
                            continue

                        m_assign = re.search(r'(.+?)\s*提出物(?:\s*リマインド\s*(\d+)日前)?', line)
                        if m_assign:
                            remind = t_date - timedelta(days=int(m_assign.group(2))) if m_assign.group(2) else None
                            cur.execute("INSERT INTO assignments (deadline, subject_name, content, remind_date) VALUES (%s, %s, %s, %s)", (t_date.isoformat(), m_assign.group(1), "課題登録", remind))
                            replies.append(f"📝 {t_date.strftime('%m/%d')}締切「{m_assign.group(1)}」登録")
                            continue

                        res_match = re.search(r'(ファミマ|ふぁみま|トライ|とらい).*?実績.*?(\d{1,2})(?:[:時](\d{1,2})分?)?[-－ー~〜](\d{1,2})(?:[:時](\d{1,2})分?)?(.*?休憩(\d+)(時間|分))?', line)
                        if res_match:
                            job_raw = res_match.group(1)
                            h1, m1 = int(res_match.group(2)), int(res_match.group(3) or 0)
                            h2, m2 = int(res_match.group(4)), int(res_match.group(5) or 0)
                            start_str, end_str = f"{h1:02}:{m1:02}", f"{h2:02}:{m2:02}"
                            rest_val = int(res_match.group(7) or 0)
                            rest_min = rest_val * 60 if res_match.group(8) == "時間" else rest_val
                            job_type = 'try_admin' if '事務' in line else 'try_instruction' if 'トライ' in job_raw or 'とらい' in job_raw else 'ファミマ'
                            
                            wage_key = 'wage_famima' if job_type == 'ファミマ' else f"wage_{job_type}"
                            cur.execute("SELECT value FROM settings WHERE key = %s", (wage_key,))
                            wage_row = cur.fetchone()
                            wage = int(wage_row['value']) if wage_row else 1000
                            pay, _ = calculate_salary(start_str, end_str, wage, rest_min)
                            
                            j_name = job_type.replace('try_admin','トライ(事務)').replace('try_instruction','トライ(講師)')
                            cur.execute("INSERT INTO work_results (job_name, work_date, actual_start, actual_end, pay_amount) VALUES (%s, %s, %s, %s, %s)", (j_name, t_date.isoformat(), start_str, end_str, pay))
                            replies.append(f"💰 {j_name} 実績登録! ({pay}円)")
                            continue

                        if re.search(r'(トライ|とらい).*?(終了|おわり)', line):
                            job_type = 'try_admin' if '事務' in line else 'try_instruction'
                            wage_key = f"wage_{job_type}"
                            cur.execute("SELECT start_time, end_time FROM lifestyle_schedules WHERE event_date = %s AND sub_category = 'try'", (t_date.isoformat(),))
                            sched = cur.fetchone()
                            if sched:
                                cur.execute("SELECT value FROM settings WHERE key = %s", (wage_key,))
                                wage = int(cur.fetchone()['value'])
                                s_str, e_str = sched['start_time'].strftime('%H:%M'), sched['end_time'].strftime('%H:%M')
                                pay, _ = calculate_salary(s_str, e_str, wage)
                                j_label = "トライ(事務)" if "admin" in wage_key else "トライ(講師)"
                                cur.execute("INSERT INTO work_results (job_name, work_date, actual_start, actual_end, pay_amount) VALUES (%s, %s, %s, %s, %s)", (j_label, t_date.isoformat(), s_str, e_str, pay))
                                replies.append(f"💰 {j_label} 実績完了! ({pay}円)")
                            else:
                                replies.append(f"⚠️ {t_date.strftime('%m/%d')}のトライ予定なし")
                            continue

                        once_match = re.search(r'単発.*?(\d+)[円円]', line)
                        if once_match:
                            cur.execute("INSERT INTO work_results (job_name, work_date, pay_amount) VALUES ('単発', %s, %s)", (t_date.isoformat(), int(once_match.group(1))))
                            replies.append(f"💰 単発登録! ({once_match.group(1)}円)")
                            continue

                        # 💡究極の試験抽出フィルター（正規表現の壁を撤廃し、"試験"という文字で真っ二つに割る）
                        if "試験" in line and "追加" not in line and "削除" not in line:
                            parts = line.split("試験", 1)
                            subject = parts[0].strip()
                            location = parts[1].strip()
                            
                            # 「場所:」という文字があれば綺麗に消す
                            location = re.sub(r'^場所[:：]\s*', '', location).strip()
                            
                            if not subject:
                                subject = "試験"

                            cur.execute("INSERT INTO exams (exam_date, subject_name, location) VALUES (%s, %s, %s)", (t_date.isoformat(), subject, location))
                            
                            loc_text = f" (場所: {location})" if location else ""
                            replies.append(f"🎓 {t_date.strftime('%m/%d')}の試験「{subject}」を登録したよ！{loc_text}")
                            continue

                        time_match = re.search(r'(\d{1,2})[時:](半|\d{1,2})?分?(?:\s*[-－ー~〜～]\s*(\d{1,2})[時:]?(半|\d{1,2})?分?)?', line)
                        start_t = None
                        end_t = None
                        clean_detail = line
                        
                        if time_match:
                            h1 = time_match.group(1)
                            m_raw = time_match.group(2)
                            m1 = "30" if m_raw == "半" else (m_raw or "00")
                            start_t = f"{h1.zfill(2)}:{m1.zfill(2)}:00"
                            
                            if time_match.group(3):
                                h2 = time_match.group(3)
                                m2_raw = time_match.group(4)
                                m2 = "30" if m2_raw == "半" else (m2_raw or "00")
                                end_t = f"{h2.zfill(2)}:{m2.zfill(2)}:00"
                                
                            clean_detail = clean_detail.replace(time_match.group(0), "").strip()

                        clean_detail = re.sub(r'^(今日|明日|明後日|明々後日|(?:\d{1,2}[/月])?\d{1,2}日?|[月火水木金土日]曜日?)\s*', '', clean_detail).strip()

                        cat, sub_cat = 'private', 'private'
                        if re.search(r'(ファミマ|ふぁみま)', line): cat, sub_cat = 'part_time', 'famima'
                        elif re.search(r'(トライ|とらい)', line): cat, sub_cat = 'part_time', 'try'
                        elif re.search(r'(部活|ぶかつ)', line): cat, sub_cat = 'club', 'club'

                        if "入れ替え" in line or "いれかえ" in line:
                            cur.execute("DELETE FROM lifestyle_schedules WHERE event_date = %s AND start_time = %s", (t_date.isoformat(), start_t))
                            
                        cur.execute("INSERT INTO lifestyle_schedules (category, sub_category, event_date, start_time, end_time, detail) VALUES (%s, %s, %s, %s, %s, %s)", (cat, sub_cat, t_date.isoformat(), start_t, end_t, clean_detail))
                        add_life_count += 1

            conn.commit()
            
            final_msg_parts = []
            if del_count > 0: final_msg_parts.append(f"🗑 {del_count}件のデータを削除したで！")
            if add_lec_count > 0: final_msg_parts.append(f"✅ {add_lec_count}件の講義を追加！")
            if add_life_count > 0: final_msg_parts.append(f"✅ {add_life_count}件の予定を登録したよ！")
            if replies: final_msg_parts.extend(replies)
            
            if final_msg_parts:
                return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(final_msg_parts)))
            else:
                return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤔 該当データなしか、認識できんかったわ。"))
                
    except Exception as e:
        return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"エラー: {e}"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
