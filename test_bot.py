"""End-to-end tests with an in-memory database and fake Telegram/AI/scraper.
Run:  python test_bot.py"""
import asyncio
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for k, v in dict(API_ID="1", API_HASH="x", SESSION_STRING="", BOT_TOKEN="1:x", ADMIN_ID="42",
                 SUPABASE_URL="http://x", SUPABASE_KEY="x").items():
    os.environ.setdefault(k, v)


# ---------- fake database (enforces unique constraints like Postgres) ----------
class Q:
    def __init__(self, db, name):
        self.db, self.name, self.op, self.filters, self.payload, self.conflict = db, name, "select", [], None, None

    def select(self, *_): self.op = "select"; return self
    def order(self, *_): return self
    def insert(self, p): self.op, self.payload = "insert", p; return self
    def upsert(self, p, on_conflict=None): self.op, self.payload, self.conflict = "upsert", p, on_conflict; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, k, v): self.filters.append((k, v)); return self

    def _match(self, r): return all(r.get(k) == v for k, v in self.filters)

    def execute(self):
        rows = self.db.setdefault(self.name, [])
        uniq = {"keywords": ["word"], "source_channels": ["username"], "settings": ["key"],
                "processed_posts": ["source_chat", "message_id"]}[self.name]
        res = types.SimpleNamespace(data=[])
        if self.op == "select":
            res.data = [dict(r) for r in rows if self._match(r)]
        elif self.op == "delete":
            res.data = [r for r in rows if self._match(r)]
            self.db[self.name] = [r for r in rows if not self._match(r)]
        else:
            key = tuple(self.payload[c] for c in uniq)
            ex = next((r for r in rows if tuple(r.get(c) for c in uniq) == key), None)
            if ex and self.op == "insert":
                raise Exception('duplicate key value violates unique constraint (23505)')
            if ex:
                ex.update(self.payload)
            else:
                rows.append({"id": len(rows) + 1, **self.payload})
            res.data = [self.payload]
        return res


class FakeSB:
    def __init__(self): self.data = {}
    def table(self, n): return Q(self.data, n)


import supabase  # noqa: E402
FAKE = FakeSB()
supabase.create_client = lambda *a, **k: FAKE

import main, db, timeutil, ai_formatter  # noqa: E402

SENT = []


async def fake_send_message(dest, text, **k): SENT.append((dest, text))
async def fake_send_file(dest, path, caption=None, **k): SENT.append((dest, caption or "<file>"))


main.bot.send_message = fake_send_message
main.bot.send_file = fake_send_file
AI_CALLS = []


async def fake_ask(prompt):
    AI_CALLS.append(prompt)
    return '{"title":"Spot Trading Tournament S6","reward":"Get Up 350 $FF","eligibility":"For All User","title_emoji":"🛒","steps":["Register Event Page","Trade $500"]}'


ai_formatter._ask = fake_ask
main.scrape = lambda url: {"title": "T", "text": "Promotion Period: 2026-10-22 10:00 (UTC) to 2026-10-28 12:00 (UTC)",
                           "join_link": "https://www.binance.com/en/activity/x", "url": url}


def msg(mid, text, entities=None):
    return types.SimpleNamespace(id=mid, raw_text=text, entities=entities, reply_markup=None,
                                 photo=None, document=None, file=None)


def run(c): return asyncio.get_event_loop().run_until_complete(c) if False else asyncio.run(c)


def reset():
    FAKE.data.clear(); SENT.clear(); AI_CALLS.clear()
    db.set_setting("destination", "@dest")


# ---------------- tests ----------------
def test_schema_has_no_seed_keywords():
    sql = open(os.path.join(os.path.dirname(__file__), "..", "schema.sql")).read().lower()
    assert "create table if not exists keywords" in sql
    assert "insert into keywords" not in sql


def test_keyword_crud_case_insensitive():
    reset()
    assert db.get_keywords() == []
    assert db.add_keyword("  Airdrop ") is True
    assert db.add_keyword("AIRDROP") is False          # no duplicate in other case
    assert db.get_keywords() == ["Airdrop"]
    assert db.add_keyword("100%_Bonus") is True        # % and _ are literal, not wildcards
    assert db.remove_keyword("100x Bonus") is False
    assert db.remove_keyword("airdrop") is True
    assert db.remove_keyword("airdrop") is False
    assert db.get_keywords() == ["100%_Bonus"]


def test_match_keyword_whole_word():
    kw = ["Will List", "airdrop", "$FF"]
    assert main.match_keyword("Binance WILL   list Foo (FOO)", kw)
    assert main.match_keyword("New AIRDROP campaign", kw)
    assert main.match_keyword("Trade $FF now", kw)
    assert not main.match_keyword("Airdrops are fun", ["airdrop"])  # exact word only
    assert not main.match_keyword("Listing soon", ["list"])
    assert not main.match_keyword("anything", [])
    assert not main.match_keyword("", ["x"])


def test_non_matching_post_skips_ai_and_posting():
    reset(); db.add_keyword("Tournament")
    run(main.process_message(msg(1, "BTC price is up today"), "binance"))
    assert AI_CALLS == [] and SENT == [] and FAKE.data.get("processed_posts", []) == []


def test_matching_post_is_posted_once_even_when_duplicated_concurrently():
    reset(); db.add_keyword("tournament")
    m = msg(2, "Spot Trading Tournament S6 https://www.binance.com/en/support/announcement/abc")
    async def both():
        await asyncio.gather(main.process_message(m, "binance"), main.process_message(m, "binance"))
    run(both())
    run(main.process_message(m, "binance"))  # edited / redelivered event
    assert len(SENT) == 1
    text = SENT[0][1]
    assert "**🛒Spot Trading Tournament S6**" in text
    assert "[Open✅](https://www.binance.com/en/activity/x)" in text
    assert "4:00 PM" in text and "28 OCT at 6:00 PM" in text
    assert FAKE.data["processed_posts"][0]["status"] == "posted"


def test_scrape_and_ai_failures_still_post():
    reset(); db.add_keyword("airdrop")
    orig_s, orig_a = main.scrape, ai_formatter._ask
    def boom(url): raise RuntimeError("403 blocked")
    async def ai_boom(p): raise RuntimeError("quota")
    main.scrape, ai_formatter._ask = boom, ai_boom
    try:
        run(main.process_message(msg(3, "HODLer Airdrop: XYZ\nhttps://www.binance.com/en/support/announcement/q"), "binance"))
    finally:
        main.scrape, ai_formatter._ask = orig_s, orig_a
    assert len(SENT) == 1 and "**🛒HODLer Airdrop: XYZ**" in SENT[0][1]


def test_missing_link_and_long_text():
    reset(); db.add_keyword("listing")
    run(main.process_message(msg(4, "New listing " + "x " * 5000), "binance"))
    assert len(SENT) == 1 and "https://www.binance.com" in SENT[0][1]


def test_no_destination_does_not_crash_or_claim():
    reset(); FAKE.data["settings"] = []; db.add_keyword("x")
    run(main.process_message(msg(5, "x"), "binance"))
    assert FAKE.data.get("processed_posts", []) == []


def test_unauthorized_blocked():
    replies = []
    called = []
    @main.admin_only
    async def h(e): called.append(1)
    async def reply(t): replies.append(t)
    run(h(types.SimpleNamespace(sender_id=999, reply=reply)))
    assert replies == ["⛔️ Unauthorized"] and called == []
    run(h(types.SimpleNamespace(sender_id=42, reply=reply)))
    assert called == [1]


def test_command_patterns():
    import re
    p = r"^/addkw(@\w+)?(\s|$)"
    assert re.match(p, "/addkw airdrop") and re.match(p, "/addkw@MyBot airdrop") and re.match(p, "/addkw")
    assert not re.match(p, "/addkwfoo")
    assert not re.match(r"^/dest(@\w+)?\s*$", "/destination")


def test_time_conversion_formats():
    f = timeutil.find_utc_times
    exp = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    for s in ["2026-10-22 10:00 (UTC)", "2026-10-22 10:00:00 UTC", "2026/10/22 10:00 UTC",
              "22 October 2026 10:00 UTC", "October 22, 2026, 10:00 (UTC)", "Oct 22, 2026 at 10:00 UTC"]:
        assert f(s) == [exp], s
    assert f("2026-13-45 10:00 UTC") == []           # invalid date does not crash
    assert f("no dates") == [] and f(None) == []
    now = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)
    assert timeutil.fmt_bst(exp, now) == "Today 4:00 PM"
    assert timeutil.fmt_bst(datetime(2026, 10, 22, 18, 30, tzinfo=timezone.utc), now) == "Tomorrow 12:30 AM"  # crosses midnight
    assert timeutil.fmt_bst(datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc), now) == "28 OCT at 12:00 PM"


if __name__ == "__main__":
    fails = 0
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            try: fn(); print("PASS", n)
            except Exception as e: fails += 1; print("FAIL", n, repr(e))
    sys.exit(1 if fails else 0)
