import asyncio

from app.config import load_settings
from app.db import Database
from app.llm.cache import LLMCache
from app.llm.policy_rag import get_policy_context

POLICY_IDS = [
    "shipping_policy", "returns_refunds", "business_identity", "misrepresentation",
    "prohibited_content", "editorial_quality", "privacy_policy", "terms_of_service",
]

QUERY = (
    "What happens if a merchant violates this policy - is the individual product "
    "disapproved, or can the entire Google Merchant Center account be suspended?"
)


async def main():
    settings = load_settings()
    db = Database(settings.database_url)
    await db.init()
    cache = LLMCache(db)

    with open("tier_research_output_utf8.txt", "w", encoding="utf-8") as out:
        for policy_id in POLICY_IDS:
            ctx = await get_policy_context(policy_id, QUERY, settings, db, cache, top_n=4)
            out.write("=" * 100 + "\n")
            out.write(f"{policy_id} | from_real_index: {ctx.from_real_index if ctx else None}\n")
            if ctx:
                out.write("SUMMARY:\n" + ctx.summary + "\n")
                out.write("CITATIONS: " + repr(ctx.citations) + "\n")
    await db.dispose()


asyncio.run(main())
