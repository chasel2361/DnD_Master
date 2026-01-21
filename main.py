import os
import json
import re
import random
import threading
import logging
from collections import deque
from flask import Flask
import discord
from discord.ext import commands
from google import genai  
from google.genai import types
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# --- 1. Logging 專業日誌設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("DnDBot")

# --- 2. Flask 保活網頁伺服器 ---
app = Flask('')

@app.route('/')
def home():
    return "DM is Online and Logging!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000)) 
    logger.info(f"📡 正在啟動 Flask 保活伺服器，Port: {port}")
    app.run(host='0.0.0.0', port=port)

# --- 3. Google Sheets 資料庫設定 ---
load_dotenv()
SCOPE = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

def get_sheet():
    creds_json = os.getenv("G_SHEET_JSON")
    if not creds_json:
        logger.error("❌ 找不到 G_SHEET_JSON 環境變數，請在 Render 設定頁面檢查。")
        return None
    try:
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)
        client = gspread.authorize(creds)
        # ⚠️ 請確保此處名稱與你的試算表名稱一致
        sheet_name = "你的試算表名稱" 
        return client.open(sheet_name).sheet1
    except Exception as e:
        logger.error(f"❌ 無法連接至 Google Sheets: {e}")
        return None

def save_to_sheets(players, log):
    logger.info("💾 正在同步資料至 Google Sheets...")
    sheet = get_sheet()
    if not sheet: return
    try:
        sheet.update_acell('A1', json.dumps(players, ensure_ascii=False))
        sheet.update_acell('B1', log)
        logger.info("✅ 雲端資料同步成功。")
    except Exception as e:
        logger.error(f"❌ 寫入 Sheets 失敗: {e}", exc_info=True)

def load_all_data():
    logger.info("🔍 正在從雲端讀取初始資料...")
    sheet = get_sheet()
    if not sheet: return {}, "冒險才剛開始。"
    try:
        data_cells = sheet.get('A1:B1')
        players, log = {}, "冒險才剛開始。"
        if data_cells and len(data_cells[0]) >= 1:
            players = json.loads(data_cells[0][0]) if data_cells[0][0] else {}
        if data_cells and len(data_cells[0]) >= 2:
            log = data_cells[0][1] if data_cells[0][1] else log
        logger.info("✅ 初始資料加載完成。")
        return players, log
    except Exception as e:
        logger.warning(f"⚠️ 讀取失敗，使用預設值: {e}")
        return {}, "冒險才剛開始。"

def get_modifier(stat_value):
    return (stat_value - 10) // 2

# --- 4. Gemini API 與 記憶設定 ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_model_name = 'gemini-2.0-flash'

client = genai.Client(api_key=GEMINI_API_KEY)
SYSTEM_INSTRUCTION = """你是一位專業的 D&D 5E 地下城主(DM)。
1. 請生動敘事，根據玩家行動與屬性描述後果。
2. 20 是大成功，1 是大失敗。"""

player_data = {}
adventure_log = ""
recent_chats = {}
AUTO_LOG_INTERVAL = 10 
message_counter = 0 

def build_dnd_prompt(user_input, char_info, adventure_log, history):
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    return f"""
【角色檔案】: {char_info}
【冒險日誌】: {adventure_log}
【近期對話】:
{history_text}
【玩家行動】: {user_input}
"""

async def auto_summarize(history, current_log):
    logger.info("🪄 觸發自動摘要機制...")
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    prompt = f"根據現有日誌：{current_log} 與最近紀錄：{history_text}，撰寫一份 300字內的精簡冒險日誌。"
    try:
        response = client.models.generate_content(
            model=gemini_model_name,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5)
        )
        logger.info("📝 摘要生成完畢。")
        return response.text.strip()
    except Exception as e:
        logger.error(f"❌ 摘要生成失敗: {e}")
        return current_log

# --- 5. Discord Bot 指令 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.command(name="roll")
async def roll(ctx, notation: str):
    logger.info(f"🎲 {ctx.author.name} 要求擲骰: {notation}")
    match = re.match(r'(\d+)d(\d+)([+-]\d+)?', notation.lower())
    if not match: return await ctx.send("❌ 格式錯誤 (例: !roll 1d20+5)")
    
    num, sides = int(match.group(1)), int(match.group(2))
    mod = int(match.group(3)) if match.group(3) else 0
    res = sum([random.randint(1, sides) for _ in range(num)]) + mod
    
    await ctx.send(f"🎲 **{ctx.author.name}** 擲出 **{res}**")
    try:
        resp = client.models.generate_content(
            model=gemini_model_name,
            contents=f"系統訊息：{ctx.author.name} 擲骰結果為 {res}。請描述後果。",
            config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.7)
        )
        await ctx.send(f"🎙️ **DM**: {resp.text}")
    except Exception as e:
        logger.error(f"Gemini 敘事錯誤: {e}")

@bot.command(name="create_char")
async def create_char(ctx, char_name: str, profession: str, *, bio_keywords: str):
    logger.info(f"👤 正在為 {ctx.author.name} 創建角色: {char_name}")
    user_id = str(ctx.author.id)
    prompt = f"建立 D&D 角色。姓名：{char_name}, 職業：{profession}, 背景：{bio_keywords}。格式: [STORY]...[STATS] Strength: 10... [END]"
    try:
        resp = client.models.generate_content(
            model=gemini_model_name, 
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction="請依 [STORY]...[STATS]...[END] 格式回傳。")
        )
        text = resp.text
        new_stats = {}
        for stat in ["strength", "dexterity", "intelligence", "wisdom", "constitution", "charisma"]:
            val = re.search(rf"{stat.capitalize()}:\s*(\d+)", text, re.IGNORECASE)
            if val: new_stats[stat] = int(val.group(1))

        player_data[user_id] = {"char_name": char_name, "profession": profession, "stats": new_stats}
        save_to_sheets(player_data, adventure_log)
        await ctx.send(f"✅ **{char_name}** 已同步至雲端試算表！")
    except Exception as e:
        logger.error(f"角色創建失敗: {e}")
        await ctx.send("❌ 角色生成出錯，請查看 Log。")

@bot.event
async def on_ready():
    global player_data, adventure_log
    player_data, adventure_log = load_all_data()
    logger.info(f"🎲 機器人已就緒：{bot.user}")

@bot.event
async def on_message(message):
    global message_counter, adventure_log
    if message.author == bot.user: return
    await bot.process_commands(message)
    
    if not message.content.startswith('!') and (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
        logger.info(f"💬 收到來自 {message.author.name} 的冒險行動")
        channel_id = str(message.channel.id)
        char_info = player_data.get(str(message.author.id), "初出茅廬的冒險者")
        
        if channel_id not in recent_chats: recent_chats[channel_id] = []
        
        full_prompt = build_dnd_prompt(message.content, char_info, adventure_log, recent_chats[channel_id])
        
        try:
            response = client.models.generate_content(
                model=gemini_model_name,
                contents=full_prompt,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.7)
            )
            reply = response.text
            await message.reply(reply)
            
            recent_chats[channel_id].append({"role": "玩家", "content": message.content})
            recent_chats[channel_id].append({"role": "DM", "content": reply})
            
            # --- 自動摘要邏輯 ---
            message_counter += 1
            if message_counter >= AUTO_LOG_INTERVAL:
                adventure_log = await auto_summarize(recent_chats[channel_id], adventure_log)
                save_to_sheets(player_data, adventure_log)
                message_counter = 0
            
            if len(recent_chats[channel_id]) > 10: recent_chats[channel_id] = recent_chats[channel_id][-10:]
        except Exception as e:
            logger.error(f"對話處理出錯: {e}")
            await message.reply("❌ DM 暫時斷線了，請稍後再試。")

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)