"""查看 API key 使用情况：python show_keys.py"""

from dotenv import load_dotenv
from mimo_agent.key_pool import KeyPool
from mimo_agent.config import MODEL

load_dotenv()

pool = KeyPool.from_env(MODEL)
print(pool.summary())
