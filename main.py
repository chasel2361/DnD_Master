import os
import json
import re
import random
import threading
import logging
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
    return "DM is Online, Logging and Ready!"

def run_web_server():
    # 這裡加入預防性處理，確保 PORT 一定有數值
    try:
        port = int(os.environ.get("PORT", 10000)) 
        logger.info(f"📡 嘗試啟動 Flask 於 Port: {port}...")
        # 加上 use_reloader=False 避免在 Thread 中啟動兩次
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"❌ Flask 啟動失敗: {e}")

# --- 3. Google Sheets 資料庫邏輯 ---
load_dotenv()
SCOPE = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

def get_sheet():
    creds_json = os.getenv("G_SHEET_JSON")
    sheet_id = os.getenv("G_SHEET_ID") # 建議將試算表 ID 存於環境變數
    
    if not creds_json or not sheet_id:
        logger.error("❌ 缺失 Google Sheets 必要環境變數 (G_SHEET_JSON 或 G_SHEET_ID)")
        return None
    try:
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)
        client = gspread.authorize(creds)
        return client.open_by_key(sheet_id).sheet1
    except Exception as e:
        logger.error(f"❌ 無法連接至 Google Sheets: {e}")
        return None

def save_to_sheets(players, log):
    logger.info("💾 正在發起雲端同步...")
    sheet = get_sheet()
    if not sheet: 
        logger.error("❌ 同步失敗：無法取得 Sheet 物件")
        return
    try:
        player_json = json.dumps(players, ensure_ascii=False)
        sheet.update_acell('A1', player_json)
        sheet.update_acell('B1', log)
        logger.info(f"✅ 同步完成。玩家數: {len(players)}, 日誌長度: {len(log)}")
    except Exception as e:
        logger.error(f"❌ 寫入 Sheets 失敗: {e}", exc_info=True)

def load_all_data():
    logger.info("🔍 正在從雲端抓取冒險進度...")
    sheet = get_sheet()
    if not sheet: return {}, "冒險才剛開始。"
    try:
        data_cells = sheet.get('A1:B1')
        players = {}
        log = "冒險才剛開始。"
        
        if data_cells and len(data_cells[0]) >= 1:
            if data_cells[0][0]:
                players = json.loads(data_cells[0][0])
                logger.info("✅ 成功讀取 A1 角色檔案。")
        
        if data_cells and len(data_cells[0]) >= 2:
            if data_cells[0][1]:
                log = data_cells[0][1]
                logger.info("✅ 成功讀取 B1 冒險日誌。")
            
        return players, log
    except Exception as e:
        logger.warning(f"⚠️ 讀取失敗，使用預設值啟動: {e}")
        return {}, "冒險才剛開始。"

# --- 4. Gemini 與 核心邏輯 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_model_name = 'gemini-2.5-flash-lite'
client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = """你是一位專業的 D&D 5E 地下城主(DM)。
1. 請引導玩家冒險，保持生動敘事。
2. 根據玩家行動與屬性描述後果。20 是大成功，1 是大失敗。"""

# 全域狀態變數
player_data = {}
adventure_log = ""
recent_chats = {}
AUTO_LOG_INTERVAL = 10 
message_counter = 0 

def build_dnd_prompt(user_input, char_info, log, history):
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    return f"""
【角色檔案】: {char_info}
【冒險日誌】: {log}
【近期對話紀錄】:
{history_text}

【玩家行動】: {user_input}
"""

async def auto_summarize(history, current_log):
    logger.info("🪄 正在自動更新冒險日誌摘要...")
    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    prompt = f"請將以下對話與現有日誌合併，撰寫一份新的、300字內的冒險日誌摘要：\n現有日誌：{current_log}\n新對話：{history_text}"
    try:
        response = client.models.generate_content(
            model=gemini_model_name,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5)
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"❌ 摘要生成失敗: {e}")
        return current_log

# --- 5. Discord 指令集 ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
NOTIFY_CHANNEL_ID = os.getenv("NOTIFY_CHANNEL_ID")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.command(name="roll")
async def roll(ctx, notation: str):
    logger.info(f"🎲 {ctx.author.name} 擲骰: {notation}")
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
        logger.error(e)

@bot.command(name="create_char")
async def create_char(ctx, char_name: str, profession: str, *, bio_keywords: str):
    global player_data
    logger.info(f"👤 玩家 {ctx.author.name} 請求建立角色: {char_name}")
    user_id = str(ctx.author.id)
    prompt = f"建立 D&D 角色。姓名：{char_name}, 職業：{profession}, 背景：{bio_keywords}。格式: [STORY]...[STATS] Strength: 10... [END]"
    try:
        resp = client.models.generate_content(
            model=gemini_model_name, 
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction="請依標籤格式回傳故事與數值。")
        )
        text = resp.text
        new_stats = {}
        for stat in ["strength", "dexterity", "intelligence", "wisdom", "constitution", "charisma"]:
            val = re.search(rf"{stat.capitalize()}:\s*(\d+)", text, re.IGNORECASE)
            if val: new_stats[stat] = int(val.group(1))

        player_data[user_id] = {"char_name": char_name, "profession": profession, "stats": new_stats}
        save_to_sheets(player_data, adventure_log)
        await ctx.send(f"✅ **{char_name}** 角色已建立並同步至雲端！")
    except Exception as e:
        logger.error(f"❌ 角色創建失敗: {e}")
        await ctx.send("❌ 角色生成出錯。")

@bot.command(name="reset_adventure")
@commands.has_permissions(administrator=True)
async def reset_adventure(ctx):
    global player_data, adventure_log, message_counter
    logger.warning(f"🚨 玩家 {ctx.author.name} 執行世界重置！")
    player_data = {}
    adventure_log = "冒險才剛開始，冒險者們正聚在一起準備出發。"
    message_counter = 0
    save_to_sheets(player_data, adventure_log)
    await ctx.send("🧹 **世界已重置**。所有雲端資料已清除。")

# --- 6. 事件監聽 ---

@bot.event
async def on_ready():
    global player_data, adventure_log
    logger.info(f"🎲 機器人登入成功：{bot.user}")
    
    # 測試與雲端連線
    player_data, adventure_log = load_all_data()
    
    notify_id = os.getenv("NOTIFY_CHANNEL_ID")
    if notify_id:
        try:
            # 修正點：使用 fetch_channel 替代 get_channel
            channel = await bot.fetch_channel(int(notify_id))
            await channel.send(f"✨ **傳送門已開啟！** (重啟時間: {datetime.now().strftime('%H:%M:%S')})\nDM 已經就緒，並同步了 {len(player_data)} 位冒險者的資料。")
            logger.info(f"📢 已向頻道 {notify_id} 發送啟動通知。")
        except Exception as e:
            logger.error(f"❌ 發送啟動通知失敗: {e}")

@bot.event
async def on_message(message):
    global message_counter, adventure_log
    if message.author == bot.user: return
    await bot.process_commands(message)
    
    if not message.content.startswith('!') and (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
        logger.info(f"💬 收到行動: {message.author.name}")
        channel_id = str(message.channel.id)
        char_info = player_data.get(str(message.author.id), "一位神祕的新冒險者")
        
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
            
            recent_chats[channel_id].append({"role": "玩家", "content": f"{message.author.name}: {message.content}"})
            recent_chats[channel_id].append({"role": "DM", "content": reply})
            
            # --- 自動更新日誌 ---
            message_counter += 1
            if message_counter >= AUTO_LOG_INTERVAL:
                adventure_log = await auto_summarize(recent_chats[channel_id], adventure_log)
                save_to_sheets(player_data, adventure_log)
                message_counter = 0
            
            if len(recent_chats[channel_id]) > 10: 
                recent_chats[channel_id] = recent_chats[channel_id][-10:]
        except Exception as e:
            logger.error(f"對話處理出錯: {e}")
            await message.reply("❌ DM 喉嚨不太舒服 (API 錯誤)，請稍後再試。")

if __name__ == "__main__":
    # 1. 優先啟動 Flask 線程
    flask_thread = threading.Thread(target=run_web_server, daemon=True)
    flask_thread.start()
    
    # 2. 檢查必要的環境變數，若缺失則直接報錯在 Log，不要讓它默默死掉
    required_vars = ["DISCORD_TOKEN", "GEMINI_API_KEY", "G_SHEET_JSON", "G_SHEET_ID"]
    missing_vars = [v for v in required_vars if not os.getenv(v)]
    
    if missing_vars:
        logger.error(f"❌ 部署失敗：缺失環境變數 {missing_vars}")
        # 這裡不退出，讓 Flask 繼續跑，這樣 Render 的 Log 才會顯示錯誤而不是直接 Timeout
    else:
        try:
            logger.info("🤖 正在啟動 Discord Bot...")
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            logger.error(f"❌ Discord Bot 啟動失敗: {e}")