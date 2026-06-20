from app.platforms.steam import SteamPlatform
from app.platforms.retroachievements import RetroAchievementsPlatform

PLATFORMS = {
    "steam": SteamPlatform,
    "retroachievements": RetroAchievementsPlatform,
}
