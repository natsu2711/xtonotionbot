# save_my_state.py
import asyncio
from playwright.async_api import async_playwright

CHROME_DEBUG_PORT = 9222
OUTPUT_FILE = "storage_state.json"

async def main():
    """
    连接到已经手动登录的Chrome实例，并将其登录状态保存到文件。
    """
    async with async_playwright() as p:
        try:
            print(f">>> 正在尝试连接到 localhost:{CHROME_DEBUG_PORT} 上的Chrome实例...")
            # 连接到您手动打开的浏览器
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{CHROME_DEBUG_PORT}")
            # 获取浏览器的第一个上下文（通常是默认的那个）
            context = browser.contexts[0]
            print(">>> ✅ 连接成功！")

            # 从这个已登录的上下文中抓取并保存状态
            await context.storage_state(path=OUTPUT_FILE)
            print(f">>> ✅ 登录状态已成功保存到文件: {OUTPUT_FILE}")
            print(">>> 您现在可以关闭此脚本和之前打开的Chrome窗口了。")

        except Exception as e:
            print(f"❌ 连接失败: {e}")
            print("---")
            print("请确保您已经按照指引，通过终端命令启动了一个带 --remote-debugging-port=9222 的Chrome实例。")

if __name__ == "__main__":
    asyncio.run(main())