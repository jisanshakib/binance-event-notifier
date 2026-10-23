"""AI parsing + strict Telegram Markdown formatting (all bold except link)."""
import asyncio
import json
import re
import config

PROMPT = """You extract crypto campaign details from a Binance announcement.
Return ONLY valid JSON with these keys:
{{"title": short campaign title (e.g. "Spot Trading Tournament S6"),
 "reward": short reward line (e.g. "Get Up 350 $FF"),
 "eligibility": who can join (e.g. "For All User"),
 "title_emoji": one emoji matching the campaign (🛒 trading, 🪂 airdrop, 💰 earn, 🏆 tournament),
 "steps": list of 2-5 very short task steps (e.g. ["Register Event Page","Make $500 Volume on $FF/USDT","More Volume More Reward"])}}
Do not include dates. Keep every value under 60 characters.

Telegram post:
{post}

Announcement title: {title}
Announcement text:
{text}
"""


def _parse_json(s: str) -> dict:
    m = re.search(r"\{.*\}", s or "", re.S)
    d = json.loads(m.group(0)) if m else {}
    if not isinstance(d, dict):
        return {}
    if not isinstance(d.get("steps"), list):
        d["steps"] = []
    return d


async def _ask(prompt: str) -> str:
    if config.AI_PROVIDER == "openai":
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
        r = await client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        return r.choices[0].message.content
    import google.generativeai as genai
    genai.configure(api_key=config.GEMINI_API_KEY)
    model = genai.GenerativeModel(config.GEMINI_MODEL)
    r = await asyncio.to_thread(model.generate_content, prompt)
    return r.text


def _clean(s: str) -> str:
    # remove characters that would break **bold** markdown
    return str(s or "").replace("*", "").replace("[", "(").replace("]", ")").replace("_", " ").strip()


def _safe_url(u: str) -> str:
    return (u or "https://www.binance.com").replace(")", "%29").replace("(", "%28").replace(" ", "%20")


def b(s: str) -> str:
    s = _clean(s)
    return f"**{s}**" if s else ""


def build_post(d: dict, link: str, start: str | None, end: str | None, link_label: str = "Open✅") -> str:
    lines = [
        b(f"{_clean(d.get('title_emoji') or '🛒')}{d.get('title', 'New Binance Event')}"),
        b(f"🎁 {d.get('reward', 'Check Rewards')}"),
        b(f"🎉 {d.get('eligibility', 'For All User')}"),
        "",
        f"{b('🔗 Link :')} [{link_label}]({_safe_url(link)})",
        b("• Event Goes Live"),
        b(f"at {start or 'Now'} ✅"),
        b("• Event Will Expire"),
        b(f"{end or 'Check Announcement'} ❕"),
    ]
    for step in [x for x in (d.get("steps") or []) if isinstance(x, str) and x.strip()][:5]:
        lines.append(b(f"➡️{step}"))
    lines.append(b("➡️Done"))
    return "\n".join(lines)


async def format_post(post_text: str, page: dict, start, end) -> str:
    try:
        raw = await asyncio.wait_for(
            _ask(PROMPT.format(post=post_text[:3000], title=page.get("title", ""), text=page.get("text", "")[:8000])),
            timeout=60,
        )
        data = _parse_json(raw)
    except Exception as e:
        print("AI error:", repr(e))
        data = {}
    if not data.get("title"):  # AI failed or returned nothing -> safe fallback, never crash
        first = next((l.strip() for l in (post_text or "").splitlines() if l.strip()), "")
        data["title"] = (page.get("title") or first or "New Binance Event")[:60]
    return build_post(data, page.get("join_link") or "https://www.binance.com", start, end)
