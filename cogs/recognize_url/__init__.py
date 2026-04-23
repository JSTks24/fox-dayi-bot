import os

from paths import (
    API_TABLE_BAD_FILE,
    API_TABLE_GOOD_FILE,
    API_TABLE_PROMPT_FILE,
)

from .check_cog import URLCheckCog
from .table_cog import URLTableCog
from .url_matcher import URLMatcher

API_TABLE_PROMPT_PATH = os.fspath(API_TABLE_PROMPT_FILE)
API_TABLE_GOOD_PATH = os.fspath(API_TABLE_GOOD_FILE)
API_TABLE_BAD_PATH = os.fspath(API_TABLE_BAD_FILE)


def _make_matcher() -> URLMatcher:
    return URLMatcher(API_TABLE_GOOD_PATH, API_TABLE_BAD_PATH, API_TABLE_PROMPT_PATH)


async def setup(bot):
    matcher = _make_matcher()
    await bot.add_cog(URLCheckCog(bot, matcher))
    await bot.add_cog(URLTableCog(bot, matcher))
