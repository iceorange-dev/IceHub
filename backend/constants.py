"""Static product constants shared by IceHub backend modules.

This module intentionally contains data only.  Runtime-dependent paths and
mutable services belong to :mod:`backend.runtime` instead.
"""

from types import MappingProxyType

RYUU_API_BASE = 'https://generator.ryuu.lol'
SUDAMA_API = 'https://api.993499094.xyz/depotkeys.json'
GITHUB_API = 'https://api.github.com'

OST_DEFAULT_REPO = 'OpenSteam001/OpenSteamTool'
STEAMLESS_DEFAULT_REPO = 'atom0s/Steamless'
KRKRZ_REPO = '1F1E33-float32/Fuck_Cxdec_Check'

GITHUB_DOWNLOAD_HOSTS = frozenset({
    'github.com',
    'objects.githubusercontent.com',
    'release-assets.githubusercontent.com',
})

MAX_RELEASE_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_LUA_BYTES = 2 * 1024 * 1024
MAX_SUDAMA_BYTES = 256 * 1024 * 1024

OST_FILES = ('OpenSteamTool.dll', 'dwmapi.dll', 'xinput1_4.dll')

# Older GitHub assets may predate the API-provided digest field. Such assets
# are accepted only when a digest was independently verified and shipped in a
# reviewed IceHub release. Future digest-less releases fail closed.
TRUSTED_LEGACY_ASSET_DIGESTS = MappingProxyType({
    (
        'atom0s/steamless',
        'v3.1.0.5',
        'Steamless.v3.1.0.5.-.by.atom0s.zip',
        610646,
    ): 'sha256:e3e2d22e098ff3fb359b2876aa2bed9596f0501e6ff588cbffae90a76d2dc4f5',
})

# Independently verified files from OpenSteamTool v1.4.8. These hashes are
# used only to identify legacy IceHub deployments that predate ownership
# manifests; they are never a substitute for validating a downloaded archive.
TRUSTED_OST_FILE_HASHES = MappingProxyType({
    'dwmapi.dll': frozenset({
        'cc086189e9ae5f6fec1b9839110fd5ec5836e86989cb5f15cab80bb813df44f8',
    }),
    'OpenSteamTool.dll': frozenset({
        'b2ed24e0b4e2d0dae4caa8817ed4c0c34af8fdf056f356fd697ae1356ea22581',
    }),
    'xinput1_4.dll': frozenset({
        '730d6e3c1216228392cf336127ad663564cafdb560faf3ae8bdfcc8ca6f38a27',
    }),
})

STEAMLESS_INTERNAL_MANIFEST = '.icehub-ownership.json'

# Display name, CLI flag, default value, and UI description.
STEAMLESS_FLAGS = (
    ('realign', '--realign', True, '重新對齊 sections（建議）'),
    ('recalcchecksum', '--recalcchecksum', True, '重新計算 PE checksum（建議）'),
    ('keepbind', '--keepbind', False, '保留 .bind section（DRM stub 所在）'),
    ('keepstub', '--keepstub', False, '保留 DOS stub（一般歸零即可）'),
    ('dumppayload', '--dumppayload', False, '輸出 stub payload 供分析'),
    ('dumpdrmp', '--dumpdrmp', False, '輸出 SteamDRMP.dll 供分析'),
    ('exp', '--exp', False, '啟用實驗性 unpacker'),
    ('quiet', '--quiet', False, '安靜模式（不輸出 debug log）'),
)

KRKRZ_DLL_FILENAME = 'Version.dll'
KRKRZ_BUNDLED_REL = f'assets/krkrz/{KRKRZ_DLL_FILENAME}'
KRKRZ_PAYLOAD_FILENAME = 'Version.bin'
KRKRZ_BUNDLED_PAYLOAD_REL = f'assets/krkrz/{KRKRZ_PAYLOAD_FILENAME}'

DEFAULT_CONFIG = MappingProxyType({
    'config_version': 2,
    'Custom_Steam_Path': '',
    'Ryuu_Auth_Key': '',
    'Ryuu_API_Base': RYUU_API_BASE,
    'Sudama_API': SUDAMA_API,
    'Sudama_Cache_Hours': 24,
    'default_source': 'ryuu',
    'auto_fallback': True,
    'theme_mode': 'auto',
    'theme_color': '#ff8c00',
    'debug_mode': False,
    'installed_ost_version': '',
    'OST_Repo': OST_DEFAULT_REPO,
    'installed_steamless_version': '',
    'Steamless_Repo': STEAMLESS_DEFAULT_REPO,
})

__all__ = [
    'DEFAULT_CONFIG',
    'GITHUB_API',
    'GITHUB_DOWNLOAD_HOSTS',
    'KRKRZ_BUNDLED_PAYLOAD_REL',
    'KRKRZ_BUNDLED_REL',
    'KRKRZ_DLL_FILENAME',
    'KRKRZ_PAYLOAD_FILENAME',
    'KRKRZ_REPO',
    'MAX_LUA_BYTES',
    'MAX_RELEASE_DOWNLOAD_BYTES',
    'MAX_SUDAMA_BYTES',
    'OST_DEFAULT_REPO',
    'OST_FILES',
    'RYUU_API_BASE',
    'STEAMLESS_DEFAULT_REPO',
    'STEAMLESS_FLAGS',
    'STEAMLESS_INTERNAL_MANIFEST',
    'SUDAMA_API',
    'TRUSTED_LEGACY_ASSET_DIGESTS',
    'TRUSTED_OST_FILE_HASHES',
]
