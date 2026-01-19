import os
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