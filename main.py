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
1. 請引導玩家冒險，保持生動敘事。
2. 根據玩家行動與屬性描述後果。
3. 玩家資料包含在 Prompt 中，請根據該玩家的身分做出回應。"""

player_data = {}
adventure_log = ""
recent_chats = {} # 格式: { channel_id: [history] }
message_counter = 0 
AUTO_LOG_INTERVAL = 10

def build_dnd_prompt(current_author_name, user_input, char_info, log, history):
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    return f"""
【當前發言玩家】: {current_author_name}
【發言者角色檔案】: {char_info}
【當前世界冒險日誌】: {log}
【此頻道近期對話紀錄】:
{history_text}

【{current_author_name} 的行動】: {user_input}
"""

async def auto_summarize(history, current_log):
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    prompt = f"請將以下對話與現有日誌合併，更新為一份 300 字內的冒險日誌摘要：\n現有：{current_log}\n新發生：{history_text}"
    try:
        response = genai_client.models.generate_content(model=gemini_model_name, contents=prompt)
        return response.text.strip()
    except:
        return current_log

# --- 5. Discord 機器人設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.command(name="create_char")
async def create_char(ctx, char_name: str, profession: str, *, bio_keywords: str):
    global player_data
    user_id = str(ctx.author.id)
    logger.info(f"👤 玩家 {ctx.author.name} (ID: {user_id}) 正在建立角色: {char_name}")
    
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

        # 將資料存入該 User ID 對應的空間
        player_data[user_id] = {"char_name": char_name, "profession": profession, "stats": stats}
        save_to_sheets(player_data, adventure_log)
        await ctx.send(f"✅ **{char_name}** 角色檔案已建立！這份檔案將連結至您的 Discord 帳號。\n{text}")
    except Exception as e:
        logger.error(f"創角失敗: {e}")

@bot.event
async def on_message(message):
    global message_counter, adventure_log
    if message.author == bot.user: return

    # 處理以 ! 開頭的指令
    await bot.process_commands(message)
    
    # 聊天模式：非指令且標記機器人或私訊
    if not message.content.startswith('!') and (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
        channel_id = str(message.channel.id)
        user_id = str(message.author.id)
        
        # 根據 User ID 抓取角色檔案
        char_info = player_data.get(user_id, "一位尚未在世界註冊的神祕冒險者")
        
        if channel_id not in recent_chats: recent_chats[channel_id] = []
        
        # 構建讓 AI 能辨識身分的 Prompt
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
            
            # 紀錄對話，包含發言者名字
            recent_chats[channel_id].append({"role": "玩家", "content": f"{message.author.name}: {message.content}"})
            recent_chats[channel_id].append({"role": "DM", "content": reply})
            
            # 自動摘要邏輯
            message_counter += 1
            if message_counter >= AUTO_LOG_INTERVAL:
                adventure_log = await auto_summarize(recent_chats[channel_id], adventure_log)
                save_to_sheets(player_data, adventure_log)
                message_counter = 0
            
            if len(recent_chats[channel_id]) > 10: recent_chats[channel_id] = recent_chats[channel_id][-10:]
        except Exception as e:
            logger.error(f"對話處理出錯: {e}")

@bot.event
async def on_ready():
    global player_data, adventure_log
    logger.info(f"🎲 {bot.user} 登入成功，準備開始主持冒險！")
    player_data, adventure_log = load_all_data()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(os.getenv("DISCORD_TOKEN"))