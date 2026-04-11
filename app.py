import os
import psycopg2
from psycopg2.extras import DictCursor
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime, date
import pytz

app = Flask(__name__)

# Renderの環境変数から鍵を受け取る設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
SUPABASE_URI = os.environ.get('SUPABASE_URI')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def get_db_connection():
    return psycopg2.connect(SUPABASE_URI)

# サーバーが生きているか確認するためのURL
@app.route("/")
def hello():
    return "Bot is running!"

# LINEからのメッセージを受け取る窓口
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# メッセージに対する返答ロジック
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text
    reply = ""

    # 「テスト」や「試験」と聞かれた場合
    if "テスト" in user_msg or "試験" in user_msg or "CBT" in user_msg:
        try:
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=DictCursor) as cursor:
                    # 今日の日付以降の試験データを取得
                    cursor.execute("SELECT task_name, task_date FROM tasks WHERE task_type = '試験' AND task_date >= CURRENT_DATE ORDER BY task_date ASC LIMIT 5")
                    tests = cursor.fetchall()

            if not tests:
                reply = "今のところ予定されている試験はないみたい！やったね！"
            else:
                reply = "直近の試験予定だよ！🩺\n\n"
                today = datetime.now(pytz.timezone('Asia/Tokyo')).date()
                
                for t in tests:
                    t_date = date.fromisoformat(t['task_date'])
                    delta = (t_date - today).days
                    if delta > 0:
                        reply += f"📅 {t['task_name']}\n期日: {t['task_date']} (あと {delta} 日)\n\n"
                    elif delta == 0:
                        reply += f"🚨 {t['task_name']}\n今日が本番！全力を出し切って！\n\n"
                
                reply += "日々の勉強大変だと思うけど、応援してるよ！"
        except Exception:
            reply = "ごめんね、データ取得でエラーが発生しちゃった😭"

    # 「予定」と聞かれた場合
    elif "予定" in user_msg or "今日" in user_msg:
        try:
            today_str = datetime.now(pytz.timezone('Asia/Tokyo')).strftime('%Y-%m-%d')
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=DictCursor) as cursor:
                    cursor.execute("SELECT period, subject_name FROM attendance WHERE date = %s ORDER BY period ASC", (today_str,))
                    records = cursor.fetchall()
            
            if not records:
                reply = "今日の講義予定は登録されていないみたい！\nゆっくり休むか、CBTの勉強を進めよう📚"
            else:
                reply = f"【今日の時間割】({today_str})\n\n"
                for r in records:
                    reply += f"{r['period']}限: {r['subject_name']}\n"
                reply += "\n今日も一日頑張ろう！"
        except Exception:
            reply = "ごめんね、時間割の取得に失敗しちゃった😭"
            
    # シークレット機能
    elif "ゆめちゃん" in user_msg:
        reply = "ゆめちゃんとの時間はしっかり確保してね！\n試験 ＞ 部活 ＞ バイト の優先順位でスケジュール管理していこう！"

    else:
        reply = "「テストいつ？」や「今日の予定」と聞いてみてね！"

    # LINEに返信する
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
