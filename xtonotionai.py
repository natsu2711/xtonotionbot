# -*- coding: utf-8 -*-
import os
import time
import asyncio
import random
import re
from typing import Union
from playwright.async_api import async_playwright, TimeoutError
from notion_client import Client
from dotenv import load_dotenv



# 加载 .env 文件中的环境变量
load_dotenv()

# --- 配置区 ---
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
PROCESSED_TWEETS = set()

# === 核心配置 ===
LIKE_THRESHOLD = 10
REPOST_THRESHOLD = 5
LIKE_PROBABILITY = 0.6
TOTAL_SCROLLS = 20 
CUSTOM_EXPLAIN_PROMPT = "请对以下推文进行深度剖析，输出总长度不得超过2000字,用中文回答。回答需严格按照以下结构：1. 摘要：用300字以内高度概括推文的本质观点和核心矛盾。2. 核心知识点（3–5条）：只保留与推文主题最紧密相关的知识点，每条≤100字。3. 核心问题（2–4条）：列出推文背后真正要解决的关键矛盾，每条≤100字。4. 业务运作全流程（4–6步）：用简明步骤展示该问题在真实情境中的运行逻辑。然后，仅选择最重要的2个知识点和2个核心问题，逐一进行“四层次深度剖析：第一性原理：指出该领域稳定不变的通用法则，解释其矛盾与盲点，可引用经典书籍/论文。模型：给出基于原理的结构化框架或算法（简化、可度量、可演算）。操作：写出4–6步的流程化实施方案，解释每步的原因，并用贴切类比帮助理解。经验：提供一个真实案例或故事，展示执行后的反馈与复盘。要求：  全文≤2000字，必要时优先保留“摘要 > 核心问题 > 第一性原理 > 模型”。  使用纯文本输出，不允许出现任何 Markdown 格式符号（例如 ###、**、-、•）。避免赘述与重复，不要输出客套话，不要多余元信息。输出风格要简洁凝练，但思想深刻。"
DAILY_GOAL = 5

# --- 辅助函数：解析数字 ---
def parse_count(text: str) -> int:
    if not text: return 0
    text_upper = text.upper()
    match = re.search(r'([\d.,]+)\s?([KM]?)', text_upper)
    if not match: return 0
    number_str = match.group(1).replace(',', '')
    modifier = match.group(2)
    try:
        value = float(number_str)
        if modifier == 'K': return int(value * 1000)
        elif modifier == 'M': return int(value * 1000000)
        else: return int(value)
    except (ValueError, TypeError): return 0

# === 更新：add_to_notion 函数，增加智能截断功能 ===
async def add_to_notion(summary_text, tweet_url, original_tweet_text):
    """根据您的数据库字段 (Name, URL, 简介) 将内容添加到Notion"""
    try:
        query_results = notion.databases.query(database_id=NOTION_DATABASE_ID, filter={"property": "URL", "url": {"equals": tweet_url}})
        if query_results["results"]:
            print(f"    - Skipped Notion: Tweet already exists. URL: {tweet_url}")
            return False
        
        # 准备简介内容
        summary_content = summary_text or "No summary generated."
        
        # 智能截断：如果内容超过2000字符，则截取并添加省略号
        if len(summary_content) > 2000:
            summary_content = summary_content[:1995] + "..."
            print("    - INFO: Summary was truncated to fit Notion's 2000 character limit.")

        properties = {
            "简介": {"rich_text": [{"text": {"content": summary_content}}]},
            "URL": {"url": tweet_url},
            "Name": {"title": [{"text": {"content": original_tweet_text or "Untitled Tweet"}}]}
        }
        
        notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
        print(f"    - ✅ SUCCESS: Added generated summary to Notion.")
        return True
    except Exception as e:
        print(f"    - ❌ ERROR: Failed to add to Notion. Reason: {e}")
        return False

async def get_summary_from_grok_site(context, tweet_url) -> Union[str , None]:
    """打开grok.com，粘贴链接和提示词，并精确地复制AI的回答"""
    grok_page = None
    try:
        print("    - Navigating to grok.com...")
        grok_page = await context.new_page()
        await grok_page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=60000)

        prompt_input = grok_page.locator("textarea[aria-label='向 Grok 提任何问题']")
        await prompt_input.wait_for(state="visible", timeout=60000)
        
        full_prompt = f"这是推文链接: {tweet_url}\n\n{CUSTOM_EXPLAIN_PROMPT}"
        await prompt_input.fill(full_prompt)
        
        submit_button = grok_page.locator("button[aria-label='提交']")
        await submit_button.click()
        print("    - ACTION: Submitted prompt to grok.com. Waiting for response...")

        stop_button = grok_page.locator("button[aria-label='停止模型响应']")
        await stop_button.wait_for(state="hidden", timeout=300000)
        await asyncio.sleep(2)  # 给 DOM 渲染留时间
        print("    - INFO: AI response is complete.")
        await asyncio.sleep(1)

        last_response_container = grok_page.locator(".last-response")
        copy_button = last_response_container.locator("button[aria-label='复制']")
        await copy_button.wait_for(state="visible", timeout=300000)
        await copy_button.click()
        print("    - ACTION: Clicked the AI's specific 'Copy' button.")
        await asyncio.sleep(0.5)

        generated_summary = await grok_page.evaluate("() => navigator.clipboard.readText()")
        print("    - ✅ SUCCESS: Successfully read AI summary from clipboard.")

        return generated_summary
    except Exception as e:
        print(f"    - ❌ ERROR on grok.com: {type(e).__name__} - {e}")
        return None
    finally:
        if grok_page:
            await grok_page.close()
            print("    - ACTION: Closed grok.com tab.")


# === 修正语法的 scrape_main_timeline 函数 ===
async def scrape_main_timeline():
    """主函数，适配云端环境，采用实时流处理逻辑，并持久化登录状态"""
    processed_count = 0
    browser = None
    context = None
    async with async_playwright() as p:
        try:
            # --- 浏览器和上下文的设置 ---
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(storage_state="storage_state.json")
                print("✅ Launched browser and loaded existing storage_state.json.")
            except FileNotFoundError:
                print("⚠️ storage_state.json not found. Creating a new context.")
                context = await browser.new_context()
            
            page = await context.new_page()

            # --- 核心抓取逻辑 ---
            await page.goto("https://x.com", wait_until="domcontentloaded", timeout=60000)
            print(f"--- Navigated to main timeline. Goal: {DAILY_GOAL} summaries. ---")

            for i in range(TOTAL_SCROLLS):
                if processed_count >= DAILY_GOAL:
                    print(f"\n🎉 Daily goal of {DAILY_GOAL} summaries reached. Halting script.")
                    break

                print(f"\n--- Scrolling... Round {i+1}/{TOTAL_SCROLLS} ---")
                await page.mouse.wheel(0, 8000)
                await asyncio.sleep(4)

                articles = page.locator('article[data-testid="tweet"]')
                count = await articles.count()
                print(f"  - Found {count} potential tweets on the page.")

                for i in range(count):
                    if processed_count >= DAILY_GOAL: break
                    
                    article = articles.nth(i)
                    try:
                        link_locator = article.locator("a[href*='/status/']").first
                        href = await link_locator.get_attribute('href')
                        if not href: continue
                        tweet_url = "https://x.com" + href

                        if tweet_url in PROCESSED_TWEETS: continue
                        
                        button_group = article.locator("div[role='group']")
                        like_locator = button_group.locator("button[aria-label*='Like']")
                        repost_locator = button_group.locator("button[aria-label*='Repost']")
                        like_text = await like_locator.inner_text() if await like_locator.count() > 0 else ""
                        repost_text = await repost_locator.inner_text() if await repost_locator.count() > 0 else ""
                        like_count = parse_count(like_text)
                        repost_count = parse_count(repost_text)
                        #新增调试日志
                        print(f"  - [DEBUG] Checking tweet: {tweet_url} (Likes: {like_count}, Reposts: {repost_count})")
                        if like_count > LIKE_THRESHOLD or repost_count > REPOST_THRESHOLD:
                            print(f"\n  -> Found high-value tweet: {tweet_url} (Likes: {like_count}, Reposts: {repost_count})")
                            PROCESSED_TWEETS.add(tweet_url)

                            text_locator = article.locator('[data-testid="tweetText"]')
                            original_tweet_text = await text_locator.inner_text() if await text_locator.count() > 0 else tweet_url
                            
                            if random.random() < LIKE_PROBABILITY:
                                if await like_locator.is_visible() and "Unlike" not in (await like_locator.get_attribute("aria-label")):
                                        await like_locator.click()
                                        print("  - ✅ ACTION: Liked tweet to train algorithm.")
                                        await asyncio.sleep(random.uniform(1, 3))
                                else: print("  - INFO: Tweet was already liked.")
                            else: print("  - INFO: Skipped liking due to random chance.")
                            
                            summary_text = await get_summary_from_grok_site(context, tweet_url)
                            
                            if summary_text:
                                is_added = await add_to_notion(summary_text, tweet_url, original_tweet_text)
                                if is_added:
                                    processed_count += 1
                                    print(f"  - PROGRESS: {processed_count}/{DAILY_GOAL} summaries collected.")
                            else:
                                print("  - INFO: Failed to get summary from grok.com, moving to next tweet.")
                    except Exception as e:
                        # 忽略处理单条推文时的错误
                        continue
        
        # 将 except 和 finally 移到与 try 对齐的位置
        except Exception as e:
            print(f"❌ A FATAL ERROR occurred in the main process: {type(e).__name__} - {e}")

        finally:
            # --- 确保最后保存状态并关闭浏览器 ---
            if context:
                print("\n--- Saving updated storage state... ---")
                # 增加一个检查，确保文件存在才尝试保存，避免第一次运行出错
                if os.path.exists("storage_state.json"):
                    await context.storage_state(path="storage_state.json")
                    print("--- Updated storage state saved to storage_state.json. ---")
            if browser:
                await browser.close()
                print("--- Browser closed. ---")

        print(f"\n--- Script finished. Total summaries collected: {processed_count} ---")



if __name__ == "__main__":
    notion = Client(auth=NOTION_API_KEY)
    asyncio.run(scrape_main_timeline())