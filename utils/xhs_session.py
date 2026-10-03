"""One durable browser profile per Xiaohongshu account."""
import json
from pathlib import Path
from conf import BASE_DIR

async def open_xhs_context(playwright, account_file):
    account_file = Path(account_file).resolve()
    profile = Path(BASE_DIR) / "browser_profiles" / "xiaohongshu" / account_file.stem
    marker = profile / "sau-session-imported"
    profile.mkdir(parents=True, exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        str(profile), executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
        headless=False, locale="zh-CN",
    )
    try:
        if not marker.exists() and account_file.exists():
            state = json.loads(account_file.read_text(encoding="utf-8"))
            await context.add_cookies(state.get("cookies", []))
            origins = {item["origin"]: item.get("localStorage", []) for item in state.get("origins", [])}
            await context.add_init_script("const seed = " + json.dumps(origins) + "; for (const item of seed[location.origin] || []) localStorage.setItem(item.name, item.value);")
            # Visit each stored origin so localStorage is actually persisted before marking import complete.
            page = await context.new_page()
            for origin in origins:
                if origin.startswith("https://") and origin.split("/")[2].endswith(".xiaohongshu.com"):
                    await page.goto(origin, wait_until="domcontentloaded", timeout=30000)
            await page.close()
            marker.write_text("imported", encoding="utf-8")
        return context
    except BaseException:
        await context.close()
        raise
