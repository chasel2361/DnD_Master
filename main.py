import os
import json
import threading
from flask import Flask
import discord
from discord.ext import commands
from google import genai  # 升級到最新 SDK
from dotenv import load_dotenv

# --- 1. 強化版 Flask 保活設定 ---
app = Flask('')

@app.route('/')
def home():
    return "DM is Online!"

def run_web_server():
    # Render 強制要求綁定 0.0.0.0 以及指定的 PORT
    port = int(os.environ.get("PORT", 10000)) 
    app.run(host='0.0.0.0', port=port)

# --- 2. 初始化設定 ---
DATA_FILE = "players.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# 在初始化時讀取資料
player_data = load_data()

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 使用最新的 google-genai 語法
client = genai.Client(api_key=GEMINI_API_KEY)
# 定義你的 D&D 主持人風格
SYSTEM_INSTRUCTION = """你是一位專業的 D&D 5E 地下城主(DM)。
1.請引導玩家冒險，保持神秘、生動的敘事，並在關鍵時刻要求玩家擲骰。
2.當玩家擲骰後，請根據結果描述「成功」或「失敗」的後果。
3.擲骰結果若得到 20 是「大成功(Critical Success)」，1 是「大失敗(Critical Fail)」。"""

# 存儲對話紀錄 (新版 SDK 處理方式略有不同)
chat_sessions = {}

# --- 3. Discord Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- 新增：擲骰子邏輯函數 ---
def roll_dice(notation):
    """解析 1d20+5 這種格式"""
    match = re.match(r'(\d+)d(\d+)([+-]\d+)?', notation.lower())
    if not match:
        return None
    
    num_dice = int(match.group(1))
    sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0
    
    rolls = [random.randint(1, sides) for _ in range(num_dice)]
    total = sum(rolls) + modifier
    return {"rolls": rolls, "total": total, "modifier": modifier}

# --- 指令：!roll ---
@bot.command(name="roll", help="擲骰子，例如 !roll 1d20+5")
async def roll(ctx, notation: str):
    result = roll_dice(notation)
    if not result:
        await ctx.send("❌ 格式錯誤！請使用像 `1d20+5` 的格式。")
        return

    roll_str = f"{' + '.join(map(str, result['rolls']))}"
    if result['modifier'] != 0:
        roll_str += f" (修正值: {result['modifier']})"
    
    msg = f"🎲 **{ctx.author.name}** 擲出了 **{result['total']}**\n(明細: {roll_str})"
    await ctx.send(msg)

    # 【核心連動】自動把擲骰結果傳給 Gemini 讓它接話
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=f"系統訊息：{ctx.author.name} 進行了動作並擲骰子，結果是 {result['total']}。請根據這個結果繼續敘事。",
            config={'system_instruction': SYSTEM_INSTRUCTION}
        )
        await ctx.send(f"🎙️ **DM**: {response.text}")
    except Exception as e:
        print(f"Gemini Error: {e}")

# 修改後的指令：!create_char 凱爾 潛行者 孤兒成長於貧民窟，擅長開鎖
@bot.command(name="create_char")
async def create_char(ctx, char_name: str, profession: str, *, bio_keywords: str):
    user_id = str(ctx.author.id) # 使用 Discord ID 作為唯一 Key，比名字更準確
    await ctx.send(f"✨ 正在為 {ctx.author.name} 創造角色：**{char_name}** ({profession})...")

    prompt = f"""
    請為一位玩家創建 D&D 5E 角色。
    角色姓名：{char_name}
    職業：{profession}
    玩家提供的背景線索：{bio_keywords}

    請執行以下任務：
    1. 根據背景線索，寫一段約 150 字的生動角色背景故事。
    2. 根據職業特性分配屬性值 (Stat 分數 8-16 之間)。
    
    請務必嚴格遵守以下格式回傳：
    [STORY]
    (這裡放故事內容)
    [STATS]
    Strength: 數值
    Dexterity: 數值
    Intelligence: 數值
    Wisdom: 數值
    Constitution: 數值
    Charisma: 數值
    [END]
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        text = response.text

        # 解析故事
        story_match = re.search(r"\[STORY\](.*?)\[STATS\]", text, re.DOTALL)
        story_text = story_match.group(1).strip() if story_match else "故事生成失敗"

        # 解析屬性
        stats_match = re.search(r"\[STATS\](.*?)\[END\]", text, re.DOTALL)
        new_stats = {}
        if stats_match:
            stats_text = stats_match.group(1)
            for stat in ["strength", "dexterity", "intelligence", "wisdom", "constitution", "charisma"]:
                val = re.search(rf"{stat.capitalize()}:\s*(\d+)", stats_text)
                if val:
                    new_stats[stat] = int(val.group(1))

        # 儲存完整的角色檔案
        player_data[user_id] = {
            "char_name": char_name,
            "profession": profession,
            "stats": new_stats,
            "story": story_text
        }
        save_data(player_data)

        # 組合回覆訊息
        embed = discord.Embed(title=f"角色建立成功：{char_name}", color=0x00ff00)
        embed.add_field(name="職業", value=profession, inline=True)
        embed.add_field(name="背景故事", value=story_text, inline=False)
        
        stat_display = ""
        for s, v in new_stats.items():
            stat_display += f"**{s.capitalize()}**: {v} ({get_modifier(v):+d})\n"
        embed.add_field(name="屬性數值", value=stat_display, inline=False)
        
        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"❌ 發生錯誤: {str(e)}")

@bot.event
async def on_ready():
    print(f'🎲 系統就緒：{bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    
    # 讓 bot.command 能正常運作
    await bot.process_commands(message)
    
    # 原本的聊天邏輯 (排除掉指令)
    if not message.content.startswith('!') and (bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel)):
        clean_content = message.content.replace(f'<@{bot.user.id}>', '').strip()
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=f"{message.author.name}: {clean_content}",
                config={'system_instruction': SYSTEM_INSTRUCTION}
            )
            await message.reply(response.text)
        except Exception as e:
            await message.reply(f"❌ 發生錯誤: {str(e)}")

if __name__ == "__main__":
    # 先啟動網頁伺服器線程
    threading.Thread(target=run_web_server, daemon=True).start()
    # 再啟動 Discord Bot
    bot.run(DISCORD_TOKEN)