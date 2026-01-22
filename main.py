import os
import json
import re
import random
import threading
import logging
from datetime import datetime
from flask import Flask
import discord
from discord.ext import commands
from google import genai  
from google.genai import types
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# --- 1. 專業 Logging 設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("DnDBot")

# --- 2. Flask 保活伺服器 ---
app = Flask('')

@app.route('/')
def home():
    return "DM 正在監視你的冒險... (Online)"

def run_web_server():
    try:
        port = int(os.environ.get("PORT", 10000)) 
        logger.info(f"📡 嘗試啟動 Flask 於 Port: {port}...")
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"❌ Flask 啟動失敗: {e}")

# --- 3. Google Sheets 資料庫邏輯 ---
load_dotenv()
SCOPE = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

def get_sheet():
    creds_json = os.getenv("G_SHEET_JSON")
    sheet_id = os.getenv("G_SHEET_ID")
    if not creds_json or not sheet_id:
        return None
    try:
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)
        client = gspread.authorize(creds)
        return client.open_by_key(sheet_id).sheet1
    except Exception as e:
        logger.error(f"❌ Sheets 連接失敗: {e}")
        return None

def save_to_sheets(players, log):
    sheet = get_sheet()
    if not sheet: return
    try:
        player_json = json.dumps(players, ensure_ascii=False)
        sheet.update_acell('A1', player_json)
        sheet.update_acell('B1', log)
        logger.info(f"✅ 資料已備份至雲端。玩家總數: {len(players)}")
    except Exception as e:
        logger.error(f"❌ 雲端同步失敗: {e}")

def load_all_data():
    sheet = get_sheet()
    if not sheet: return {}, "冒險才剛開始。"
    try:
        data_cells = sheet.get('A1:B1')
        players = json.loads(data_cells[0][0]) if data_cells and len(data_cells[0]) >= 1 and data_cells[0][0] else {}
        log = data_cells[0][1] if data_cells and len(data_cells[0]) >= 2 and data_cells[0][1] else "冒險才剛開始。"
        return players, log
    except:
        return {}, "冒險才剛開始。"

# --- 4. Gemini 與 核心邏輯 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_model_name = 'gemini-2.5-flash-lite'
genai_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = """你是一位專業的 D&D 5E 地下城主(DM)。
1. 請引導玩家冒險，保持生動敘事，描述環境的氣味、聲音與氛圍。
2. 根據玩家行動與其角色屬性描述後果。20 是大成功，1 是大失敗。
3. 玩家資料包含在 Prompt 中，請根據該玩家的身分做出回應。"""

player_data = {}
adventure_log = ""
recent_chats = {} # 格式: { channel_id: [history] }
message_counter = 0 
AUTO_LOG_INTERVAL = 10

def build_dnd_prompt(current_author_name, user_input, char_info, log, history):
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    return f"""
【當前玩家】: {current_author_name}
【角色檔案】: {char_info}
【世界進度摘要】: {log}
【近期對話紀錄】:
{history_text}

【{current_author_name} 的行動】: {user_input}
"""

async def auto_summarize(history, current_log):
    logger.info("🪄 正在自動更新冒險日誌摘要...")
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    prompt = f"請將以下對話與現有日誌合併，更新為一份 300 字內的冒險日誌摘要：\n現有：{current_log}\n新發生：{history_text}"
    try:
        response = genai_client.models.generate_content(
            model=gemini_model_name, 
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5)
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"摘要生成失敗: {e}")
        return current_log

# --- 5. Discord 機器人設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.command(name="roll")
async def roll(ctx, notation: str):
    """擲骰子，例如 !roll 1d20+5"""
    global message_counter, adventure_log
    logger.info(f"🎲 {ctx.author.name} 擲骰: {notation}")
    match = re.match(r'(\d+)d(\d+)([+-]\d+)?', notation.lower())
    if not match: 
        return await ctx.send("❌ 格式錯誤 (例: !roll 1d20+5)")
    
    num, sides = int(match.group(1)), int(match.group(2))
    mod = int(match.group(3)) if match.group(3) else 0
    rolls = [random.randint(1, sides) for _ in range(num)]
    total = sum(rolls) + mod
    
    roll_result_text = f"🎲 **{ctx.author.name}** 擲出了 **{total}** ({' + '.join(map(str, rolls))}{f' + {mod}' if mod else ''})"
    await ctx.send(roll_result_text)
    
    # 讓 DM 描述結果，並將結果存入共享記憶
    try:
        channel_id = str(ctx.channel.id)
        if channel_id not in recent_chats: recent_chats[channel_id] = []
        
        char_info = player_data.get(str(ctx.author.id), "一位冒險者")
        resp = genai_client.models.generate_content(
            model=gemini_model_name,
            contents=f"系統訊息：玩家 {ctx.author.name} ({char_info}) 擲骰結果為 {total} (對應行動: {notation})。請根據此數值描述冒險中的後果。",
            config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.7)
        )
        reply = resp.text
        await ctx.send(f"🎙️ **DM**: {reply}")

        # 將此事件加入對話紀錄，確保 DM 以後記得
        recent_chats[channel_id].append({"role": "玩家", "content": f"{ctx.author.name} 執行了 {notation} 擲骰，結果為 {total}"})
        recent_chats[channel_id].append({"role": "DM", "content": reply})

        # 擲骰也是冒險的一部分，計入自動摘要
        message_counter += 1
        if message_counter >= AUTO_LOG_INTERVAL:
            adventure_log = await auto_summarize(recent_chats[channel_id], adventure_log)
            save_to_sheets(player_data, adventure_log)
            message_counter = 0
            
    except Exception as e:
        logger.error(f"DM 描述失敗: {e}")

@bot.command(name="create_char")
async def create_char(ctx, char_name: str, profession: str, *, bio_keywords: str):
    """創建角色，例如 !create_char 愛隆 吟遊詩人 喜愛音樂與冒險"""
    global player_data
    user_id = str(ctx.author.id)
    logger.info(f"👤 玩家 {ctx.author.name} 建立角色: {char_name}")
    
    prompt = f"建立 D&D 角色。姓名：{char_name}, 職業：{profession}, 背景：{bio_keywords}。格式: [STORY]...[STATS] Strength: 10... [END]"
    try:
        resp = genai_client.models.generate_content(
            model=gemini_model_name, 
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction="請依標籤格式回傳故事與數值。")
        )
        text = resp.text
        stats = {}
        for s in ["strength", "dexterity", "intelligence", "wisdom", "constitution", "charisma"]:
            val = re.search(rf"{s.capitalize()}:\s*(\d+)", text, re.IGNORECASE)
            if val: stats[s] = int(val.group(1))

        player_data[user_id] = {"char_name": char_name, "profession": profession, "stats": stats}
        save_to_sheets(player_data, adventure_log)
        await ctx.send(f"✅ **{char_name}** 角色檔案已建立！\n{text}")
    except Exception as e:
        logger.error(f"創角失敗: {e}")
        await ctx.send("❌ 創角過程中發生錯誤。")

@bot.command(name="reset_adventure")
@commands.has_permissions(administrator=True)
async def reset_adventure(ctx):
    """管理員重置指令"""
    global player_data, adventure_log, message_counter
    logger.warning(f"🚨 {ctx.author.name} 執行了世界重置！")
    player_data = {}
    adventure_log = "冒險才剛開始，冒險者們正聚在一起準備出發。"
    message_counter = 0
    save_to_sheets(player_data, adventure_log)
    await ctx.send("🧹 **世界已重置**。所有雲端資料已清除，新故事即將開始。")

@bot.event
async def on_message(message):
    global message_counter, adventure_log
    if message.author == bot.user: return

    # 處理指令
    await bot.process_commands(message)
    
    # 聊天模式
    if not message.content.startswith('!') and (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
        channel_id = str(message.channel.id)
        user_id = str(message.author.id)
        char_info = player_data.get(user_id, "一位尚未註冊的神祕冒險者")
        
        if channel_id not in recent_chats: recent_chats[channel_id] = []
        
        full_prompt = build_dnd_prompt(
            message.author.name, 
            message.content, 
            char_info, 
            adventure_log, 
            recent_chats[channel_id]
        )
        
        try:
            response = genai_client.models.generate_content(
                model=gemini_model_name,
                contents=full_prompt,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.7)
            )
            reply = response.text
            await message.reply(reply)
            
            recent_chats[channel_id].append({"role": "玩家", "content": f"{message.author.name}: {message.content}"})
            recent_chats[channel_id].append({"role": "DM", "content": reply})
            
            message_counter += 1
            if message_counter >= AUTO_LOG_INTERVAL:
                adventure_log = await auto_summarize(recent_chats[channel_id], adventure_log)
                save_to_sheets(player_data, adventure_log)
                message_counter = 0
            
            if len(recent_chats[channel_id]) > 10: recent_chats[channel_id] = recent_chats[channel_id][-10:]
        except Exception as e:
            logger.error(f"對話處理出錯: {e}")
            await message.reply("❌ DM 目前暫時無法回應，請稍後再試。")

@bot.event
async def on_ready():
    global player_data, adventure_log
    logger.info(f"🎲 {bot.user} 登入成功！")
    
    player_data, adventure_log = load_all_data()
    
    notify_id_str = os.getenv("NOTIFY_CHANNEL_ID")
    if notify_id_str:
        try:
            notify_id = int(notify_id_str)
            channel = bot.get_channel(notify_id) or await bot.fetch_channel(notify_id)
            if channel:
                timestamp = datetime.now().strftime('%H:%M:%S')
                await channel.send(
                    f"✨ **DM 傳送門已開啟！** (啟動時間: {timestamp})\n"
                    f"已載入 {len(player_data)} 位冒險者檔案。輸入 `!create_char` 即可加入旅程！"
                )
                logger.info("📢 已發送啟動通知。")
        except Exception as e:
            logger.error(f"❌ 啟動通知失敗: {e}")

if __name__ == "__main__":
    # 1. 優先啟動 Flask 線程
    flask_thread = threading.Thread(target=run_web_server, daemon=True)
    flask_thread.start()
    
    # 2. 檢查必要的環境變數
    required_vars = ["DISCORD_TOKEN", "GEMINI_API_KEY", "G_SHEET_JSON", "G_SHEET_ID"]
    missing_vars = [v for v in required_vars if not os.getenv(v)]
    
    if missing_vars:
        logger.error(f"❌ 部署失敗：缺失環境變數 {missing_vars}")
        # 不退出程式，讓 Flask 繼續跑，以便在 Render 查看錯誤日誌
    else:
        try:
            logger.info("🤖 正在啟動 Discord Bot...")
            bot.run(os.getenv("DISCORD_TOKEN"))
        except Exception as e:
            logger.error(f"❌ Discord Bot 啟動失敗: {e}")