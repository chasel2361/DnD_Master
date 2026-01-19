import os
import threading
from flask import Flask
import discord
from discord.ext import commands
import google.generativeai as genai
from dotenv import load_dotenv

# --- 新增：Flask 迷你網頁伺服器 ---
app = Flask('')

@app.route('/')
def home():
    return "I'm alive!"

def run_web_server():
    # Render 會自動分配 PORT，若無則預設 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.start()
# ------------------------------

# 讀取本地 .env 檔案 (本地測試用)
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 設定 Gemini
genai.configure(api_key=GEMINI_API_KEY)
# 定義你的 D&D 主持人風格
system_prompt = "你是一位專業的 D&D 5E 地下城主(DM)。請引導玩家冒險，保持神秘、生動的敘事，並在關鍵時刻要求玩家投骰。"
model = genai.GenerativeModel(
    model_name='gemini-2.5-flash-lite', # flash 速度快且便宜，適合聊天
    system_instruction=system_prompt
)

# 存儲各頻道的對話紀錄，達成多人共用記憶
chat_sessions = {}

# 設定 Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'🎲 D&D 冒險即將開始！機器人已登入為: {bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # 當機器人被標記 (@) 或在私訊中時回應
    if bot.user.mentioned_in(message) or isinstance(message.channel, discord.DMChannel):
        channel_id = message.channel.id
        
        # 初始化該頻道的對話
        if channel_id not in chat_sessions:
            chat_sessions[channel_id] = model.start_chat(history=[])
        
        # 移除訊息中的 @機器人 標籤，純化文字內容
        clean_content = message.content.replace(f'<@{bot.user.id}>', '').strip()
        user_input = f"{message.author.name}: {clean_content}"
        
        # 取得 Gemini 回應
        try:
            response = chat_sessions[channel_id].send_message(user_input)
            await message.reply(response.text)
        except Exception as e:
            await message.reply(f"❌ 發生錯誤: {str(e)}")

# 在啟動 Bot 之前，先啟動網頁伺服器
if __name__ == "__main__":
    keep_alive()  # 啟動保活網頁
    bot.run(os.getenv("DISCORD_TOKEN"))