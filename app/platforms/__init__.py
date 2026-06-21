from app.platforms.steam import SteamPlatform
from app.platforms.retroachievements import RetroAchievementsPlatform
from app.platforms.xbox import XboxPlatform

PLATFORMS = {
    "steam": SteamPlatform,
    "retroachievements": RetroAchievementsPlatform,
    "xbox": XboxPlatform,
}
