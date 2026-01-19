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

# --- 1. 強化版 Flask 保活設定 ---
app = Flask('')

@app.route('/')
def home():
    return "DM is Online!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000)) 
    app.run(host='0.0.0.0', port=port)

# --- 2. 初始化與 Google Sheets 設定 ---
load_dotenv()
SCOPE = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

def get_sheet():
    creds_json = os.getenv("G_SHEET_JSON")
    if not creds_json:
        print("❌ 找不到 Google Sheets 金鑰環境變數")
        return None
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)
    client = gspread.authorize(creds)
    # 請確保這裡的名稱與你的試算表一致
    return client.open("你的試算表名稱").sheet1

def save_to_sheets(players, log):
    sheet = get_sheet()
    if not sheet: return
    # A1 存角色，B1 存日誌
    sheet.update_acell('A1', json.dumps(players, ensure_ascii=False))
    sheet.update_acell('B1', log)

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
        print(f"讀取試算表出錯: {e}")
        return {}, "冒險才剛開始。"

def get_modifier(stat_value):
    return (stat_value - 10) // 2

# --- 3. Gemini 設定 ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_model_name = 'gemini-2.5-flash-lite'

client = genai.Client(api_key=GEMINI_API_KEY)
SYSTEM_INSTRUCTION = """你是一位專業的 D&D 5E 地下城主(DM)。
1.請引導玩家冒險，保持神秘、生動的敘事，並在關鍵時刻要求玩家擲骰。
2.當玩家擲骰後，請根據結果描述「成功」或「失敗」的後果。
3.擲骰結果若得到 20 是「大成功」，1 是「大失敗」。"""

# --- 4. 記憶管理 ---
# 全局變數暫存，減少對 Sheets 的讀取頻率
player_data = {}
adventure_log = ""
recent_chats = {}

def build_dnd_prompt(user_input, char_info, adventure_log, history):
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    prompt = f"""
【第一層：角色長期檔案】
{char_info}
【第二層：冒險日誌】
{adventure_log}
【第三層：近期對話紀錄】
{history_text}
【玩家目前行動】
{user_input}
"""
    return prompt

# 設定每隔多少次對話更新一次日誌
AUTO_LOG_INTERVAL = 10 
message_counter = 0 # 全局計數器

async def auto_summarize(history, current_log):
    print("🪄 正在自動更新冒險日誌...")
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    
    summarize_prompt = f"""
    你是一位負責記錄史詩的史官。請參考現有的【冒險日誌】以及【最近的對話紀錄】，
    撰寫一份更新後的、精簡的冒險日誌。
    
    【現有日誌】：{current_log}
    【最近紀錄】：{history_text}
    
    請確保：
    1. 保留重要的主線劇情（例如拿到的關鍵道具、擊敗的頭目）。
    2. 刪除瑣碎的對話。
    3. 總字數保持在 300 字以內，方便下次閱讀。
    """
    
    try:
        response = client.models.generate_content(
            model=gemini_model_name,
            contents=summarize_prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"自動摘要出錯: {e}")
        return current_log

# --- 5. Discord Bot 指令 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def roll_dice(notation):
    match = re.match(r'(\d+)d(\d+)([+-]\d+)?', notation.lower())
    if not match: return None
    num_dice = int(match.group(1))
    sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0
    rolls = [random.randint(1, sides) for _ in range(num_dice)]
    total = sum(rolls) + modifier
    return {"rolls": rolls, "total": total, "modifier": modifier}

@bot.command(name="roll")
async def roll(ctx, notation: str):
    result = roll_dice(notation)
    if not result:
        await ctx.send("❌ 格式錯誤！例如 `1d20+5`。")
        return
    msg = f"🎲 **{ctx.author.name}** 擲出了 **{result['total']}**"
    await ctx.send(msg)
    
    # 擲骰連動敘事
    try:
        response = client.models.generate_content(
            model=gemini_model_name,
            contents=f"系統訊息：{ctx.author.name} 擲骰結果是 {result['total']}。請描述後果。",
            config={'system_instruction': SYSTEM_INSTRUCTION}
        )
        await ctx.send(f"🎙️ **DM**: {response.text}")
    except Exception as e:
        print(f"Gemini Error: {e}")

@bot.command(name="create_char")
async def create_char(ctx, char_name: str, profession: str, *, bio_keywords: str):
    user_id = str(ctx.author.id)
    await ctx.send(f"✨ 正在為 {ctx.author.name} 創造角色...")

    prompt = f"請為玩家建立 D&D 角色。姓名：{char_name}, 職業：{profession}, 背景：{bio_keywords}。請依格式回傳 [STORY]...[STATS] Strength: 10... [END]"
    
    try:
        response = client.models.generate_content(model=gemini_model_name, contents=prompt)
        text = response.text
        
        # 解析屬性 (簡化版正則)
        new_stats = {}
        for stat in ["strength", "dexterity", "intelligence", "wisdom", "constitution", "charisma"]:
            val = re.search(rf"{stat.capitalize()}:\s*(\d+)", text, re.IGNORECASE)
            if val: new_stats[stat] = int(val.group(1))

        player_data[user_id] = {
            "char_name": char_name,
            "profession": profession,
            "stats": new_stats
        }
        # 同步回 Sheets
        save_to_sheets(player_data, adventure_log)
        await ctx.send(f"✅ **{char_name}** 已存入雲端試算表！")
    except Exception as e:
        await ctx.send(f"❌ 錯誤: {e}")

@bot.command(name="update_log")
async def update_log_command(ctx, *, new_summary: str):
    global adventure_log
    adventure_log = new_summary
    save_to_sheets(player_data, adventure_log)
    await ctx.send("✍️ **冒險日誌已更新至雲端**。")

@bot.event
async def on_ready():
    global player_data, adventure_log
    player_data, adventure_log = load_all_data()
    print(f'🎲 系統就緒：{bot.user}')

@bot.event
async def on_message(message):
    global message_counter, adventure_log, player_data
    if message.author == bot.user: return
    await bot.process_commands(message)
    
    if not message.content.startswith('!') and (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
        channel_id = str(message.channel.id)
        char_info = player_data.get(str(message.author.id), "初出茅廬的冒險者")
        
        if channel_id not in recent_chats: recent_chats[channel_id] = []
        
        full_prompt = build_dnd_prompt(message.content, char_info, adventure_log, recent_chats[channel_id])
        
        try:
            # 取得 Gemini 回應
            response = client.models.generate_content(...)
            reply = response.text
            await message.reply(reply)
            
            # 更新近期記憶視窗 (Layer 3)
            recent_chats[channel_id].append({"role": "玩家", "content": message.content})
            recent_chats[channel_id].append({"role": "DM", "content": reply})
            
            # --- 自動更新日誌邏輯 ---
            message_counter += 1
            if message_counter >= AUTO_LOG_INTERVAL:
                # 呼叫摘要函數
                new_log = await auto_summarize(recent_chats[channel_id], adventure_log)
                adventure_log = new_log
                
                # 同步到 Google Sheets
                save_to_sheets(player_data, adventure_log)
                
                # 重設計數器
                message_counter = 0
                print("✅ 冒險日誌已自動同步至 Google Sheets")
            # -----------------------

            # 保持近期記憶在一定長度
            if len(recent_chats[channel_id]) > 10:
                recent_chats[channel_id] = recent_chats[channel_id][-10:]
        except Exception as e:
            await message.reply(f"❌ 錯誤: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)