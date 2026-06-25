from app.platforms.steam import SteamPlatform
from app.platforms.retroachievements import RetroAchievementsPlatform
from app.platforms.xbox import XboxPlatform
from app.platforms.wargaming import WargamingPlatform
from app.platforms.guildwars2 import GuildWars2Platform

PLATFORMS = {
    "steam": SteamPlatform,
    "retroachievements": RetroAchievementsPlatform,
    "xbox": XboxPlatform,
    "wargaming": WargamingPlatform,
    "guildwars2": GuildWars2Platform,
}
