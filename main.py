import os
import json
import re
import random
import threading
from collections import deque
from flask import Flask
import discord
from discord.ext import commands
from google import genai  
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from google.genai import types

# --- 1. Flask 保活設定 ---
app = Flask('')

@app.route('/')
def home():
    return "DM is Online!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000)) 
    app.run(host='0.0.0.0', port=port)

# --- 2. Google Sheets 初始化 ---
load_dotenv()
SCOPE = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

def get_sheet():
    creds_json = os.getenv("G_SHEET_JSON")
    if not creds_json:
        print("❌ 找不到 Google Sheets 金鑰變數")
        return None
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)
    client = gspread.authorize(creds)
    # ⚠️ 這裡請確保與你的 Google Sheet 標題一致
    return client.open("你的試算表名稱").sheet1

def save_to_sheets(players, log):
    sheet = get_sheet()
    if not sheet: return
    try:
        sheet.update_acell('A1', json.dumps(players, ensure_ascii=False))
        sheet.update_acell('B1', log)
    except Exception as e:
        print(f"寫入 Sheets 失敗: {e}")

def load_all_data():
    sheet = get_sheet()
    if not sheet: return {}, "冒險才剛開始。"
    try:
        data_cells = sheet.get('A1:B1')
        players = {}
        log = "冒險才剛開始，冒險者們正聚在一起準備出發。"
        if len(data_cells) > 0:
            if len(data_cells[0]) >= 1:
                players = json.loads(data_cells[0][0]) if data_cells[0][0] else {}
            if len(data_cells[0]) >= 2:
                log = data_cells[0][1] if data_cells[0][1] else log
        return players, log
    except Exception as e:
        print(f"讀取 Sheets 出錯: {e}")
        return {}, "冒險才剛開始。"

def get_modifier(stat_value):
    return (stat_value - 10) // 2

# --- 3. Gemini 設定 ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_model_name = 'gemini-2.5-flash-lite' # 建議使用穩定型號

client = genai.Client(api_key=GEMINI_API_KEY)
SYSTEM_INSTRUCTION = """你是一位專業的 D&D 5E 地下城主(DM)。
1.請引導玩家冒險，保持生動敘事。
2.當玩家擲骰後，根據結果描述後果。20 是大成功，1 是大失敗。"""

# --- 4. 記憶管理 ---
player_data = {}
adventure_log = ""
recent_chats = {}
AUTO_LOG_INTERVAL = 10 
message_counter = 0 

def build_dnd_prompt(user_input, char_info, adventure_log, history):
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    return f"""
【第一層：角色檔案】
{char_info}
【第二層：冒險日誌】
{adventure_log}
【第三層：近期對話】
{history_text}
【玩家目前行動】
{user_input}
"""

async def auto_summarize(history, current_log):
    print("🪄 正在自動更新冒險日誌...")
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    prompt = f"根據現有日誌：{current_log} 與最近紀錄：{history_text}，撰寫一份更新後的、300字內精簡冒險日誌。"
    
    try:
        response = client.models.generate_content(
            model=gemini_model_name,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5)
        )
        return response.text.strip()
    except Exception as e:
        print(f"摘要失敗: {e}")
        return current_log

# --- 5. Discord Bot 邏輯 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def roll_dice(notation):
    match = re.match(r'(\d+)d(\d+)([+-]\d+)?', notation.lower())
    if not match: return None
    num, sides = int(match.group(1)), int(match.group(2))
    mod = int(match.group(3)) if match.group(3) else 0
    rolls = [random.randint(1, sides) for _ in range(num)]
    return {"rolls": rolls, "total": sum(rolls) + mod, "mod": mod}

@bot.command(name="roll")
async def roll(ctx, notation: str):
    res = roll_dice(notation)
    if not res: return await ctx.send("❌ 格式錯誤 (例: !roll 1d20+5)")
    await ctx.send(f"🎲 **{ctx.author.name}** 擲出 **{res['total']}**")
    try:
        response = client.models.generate_content(
            model=gemini_model_name,
            contents=f"系統訊息：{ctx.author.name} 擲骰結果為 {res['total']}。請描述後果。",
            config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.7)
        )
        await ctx.send(f"🎙️ **DM**: {response.text}")
    except Exception as e:
        print(e)

@bot.command(name="create_char")
async def create_char(ctx, char_name: str, profession: str, *, bio_keywords: str):
    user_id = str(ctx.author.id)
    await ctx.send(f"✨ 正在為 {ctx.author.name} 創造角色...")
    prompt = f"建立 D&D 角色。姓名：{char_name}, 職業：{profession}, 背景：{bio_keywords}。格式: [STORY]...[STATS] Strength: 10... [END]"
    try:
        response = client.models.generate_content(
            model=gemini_model_name, 
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction="請依 [STORY]...[STATS]...[END] 格式回傳。")
        )
        text = response.text
        new_stats = {}
        for stat in ["strength", "dexterity", "intelligence", "wisdom", "constitution", "charisma"]:
            val = re.search(rf"{stat.capitalize()}:\s*(\d+)", text, re.IGNORECASE)
            if val: new_stats[stat] = int(val.group(1))

        player_data[user_id] = {"char_name": char_name, "profession": profession, "stats": new_stats}
        save_to_sheets(player_data, adventure_log)
        await ctx.send(f"✅ **{char_name}** 已存入雲端！")
    except Exception as e:
        await ctx.send(f"❌ 錯誤: {e}")

@bot.event
async def on_ready():
    global player_data, adventure_log
    player_data, adventure_log = load_all_data()
    print(f'🎲 系統就緒：{bot.user}')

@bot.event
async def on_message(message):
    global message_counter, adventure_log
    if message.author == bot.user: return
    await bot.process_commands(message)
    
    if not message.content.startswith('!') and (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
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
            
            # --- 自動更新日誌 ---
            message_counter += 1
            if message_counter >= AUTO_LOG_INTERVAL:
                adventure_log = await auto_summarize(recent_chats[channel_id], adventure_log)
                save_to_sheets(player_data, adventure_log)
                message_counter = 0
            
            if len(recent_chats[channel_id]) > 10: recent_chats[channel_id] = recent_chats[channel_id][-10:]
        except Exception as e:
            await message.reply(f"❌ 錯誤: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)