from dotenv import load_dotenv
from openai import OpenAI
import os
load_dotenv()

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")