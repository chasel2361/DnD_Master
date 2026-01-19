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
SYSTEM_INSTRUCTION = "你是一位專業的 D&D 5E 地下城主(DM)。請引導玩家冒險，保持神秘、生動的敘事，並在關鍵時刻要求玩家投骰。"

# 存儲對話紀錄 (新版 SDK 處理方式略有不同)
chat_sessions = {}

# --- 3. Discord Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'🎲 系統就緒：{bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    
    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        channel_id = str(message.channel.id)
        clean_content = message.content.replace(f'<@{bot.user.id}>', '').strip()
        
        # 呼叫 Gemini (最新 SDK 語法)
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash", # 使用 2026 年的主流模型
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