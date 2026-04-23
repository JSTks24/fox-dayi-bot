from .cog import UnansweredFilter
from . import cog as _cog_module

# 供测试和外部代码直接访问的模块级常量。
# 注意：测试通过 `pending_questions_reviewer.DB_DIR = ...` 修改这些值时，
# 需要实际写入 cog 子模块才能生效。这里用 __getattr__ / __setattr__
# 透传对 DB_DIR / DB_PATH / TARGET_FORUM_ID 的读写到 cog 子模块。

_FORWARDED_ATTRS = {"DB_DIR", "DB_PATH", "TARGET_FORUM_ID"}


def __getattr__(name: str):
    if name in _FORWARDED_ATTRS:
        return getattr(_cog_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __setattr__(name: str, value):
    if name in _FORWARDED_ATTRS:
        setattr(_cog_module, name, value)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def setup(bot):
    await bot.add_cog(UnansweredFilter(bot))
