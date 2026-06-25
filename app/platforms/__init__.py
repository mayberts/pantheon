from app.platforms.steam import SteamPlatform
from app.platforms.retroachievements import RetroAchievementsPlatform
from app.platforms.xbox import XboxPlatform
from app.platforms.wargaming import WargamingPlatform

PLATFORMS = {
    "steam": SteamPlatform,
    "retroachievements": RetroAchievementsPlatform,
    "xbox": XboxPlatform,
    "wargaming": WargamingPlatform,
}
